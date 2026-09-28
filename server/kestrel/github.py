"""GitHub - minimal, behind an interface.

Needed for three things, all mechanical: detecting the PR for a task branch
(-> `board_sync.record_pr_link` and a lifecycle transition), CI status for a
branch (what a `ci` wait in `waits.py` resolves against), and merging on green
CI (design doc §5 step 6 - merge is part of the test loop, not the end of it).

**Choice: REST over httpx, with a token from the environment, not the `gh`
CLI.** Three reasons, all following the pattern `board.py` already set for
Notion:

- Testability. The project's rule is that tests never call the real GitHub -
  `httpx.MockTransport` makes that trivial for a REST client and awkward for a
  subprocess wrapper (mocking `gh`'s stdout means maintaining a second, looser
  copy of its output format).
- No extra runtime dependency in the *code* path. The Pi will have `gh`
  logged in per the brief, which is exactly what makes a REST token cheap to
  obtain there (`gh auth token`) without making the server depend on the `gh`
  binary being on PATH at all.
- Symmetry with `NotionBoard`: one small async client per external system,
  each behind a `Protocol` with a fake, rather than two different integration
  styles for the two integrations this codebase has.

Token comes from `GITHUB_TOKEN` (falling back to `GH_TOKEN`, which is what
`gh auth token` and most CI runners already populate) - see `Config`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

import httpx

from .outcomes import Ok, Outcome, Refused

GITHUB_API_URL = "https://api.github.com"


class CIState(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"  # no checks reported at all, e.g. nothing configured


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    branch: str
    state: str  # "open" | "closed"
    merged: bool


@dataclass(frozen=True)
class CIStatus:
    state: CIState
    summary: str = ""  # one concise, factual line - this is what wakes the session

    def describe(self) -> str:
        return self.summary or f"CI is {self.state}"


@runtime_checkable
class GitHub(Protocol):
    async def find_pr_for_branch(self, branch: str) -> PullRequest | None:
        """The PR for a task branch, if one has been opened. `None` covers
        both "no PR yet" and "not found" - there is nothing else to search."""
        ...

    async def ci_status(self, branch: str) -> CIStatus:
        """Aggregate CI status for a branch's latest commit."""
        ...

    async def merge(self, pr_number: int) -> Outcome:
        """Squash-merge on green CI. `Refused` covers everything from
        merge conflicts to branch protection - the orchestrator treats any
        refusal as "landing did not happen", never as a crash."""
        ...

    async def workflow_run_status(self, workflow: str, sha: str) -> CIStatus | None:
        """Status of the named GitHub Actions workflow's run for `sha` - one
        way validation confirms the dev deploy actually happened for *this*
        merge, not just that some earlier deploy is still green. `None` means
        no run has been recorded for that sha yet (distinct from `PENDING`,
        which means one is in flight)."""
        ...

    async def pr_files(self, pr_number: int) -> list[str]:
        """File paths changed by a PR. Read at validation time (after the PR
        branch itself may already be gone, squashed into the default branch)
        for the unrequested-spec-change check - reusing the PR's own file
        list is simpler and cheaper than diffing commits by sha."""
        ...


