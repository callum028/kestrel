"""The tick - where the modules become a system.

One pass over everything in flight. Deliberately boring and deterministic: it
reads state, applies the ladders, and acts. No model is consulted anywhere in
this file, which is the point - the thing supervising an unreliable agent must
not itself be unreliable.

The nine-hour overnight loss is the shape this was written against. Under this
loop it costs about fifteen minutes and one sentence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from kestrel_agent.worktrees import task_branch

from .attention import AttentionState, Urgency
from .board_sync import BoardSync
from .delivery import DeliveryTracker
from .devlock import DevLock
from .events import EventKind, EventLog
from .executors import Executor
from .github import CIState, GitHub
from .outcomes import Refused
from .supervision import (
    Action,
    Budget,
    Diff,
    StallReason,
    StallVerdict,
    detect,
    next_action,
    unrequested_spec_changes,
)
from .tasks import Task, TaskState, TaskStore
from .validation import (
    DeployCheck,
    HealthChecker,
    HttpHealthChecker,
    ProjectValidation,
    ValidationRunner,
)
from .waits import Wait, WaitKind, WaitStore


@dataclass
class TickReport:
    """What the pass did. Feeds the morning report, so a night with two nudges
    reads as a night with two nudges rather than as a clean run."""

    nudged: list[str] = field(default_factory=list)
    restarted: list[str] = field(default_factory=list)
    parked: list[str] = field(default_factory=list)
    escalated: list[str] = field(default_factory=list)
    held_back: list[str] = field(default_factory=list)
    woken: list[str] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)
    validated: list[str] = field(default_factory=list)  # passed validation -> DONE
    reopened: list[str] = field(default_factory=list)  # failed validation -> back to RUNNING
    stuck_deploy: str | None = None

    @property
    def quiet(self) -> bool:
        return not (
            self.nudged
            or self.restarted
            or self.parked
            or self.escalated
            or self.held_back
            or self.woken
            or self.merged
            or self.validated
            or self.reopened
        )


# Stall reasons that bypass the nudge ladder entirely and park immediately -
# a process that has died, or a session that never got past a startup prompt,
# is not going to respond to a nudge, so budgeting nudges against it would
# just delay the one thing that helps: moving on and reporting it.
_IMMEDIATE_PARK = {StallReason.PROCESS_DIED, StallReason.STARTUP_STUCK}


class Orchestrator:
    def __init__(
        self,
        tasks: TaskStore,
        log: EventLog,
        deliveries: DeliveryTracker,
        dev_lock: DevLock,
        executors: dict[str, Executor],
        budget: Budget | None = None,
        waits: WaitStore | None = None,
        github: GitHub | None = None,
        board_sync: BoardSync | None = None,
        validation: dict[str, ProjectValidation] | None = None,
        validation_runner: ValidationRunner | None = None,
        health_checker: HealthChecker | None = None,
    ) -> None:
        self._tasks = tasks
        self._log = log
        self._deliveries = deliveries
        self._dev_lock = dev_lock
        self._executors = executors
        self._budget = budget or Budget()
        self._waits = waits
        self._github = github
        self._board_sync = board_sync
        self._validation = validation or {}
        self._validation_runner = validation_runner or ValidationRunner()
        self._health_checker = health_checker

    def _health(self) -> HealthChecker:
        # Built lazily - most tests never configure a URL wait or deploy
        # check, so a real `httpx.AsyncClient` is never created for them.
        if self._health_checker is None:
            self._health_checker = HttpHealthChecker()
        return self._health_checker

    async def tick(self, now: datetime, attention: AttentionState) -> TickReport:
        report = TickReport()

        for task in self._tasks.active():
            if task.state is not TaskState.RUNNING:
                continue
            await self._track_progress(task)
            died = await self._check_alive(task)
            if died:
                await self._intervene(
                    task,
                    StallVerdict(True, StallReason.PROCESS_DIED, died),
                    now,
                    attention,
                    report,
                )
                continue
            has_wait = bool(self._waits and self._waits.active_for_task(task.id))
            verdict = detect(
                task, self._log.for_task(task.id), now, self._budget, has_pending_wait=has_wait
            )
            if verdict:
                await self._intervene(task, verdict, now, attention, report)

        await self._check_waits(now, attention, report)
        await self._check_landings(now, attention, report)
        await self._check_validations(now, attention, report)
        self._check_dev_lock(now, attention, report)
        self._escalate_unread(now, report)
        return report

    async def _track_progress(self, task: Task) -> None:
        """The progress proxy: a hash of the worktree diff, recomputed every
        tick for whichever executor kind knows what a worktree is. Executors
        without one (kestrel/human/phone) simply do not implement this, so
        `detect()` falls back to whatever `record_progress` was last told
        directly - unchanged from before this method existed."""
        executor = self._executors.get(task.executor)
        hasher = getattr(executor, "progress_hash", None)
        if hasher is None:
            return
        current = await hasher(task)
        if current is not None:
            self._tasks.record_progress(task.id, current)

    async def _check_alive(self, task: Task) -> str | None:
        """Non-None (the evidence) when the executor reports the session's
        process has ended without Kestrel having stopped it itself - a task
        still `RUNNING` with nothing behind it. Session process death is Stuck
        immediately, never a candidate for the nudge ladder."""
        executor = self._executors.get(task.executor)
        checker = getattr(executor, "alive", None)
        if checker is None:
            return None
        alive = await checker(task)
        if alive is False:
            return f"{task.handle}'s session process ended without being stopped - Stuck."
        return None

    async def _intervene(
        self,
        task: Task,
        verdict: StallVerdict,
        now: datetime,
        attention: AttentionState,
        report: TickReport,
    ) -> None:
        self._log.append(
            EventKind.STALL_DETECTED,
            "kestrel",
            {"reason": str(verdict.reason), "evidence": verdict.evidence},
            task_id=task.id,
        )
        executor = self._executors.get(task.executor)
        if executor is None:
            # Not a stall - a system fault. Reporting it as "parked after 0
            # nudges" would blame the agent for something Kestrel is missing,
            # and the fix is entirely different.
            await self._park(
                task,
                executor=None,
                now=now,
                attention=attention,
                report=report,
                reason=f"no {task.executor} executor registered",
                body=f"{task.handle} needs a {task.executor} executor and none is registered, "
                f"so it cannot be supervised. Parked.",
            )
            return

        if verdict.reason in _IMMEDIATE_PARK:
            await self._park(
                task,
                executor=executor,
                now=now,
                attention=attention,
                report=report,
                reason=str(verdict.reason),
                body=f"{task.handle}: {verdict.evidence}",
            )
            return

        action = next_action(task.nudges)

        if action in (Action.NUDGE, Action.NUDGE_HARDER):
            # The detector's evidence is the message. Specific beats polite:
            # "you have run gh run watch 40 times" moves it, "please continue"
            # does not.
            message = verdict.evidence
            if action is Action.NUDGE_HARDER:
                message = f"{verdict.evidence}\n\nThis is the second time. Change approach."
            outcome = await executor.send(task, message)
            if outcome.status != "ok":
                await self._on_undelivered_nudge(task, outcome, verdict, now, attention, report)
                return
            self._tasks.record_nudge(task.id, verdict.evidence)
            report.nudged.append(task.handle)
            return

        if action is Action.RESTART:
            await executor.stop(task, reason=str(verdict.reason))
            await executor.start(task, brief=task.goal)
            self._tasks.record_nudge(task.id, f"restart after {verdict.reason}")
            report.restarted.append(task.handle)
            return

        await self._park(
            task,
            executor=executor,
            now=now,
            attention=attention,
            report=report,
            reason=str(verdict.reason),
            body=f"{task.handle} stalled: {verdict.evidence} Parked after "
            f"{task.nudges} nudges and moved on.",
        )

    async def _on_undelivered_nudge(
        self,
        task: Task,
        outcome: Refused,
        verdict: StallVerdict,
        now: datetime,
        attention: AttentionState,
        report: TickReport,
    ) -> None:
        """Fixes the bug where a `Refused` from `executor.send()` was silently
        ignored: the nudge was never delivered, but `record_nudge` ran anyway,
        which both lied about what happened and burned a rung of the ladder
        for nothing. A send deferred because Callum is typing in the session
        is not a failure - it outranks the nudge - so it must not count
        against the budget either; it surfaces as a question instead, since
        he is right there to answer it himself."""
        reason = outcome.reason
        if "human is active" in reason or "deferred" in reason:
            self._log.append(
                EventKind.NUDGE_HELD_BACK,
                "kestrel",
                {"reason": reason, "would_have_sent": verdict.evidence},
                task_id=task.id,
            )
            self._deliveries.send(
                subject=f"{task.handle}: nudge held back",
                body=f"{task.handle} needs a nudge ({verdict.evidence}) but you're already in "
                f"the session. Want me to send it, or are you on it?",
                urgency=Urgency.NORMAL,
                state=attention,
                task_id=task.id,
                about_task=task.handle,
                now=now,
            )
            report.held_back.append(task.handle)
            return

        # Some other failure to deliver (no session, session not running).
        # Not claiming a nudge was sent when nothing happened.
        self._log.append(
            EventKind.TICK_FAILED,
            "kestrel",
            {"error": f"nudge to {task.handle} not delivered: {reason}"},
            task_id=task.id,
        )

    async def _park(
        self,
        task: Task,
        executor: Executor | None,
        now: datetime,
        attention: AttentionState,
        report: TickReport,
        reason: str,
        body: str,
    ) -> None:
        """Park and move on. Reclaiming the night matters more than asking
        permission, so this is never urgent - the system already handled it by
        continuing with something else."""
        self._tasks.transition(task.id, TaskState.PARKED, reason=reason)
        if executor is not None:
            await executor.stop(task, reason="parked")
        self._deliveries.send(
            subject=f"{task.handle} parked",
            body=body,
            urgency=Urgency.NORMAL,
            state=attention,
            task_id=task.id,
            about_task=task.handle,
            now=now,
        )
        report.parked.append(task.handle)

    # --- Kestrel-owned waits --------------------------------------------------

    async def _check_waits(
        self, now: datetime, attention: AttentionState, report: TickReport
    ) -> None:
        """Polls the real thing behind each outstanding wait. A wait that
        resolves wakes its session with a concise factual message; a wait
        whose deadline has passed gets one direct check and then escalates as
        Needs You with the facts - it never just keeps polling."""
        if self._waits is None:
            return
        for wait in self._waits.active():
            try:
                task = self._tasks.get(wait.task_id)
            except KeyError:
                continue  # the task is gone; nothing left to wake.
            if task.state is not TaskState.RUNNING:
                continue

            outcome = await self._poll_wait(wait)
            if outcome is not None:
                self._waits.resolve(wait.id, outcome)
                await self._wake(task, outcome, now, attention, report)
                continue

            if wait.due(now):
                final = await self._poll_wait(wait) or "still unresolved after the deadline"
                self._waits.expire(wait.id, final)
                self._tasks.transition(
                    task.id,
                    TaskState.NEEDS_INPUT,
                    reason=f"wait on {wait.kind} expired: {final}",
                )
                self._deliveries.send(
                    subject=f"{task.handle} needs you",
                    body=f"{task.handle} registered a {wait.kind} wait that never resolved: "
                    f"{final}. It has been waiting since {wait.created_at.isoformat()}.",
                    urgency=Urgency.NORMAL,
                    state=attention,
                    task_id=task.id,
                    about_task=task.handle,
                    now=now,
                )
                report.escalated.append(task.handle)

    async def _poll_wait(self, wait: Wait) -> str | None:
        """Returns a concise factual result once the wait has resolved, or
        `None` while it is still pending. This is the only place that knows
        how each `WaitKind` maps onto "the real thing"."""
        if wait.kind is WaitKind.DEADLINE:
            return None  # resolved by time alone - handled by `wait.due` above.

        if wait.kind is WaitKind.CI:
            if self._github is None:
                return None
            branch = wait.params.get("branch") or task_branch(self._tasks.get(wait.task_id).handle)
            status = await self._github.ci_status(branch)
            if status.state is CIState.PENDING or status.state is CIState.UNKNOWN:
                return None
            return status.describe()

        if wait.kind is WaitKind.URL:
            url = wait.params.get("url")
            if not url:
                return None
            expect_status = int(wait.params.get("expect", 200))
            match = wait.params.get("match")
            ok = await self._health().check(url, expect_status, match)
            if not ok:
                return None
            detail = f", matching {match!r}" if match else ""
            return f"{url} returned {expect_status}{detail}"

        return None

    async def _wake(
        self, task: Task, message: str, now: datetime, attention: AttentionState, report: TickReport
    ) -> None:
        executor = self._executors.get(task.executor)
        if executor is None:
            return
        outcome = await executor.send(task, message)
        if outcome.status != "ok":
            # Same rule as a held-back nudge: never silently drop it.
            self._deliveries.send(
                subject=f"{task.handle}: wait resolved",
                body=f"{task.handle}'s wait resolved ({message}) but I couldn't wake the "
                f"session ({outcome.reason}). Passing it along here instead.",
                urgency=Urgency.NORMAL,
                state=attention,
                task_id=task.id,
                about_task=task.handle,
                now=now,
            )
            return
        report.woken.append(task.handle)

    # --- landing: PR detection, CI gate, merge-on-green -----------------------

    async def _check_landings(
        self, now: datetime, attention: AttentionState, report: TickReport
    ) -> None:
        """Mechanical, like the board sync worker - no model decision. Two
        halves, matching design doc §5 step 6 ("merge is authorised on green
        CI"):

        - a `RUNNING` task whose branch now has a PR gets that PR recorded and
          queues for the dev lock (`AWAITING_DEV`) - "detecting the PR"
        - a task already queued that holds the dev lock gets merged once CI is
          green, and moves on to `VALIDATING`; if CI is red, the lock is
          released and the task is parked with the failure, since the session
          that could fix it has already ended its turn.
        """
        if self._github is None:
            return

        for task in self._tasks.active():
            if task.state is TaskState.RUNNING:
                await self._detect_pr(task)
            elif task.state is TaskState.AWAITING_DEV:
                await self._advance_landing(task, now, attention, report)

    async def _detect_pr(self, task: Task) -> None:
        assert self._github is not None
        pr = await self._github.find_pr_for_branch(task_branch(task.handle))
        if pr is None:
            return
        self._log.append(
            EventKind.PR_DETECTED,
            "kestrel",
            {"pr_number": pr.number, "url": pr.url},
            task_id=task.id,
        )
        self._tasks.transition(task.id, TaskState.AWAITING_DEV, reason=f"PR opened: {pr.url}")
        if self._board_sync is not None:
            await self._board_sync.record_pr_link(task.id, pr.url)

    async def _advance_landing(
        self, task: Task, now: datetime, attention: AttentionState, report: TickReport
    ) -> None:
        """The dev lock is irrelevant until CI is green - CI runs against the
        PR branch in isolation, not the shared dev target, so there is
        nothing to serialise yet. Only once CI passes does this task try to
        acquire the lock, merge, and hand off to `VALIDATING` holding it -
        "held from merge until validation completes or fails" (design §5).
        A task queued behind someone else's validation is not a stall; it
        just waits for next tick, same as `DevLock.acquire`'s own contract.
        """
        assert self._github is not None
        pr = await self._github.find_pr_for_branch(task_branch(task.handle))
        if pr is None:
            return  # PR closed/force-pushed away since detection; nothing to land.

        status = await self._github.ci_status(task_branch(task.handle))
        self._log.append(
            EventKind.CI_CHECKED,
            "kestrel",
            {"pr_number": pr.number, "state": str(status.state), "summary": status.summary},
            task_id=task.id,
        )

        if status.state is CIState.PENDING or status.state is CIState.UNKNOWN:
            return  # try again next tick.

        if status.state is CIState.FAILURE:
            self._tasks.transition(
                task.id, TaskState.PARKED, reason=f"CI failed before merge: {status.summary}"
            )
            self._deliveries.send(
                subject=f"{task.handle}: CI failed at landing",
                body=f"{task.handle}'s PR ({pr.url}) failed CI before it could merge: "
                f"{status.summary}. Parked for a follow-up.",
                urgency=Urgency.NORMAL,
                state=attention,
                task_id=task.id,
                about_task=task.handle,
                now=now,
            )
            report.parked.append(task.handle)
            return

        if not self._dev_lock.acquire(task.id, now):
            return  # CI is green but dev is busy validating another task - queues.

        outcome = await self._github.merge(pr.number)
        if outcome.status != "ok":
            self._dev_lock.release(task.id)
            self._tasks.transition(
                task.id, TaskState.PARKED, reason=f"merge refused: {outcome.reason}"
            )
            self._deliveries.send(
                subject=f"{task.handle}: merge refused",
                body=f"{task.handle}'s PR ({pr.url}) is green but the merge was refused: "
                f"{outcome.reason}. Parked for a follow-up.",
                urgency=Urgency.NORMAL,
                state=attention,
                task_id=task.id,
                about_task=task.handle,
                now=now,
            )
            report.parked.append(task.handle)
            return

        self._log.append(
            EventKind.PR_MERGED,
            "kestrel",
            {"pr_number": pr.number, "pr_url": pr.url, "sha": outcome.value},
            task_id=task.id,
        )
        # The lock stays held - released by `_check_validations` once deploy
        # confirmation and the authoritative test run finish (pass or fail).
        self._tasks.transition(task.id, TaskState.VALIDATING, reason=f"merged {pr.url}")
        report.merged.append(task.handle)
        await self._notify_rebase(task, now, attention)

    async def _notify_rebase(
        self, merged_task: Task, now: datetime, attention: AttentionState
    ) -> None:
        """Anything still in flight on the same repo has to rebase and
        revalidate after each landing, because dev has moved (design §5's
        "the dev environment is a singleton"). Best-effort and silent on
        failure to deliver - a missed rebase note is not worth an escalation,
        the next PR push will surface any conflict anyway."""
        executor = self._executors.get(merged_task.executor)
        project_of = getattr(executor, "project_for", None)
        this_project = project_of(merged_task) if project_of else None
        message = (
            f"{merged_task.handle} just merged to the default branch - rebase before "
            f"opening or updating your PR."
        )
        for other in self._tasks.active():
            if other.id == merged_task.id or other.state is not TaskState.RUNNING:
                continue
            other_executor = self._executors.get(other.executor)
            if other_executor is None:
                continue
            # Without a way to name the project (this executor kind has no
            # worktree at all, e.g. `human`/`phone`), the safe default is the
            # common single-project case: treat "same executor" as "same
            # repo", matching `ClaudeCodeExecutor._project_for`'s own
            # single-project fallback.
            other_project_of = getattr(other_executor, "project_for", None)
            other_project = other_project_of(other) if other_project_of else None
            if (
                this_project is not None
                and other_project is not None
                and other_project != this_project
            ):
                continue
            await other_executor.send(other, message)

    # --- validation: deploy confirmation + Kestrel's own authoritative run ----

    def _landing_info(self, task: Task) -> tuple[int, str, str] | None:
        """(pr_number, pr_url, merge_sha) from this task's own `PR_MERGED`
        event - the event log is the source of truth, so nothing extra needs
        to be threaded onto `Task` itself to remember what was merged."""
        for event in reversed(self._log.for_task(task.id)):
            if event.kind is EventKind.PR_MERGED:
                return event.payload["pr_number"], event.payload["pr_url"], event.payload["sha"]
        return None

    def _validation_config_for(self, task: Task) -> ProjectValidation | None:
        if not self._validation:
            return None
        executor = self._executors.get(task.executor)
        project_of = getattr(executor, "project_for", None)
        project = project_of(task) if project_of else None
        if project is not None:
            return self._validation.get(project)
        if len(self._validation) == 1:
            return next(iter(self._validation.values()))
        return None

    async def _check_validations(
        self, now: datetime, attention: AttentionState, report: TickReport
    ) -> None:
        if self._github is None:
            return
        for task in self._tasks.active():
            if task.state is TaskState.VALIDATING:
                await self._advance_validation(task, now, attention, report)

    async def _advance_validation(
        self, task: Task, now: datetime, attention: AttentionState, report: TickReport
    ) -> None:
        landing = self._landing_info(task)
        if landing is None:
            # Should not happen - VALIDATING is only ever reached right after
            # recording this event - but a task stuck here with nothing to
            # validate against is worth surfacing rather than looping forever.
            self._dev_lock.release(task.id)
            self._tasks.transition(
                task.id, TaskState.PARKED, reason="validating with no recorded merge"
            )
            report.parked.append(task.handle)
            return
        pr_number, _pr_url, merge_sha = landing

        config = self._validation_config_for(task)
        if config is None or not config.configured:
            await self._pass_validation(
                task,
                now,
                attention,
                report,
                note="no validation configured — merged and deployed, not tested",
            )
            return

        if config.deploy.configured:
            status = await self._deploy_status(config.deploy, merge_sha)
            if status == "pending":
                # Timed off the dev lock's own `acquired_at`, not the task
                # record's `updated_at` - the lock is acquired with the
                # tick's own `now` (see `_advance_landing`), so this stays
                # correct under a frozen test clock, unlike a wall-clock
                # timestamp stamped by the task/event store.
                hold = self._dev_lock.holder()
                held_for = (
                    now - hold.acquired_at if hold and hold.task_id == task.id else timedelta(0)
                )
                if held_for < config.deploy.timeout:
                    return  # try again next tick
                status = "failed"  # deploy deadline passed with nothing confirmed
            if status == "failed":
                minutes = int(config.deploy.timeout.total_seconds() // 60)
                await self._reopen_or_park(
                    task,
                    now,
                    attention,
                    report,
                    reason=f"dev deploy of {merge_sha[:8]} did not confirm",
                    message=f"{task.handle}: the dev deploy for {merge_sha[:8]} did not go "
                    f"green within {minutes} minutes (or the deploy check failed). The dev "
                    f"lock has been released - check the deploy and push again once it's "
                    f"fixed.",
                )
                return

        assert config.command is not None
        executor = self._executors.get(task.executor)
        prepare = getattr(executor, "prepare_validation", None) if executor else None
        worktree = await prepare(task, merge_sha) if prepare else None
        if worktree is None:
            await self._reopen_or_park(
                task,
                now,
                attention,
                report,
                reason="validation: no worktree available to run against",
                message=f"{task.handle}: merged and deployed, but there was no worktree to run "
                f"validation in. The dev lock has been released.",
            )
            return

        try:
            outcome = await self._validation_runner.run(config.command, worktree, config.dev_url)
        except Exception as exc:  # noqa: BLE001 - never let a bad run wedge the lock
            self._dev_lock.release(task.id)
            self._tasks.transition(task.id, TaskState.PARKED, reason=f"validation crashed: {exc}")
            self._deliveries.send(
                subject=f"{task.handle}: validation crashed",
                body=f"{task.handle}'s validation run raised {type(exc).__name__}: {exc}. "
                f"Dev lock released; parked for a follow-up.",
                urgency=Urgency.NORMAL,
                state=attention,
                task_id=task.id,
                about_task=task.handle,
                now=now,
            )
            report.parked.append(task.handle)
            return

        spec_flag = await self._flag_unrequested_spec_changes(task, pr_number)

        self._log.append(
            EventKind.VALIDATION_RUN,
            "kestrel",
            {"ok": outcome.ok, "summary": outcome.summary, "failing_tests": outcome.failing_tests},
            task_id=task.id,
        )

        if outcome.ok:
            note = outcome.summary
            if spec_flag:
                note += (
                    f"; touched test/spec files the brief didn't ask for: {', '.join(spec_flag)}"
                )
            await self._pass_validation(task, now, attention, report, note=note)
            return

        evidence = f"{task.handle}: validation on dev failed - {outcome.summary}."
        if outcome.failing_tests:
            evidence += f" Failing: {', '.join(outcome.failing_tests[:10])}."
        if outcome.output_tail:
            evidence += f"\n\n{outcome.output_tail}"
        if spec_flag:
            evidence += (
                f"\n\nAlso touched test/spec files the brief didn't ask for: "
                f"{', '.join(spec_flag)}."
            )
        await self._reopen_or_park(
            task,
            now,
            attention,
            report,
            reason=f"validation failed: {outcome.summary}",
            message=evidence,
        )

    async def _deploy_status(self, deploy: DeployCheck, merge_sha: str) -> str:
        """`"ready"`, `"pending"`, or `"failed"` - the workflow run is checked
        first when configured (more precise: a still-stale health URL happily
        returns 200), the URL is a fallback or the sole signal when that is
        all a project configures."""
        if deploy.workflow:
            assert self._github is not None
            status = await self._github.workflow_run_status(deploy.workflow, merge_sha)
            if status is not None:
                if status.state is CIState.SUCCESS:
                    return "ready"
                if status.state is CIState.FAILURE:
                    return "failed"
        if deploy.url:
            ok = await self._health().check(deploy.url, 200, deploy.url_match)
            if ok:
                return "ready"
        return "pending"

    async def _flag_unrequested_spec_changes(self, task: Task, pr_number: int) -> list[str]:
        assert self._github is not None
        files = await self._github.pr_files(pr_number)
        diff = Diff(added=[], removed=[], files=files)
        result = unrequested_spec_changes(diff, task.criteria)
        return list(result.candidates) if result.status == "ambiguous" else []

    async def _pass_validation(
        self, task: Task, now: datetime, attention: AttentionState, report: TickReport, note: str
    ) -> None:
        self._dev_lock.release(task.id)
        self._tasks.transition(task.id, TaskState.DONE, reason=note)
        self._deliveries.send(
            subject=f"{task.handle} finished",
            body=f"{task.handle} finished: {note}.",
            urgency=Urgency.NORMAL,
            state=attention,
            task_id=task.id,
            about_task=task.handle,
            now=now,
        )
        report.validated.append(task.handle)

    async def _reopen_or_park(
        self,
        task: Task,
        now: datetime,
        attention: AttentionState,
        report: TickReport,
        reason: str,
        message: str,
    ) -> None:
        """Every exit from validation releases the lock first - pass, fail,
        timeout, or a crash in this method's own caller. A session still
        alive gets the evidence and goes back to `RUNNING` to fix forward
        (design §5: "it does not revert"); a session that is gone gets
        parked, same as any other stall with nobody left to nudge."""
        self._dev_lock.release(task.id)
        executor = self._executors.get(task.executor)
        alive: bool | None = None
        if executor is not None:
            checker = getattr(executor, "alive", None)
            if checker is not None:
                alive = await checker(task)

        if executor is None or alive is not True:
            self._tasks.transition(task.id, TaskState.PARKED, reason=reason)
            if executor is not None:
                await executor.stop(task, reason="parked")
            self._deliveries.send(
                subject=f"{task.handle} parked",
                body=message,
                urgency=Urgency.NORMAL,
                state=attention,
                task_id=task.id,
                about_task=task.handle,
                now=now,
            )
            report.parked.append(task.handle)
            return

        self._tasks.transition(task.id, TaskState.RUNNING, reason=reason)
        outcome = await executor.send(task, message)
        if outcome.status != "ok":
            self._tasks.transition(
                task.id, TaskState.PARKED, reason=f"{reason}; session unreachable"
            )
            self._deliveries.send(
                subject=f"{task.handle} parked",
                body=f"{message}\n\n(Also could not reach the session to say so directly: "
                f"{outcome.reason}.)",
                urgency=Urgency.NORMAL,
                state=attention,
                task_id=task.id,
                about_task=task.handle,
                now=now,
            )
            report.parked.append(task.handle)
            return

        self._deliveries.send(
            subject=f"{task.handle} reopened",
            body=message,
            urgency=Urgency.NORMAL,
            state=attention,
            task_id=task.id,
            about_task=task.handle,
            now=now,
        )
        report.reopened.append(task.handle)

    def _check_dev_lock(self, now: datetime, attention: AttentionState, report: TickReport) -> None:
        stuck = self._dev_lock.stuck(now)
        if stuck is None:
            return
        report.stuck_deploy = stuck.task_id
        waiting = self._dev_lock.queue()
        self._deliveries.send(
            subject="dev deploy stuck",
            body=f"Dev has been held for {int(stuck.held_for(now).total_seconds() // 60)} minutes "
            f"by {stuck.task_id}, with {len(waiting)} task(s) queued behind it.",
            urgency=Urgency.NORMAL,
            state=attention,
            now=now,
        )

    def _escalate_unread(self, now: datetime, report: TickReport) -> None:
        for delivery in self._deliveries.due_for_escalation(now):
            if self._deliveries.escalate(delivery.id, now) is not None:
                report.escalated.append(delivery.id)
