"""The GitHub client - PR discovery, CI status, merge.

`GitHubRestClient` is tested against `httpx.MockTransport`, never the real
API, per the project's rule that tests never call real GitHub. `FakeGitHub`
(used everywhere else, e.g. test_orchestrator.py) is exercised directly too,
since it is what the rest of the codebase actually depends on.
"""

import httpx

from kestrel.github import (
    CIState,
    FakeGitHub,
    GitHubRestClient,
    build_github,
)


def _client(handler) -> GitHubRestClient:
    transport = httpx.MockTransport(handler)
    return GitHubRestClient(
        token="tok",
        repo="callum/kestrel",
        client=httpx.AsyncClient(base_url="https://api.github.com", transport=transport),
    )


async def test_find_pr_for_branch_returns_none_when_there_is_no_pr():
    def handler(request):
        assert request.url.params["head"] == "callum:kestrel/KES-1"
        return httpx.Response(200, json=[])

    client = _client(handler)
    assert await client.find_pr_for_branch("kestrel/KES-1") is None


async def test_find_pr_for_branch_returns_the_first_match():
    def handler(request):
        return httpx.Response(
            200,
            json=[
                {
                    "number": 42,
                    "html_url": "https://github.com/callum/kestrel/pull/42",
                    "state": "open",
                    "merged_at": None,
                }
            ],
        )

    client = _client(handler)
    pr = await client.find_pr_for_branch("kestrel/KES-1")
    assert pr is not None
    assert pr.number == 42
    assert pr.merged is False


async def test_ci_status_pending_while_a_check_is_incomplete():
    def handler(request):
        return httpx.Response(
            200,
            json={"check_runs": [{"name": "lint", "status": "in_progress"}]},
        )

    status = await _client(handler).ci_status("kestrel/KES-1")
    assert status.state is CIState.PENDING
    assert "lint" in status.summary


async def test_ci_status_failure_names_the_failing_check():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "check_runs": [
                    {"name": "test", "status": "completed", "conclusion": "success"},
                    {
                        "name": "lint",
                        "status": "completed",
                        "conclusion": "failure",
                        "output": {"summary": "2 errors"},
                    },
                ]
            },
        )

    status = await _client(handler).ci_status("kestrel/KES-1")
    assert status.state is CIState.FAILURE
    assert "lint" in status.summary
    assert "2 errors" in status.summary


async def test_ci_status_success_when_everything_passed():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "check_runs": [
                    {"name": "test", "status": "completed", "conclusion": "success"},
                    {"name": "lint", "status": "completed", "conclusion": "skipped"},
                ]
            },
        )

    status = await _client(handler).ci_status("kestrel/KES-1")
    assert status.state is CIState.SUCCESS


async def test_ci_status_unknown_when_nothing_has_reported():
    status = await _client(lambda r: httpx.Response(200, json={"check_runs": []})).ci_status("x")
    assert status.state is CIState.UNKNOWN


async def test_merge_ok_on_200():
    def handler(request):
        assert request.method == "PUT"
        return httpx.Response(200, json={"sha": "abc123"})

    outcome = await _client(handler).merge(42)
    assert outcome.status == "ok"


async def test_merge_refused_surfaces_githubs_message():
    def handler(request):
        return httpx.Response(405, json={"message": "not mergeable"})

    outcome = await _client(handler).merge(42)
    assert outcome.status == "refused"
    assert "not mergeable" in outcome.reason


async def test_fake_github_round_trips_what_tests_put_in():
    from kestrel.github import CIStatus, PullRequest

    gh = FakeGitHub()
    gh.prs["kestrel/KES-1"] = PullRequest(
        number=1, url="https://x/1", branch="kestrel/KES-1", state="open", merged=False
    )
    gh.ci["kestrel/KES-1"] = CIStatus(CIState.SUCCESS, "green")

    assert (await gh.find_pr_for_branch("kestrel/KES-1")).number == 1
    assert (await gh.ci_status("kestrel/KES-1")).state is CIState.SUCCESS
    assert (await gh.ci_status("unknown-branch")).state is CIState.UNKNOWN

    outcome = await gh.merge(1)
    assert outcome.status == "ok"
    assert gh.merged == [1]


async def test_fake_github_merge_can_be_made_to_refuse():
    gh = FakeGitHub(refuse_merge="branch protection requires a review")
    outcome = await gh.merge(1)
    assert outcome.status == "refused"
    assert gh.merged == []


class _Cfg:
    def __init__(self, token=None, repo=None):
        self.github_token = token
        self.github_repo = repo


def test_build_github_is_none_without_both_token_and_repo():
    assert build_github(_Cfg(token=None, repo=None)) is None
    assert build_github(_Cfg(token="t", repo=None)) is None
    assert build_github(_Cfg(token=None, repo="a/b")) is None


def test_build_github_returns_a_real_client_when_configured():
    client = build_github(_Cfg(token="t", repo="a/b"))
    assert isinstance(client, GitHubRestClient)