class GitHubRestClient:
    def __init__(self, token: str, repo: str, *, client: httpx.AsyncClient | None = None) -> None:
        """`repo` is `owner/name`, matching how Callum already types it
        everywhere else (git remotes, PR URLs)."""
        self._repo = repo
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._client = client or httpx.AsyncClient(base_url=GITHUB_API_URL, headers=headers)
        if client is not None:
            self._client.headers.update(headers)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def find_pr_for_branch(self, branch: str) -> PullRequest | None:
        owner = self._repo.split("/", 1)[0]
        resp = await self._client.get(
            f"/repos/{self._repo}/pulls",
            params={"head": f"{owner}:{branch}", "state": "all", "per_page": 1},
        )
        resp.raise_for_status()
        results = resp.json()
        if not results:
            return None
        pr = results[0]
        return PullRequest(
            number=pr["number"],
            url=pr["html_url"],
            branch=branch,
            state=pr["state"],
            merged=bool(pr.get("merged_at")),
        )

    async def ci_status(self, branch: str) -> CIStatus:
        resp = await self._client.get(f"/repos/{self._repo}/commits/{branch}/check-runs")
        resp.raise_for_status()
        data = resp.json()
        runs = data.get("check_runs", [])
        if not runs:
            return CIStatus(CIState.UNKNOWN, "no checks have reported for this branch yet")

        incomplete = [r for r in runs if r.get("status") != "completed"]
        if incomplete:
            names = ", ".join(r["name"] for r in incomplete)
            return CIStatus(CIState.PENDING, f"still running: {names}")

        failed = [r for r in runs if r.get("conclusion") not in ("success", "skipped", "neutral")]
        if failed:
            worst = failed[0]
            detail = (worst.get("output") or {}).get("summary") or (worst.get("output") or {}).get(
                "title"
            )
            tail = f": {detail}" if detail else ""
            return CIStatus(
                CIState.FAILURE,
                f"CI failed at {worst['name']} ({worst.get('conclusion')}){tail}",
            )

        return CIStatus(CIState.SUCCESS, f"CI green ({len(runs)} check(s) passed)")

    async def merge(self, pr_number: int) -> Outcome:
        resp = await self._client.put(
            f"/repos/{self._repo}/pulls/{pr_number}/merge",
            json={"merge_method": "squash"},
        )
        if resp.status_code == 200:
            return Ok(value=resp.json().get("sha"))
        try:
            reason = resp.json().get("message", resp.text)
        except ValueError:
            reason = resp.text
        return Refused(reason=f"merge of PR #{pr_number} refused: {reason}")

    async def workflow_run_status(self, workflow: str, sha: str) -> CIStatus | None:
        resp = await self._client.get(
            f"/repos/{self._repo}/actions/workflows/{workflow}/runs",
            params={"head_sha": sha, "per_page": 1},
        )
        resp.raise_for_status()
        runs = resp.json().get("workflow_runs", [])
        if not runs:
            return None
        run = runs[0]
        if run.get("status") != "completed":
            return CIStatus(CIState.PENDING, f"{workflow} still running for {sha[:8]}")
        conclusion = run.get("conclusion")
        if conclusion == "success":
            return CIStatus(CIState.SUCCESS, f"{workflow} deployed {sha[:8]}")
        return CIStatus(CIState.FAILURE, f"{workflow} failed for {sha[:8]} ({conclusion})")

    async def pr_files(self, pr_number: int) -> list[str]:
        resp = await self._client.get(
            f"/repos/{self._repo}/pulls/{pr_number}/files", params={"per_page": 100}
        )
        resp.raise_for_status()
        return [f["filename"] for f in resp.json()]


@dataclass
class FakeGitHub:
    """In-memory stand-in for tests and for a Pi with no repo configured yet.
    Tests drive it by mutating `prs` / `ci` / poking `merged` directly rather
    than through a fluent builder - this is a fixture, not a product."""

    prs: dict[str, PullRequest] = field(default_factory=dict)  # branch -> PR
    ci: dict[str, CIStatus] = field(default_factory=dict)  # branch -> status
    merged: list[int] = field(default_factory=list)
    refuse_merge: str | None = None  # set to make `merge` refuse, evidence-testing
    merge_sha: str = "fake-sha"
    workflow_runs: dict[tuple[str, str], CIStatus | None] = field(default_factory=dict)
    files: dict[int, list[str]] = field(default_factory=dict)  # pr_number -> changed files

    async def find_pr_for_branch(self, branch: str) -> PullRequest | None:
        return self.prs.get(branch)

    async def ci_status(self, branch: str) -> CIStatus:
        return self.ci.get(branch, CIStatus(CIState.UNKNOWN, "no checks configured"))

    async def merge(self, pr_number: int) -> Outcome:
        if self.refuse_merge:
            return Refused(reason=self.refuse_merge)
        self.merged.append(pr_number)
        return Ok(value=self.merge_sha)

    async def workflow_run_status(self, workflow: str, sha: str) -> CIStatus | None:
        return self.workflow_runs.get((workflow, sha))

    async def pr_files(self, pr_number: int) -> list[str]:
        return self.files.get(pr_number, [])


def build_github(config: object) -> GitHub | None:
    """`config` is a `kestrel.config.Config`, typed loosely to avoid a cycle
    (same trick `board.build_board` uses).

    `None` - not a fake - when nothing is configured, because unlike Notion
    there is no dev-mode value in silently faking GitHub: every caller of a
    `None` client already treats "no GitHub configured" as "skip this"
    (no PR detection, no CI waits, no merge-on-green) rather than needing a
    board-shaped placeholder.
    """
    token = getattr(config, "github_token", None)
    repo = getattr(config, "github_repo", None)
    if not token or not repo:
        return None
    return GitHubRestClient(token=token, repo=repo)
