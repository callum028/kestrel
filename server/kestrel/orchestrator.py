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
from datetime import datetime

from .attention import AttentionState, Urgency
from .delivery import DeliveryTracker
from .devlock import DevLock
from .events import EventKind, EventLog
from .executors import Executor
from .supervision import Action, Budget, StallVerdict, detect, next_action
from .tasks import Task, TaskState, TaskStore


@dataclass
class TickReport:
    """What the pass did. Feeds the morning report, so a night with two nudges
    reads as a night with two nudges rather than as a clean run."""

    nudged: list[str] = field(default_factory=list)
    restarted: list[str] = field(default_factory=list)
    parked: list[str] = field(default_factory=list)
    escalated: list[str] = field(default_factory=list)
    stuck_deploy: str | None = None

    @property
    def quiet(self) -> bool:
        return not (self.nudged or self.restarted or self.parked or self.escalated)


class Orchestrator:
    def __init__(
        self,
        tasks: TaskStore,
        log: EventLog,
        deliveries: DeliveryTracker,
        dev_lock: DevLock,
        executors: dict[str, Executor],
        budget: Budget | None = None,
    ) -> None:
        self._tasks = tasks
        self._log = log
        self._deliveries = deliveries
        self._dev_lock = dev_lock
        self._executors = executors
        self._budget = budget or Budget()

    async def tick(self, now: datetime, attention: AttentionState) -> TickReport:
        report = TickReport()

        for task in self._tasks.active():
            if task.state is not TaskState.RUNNING:
                continue
            verdict = detect(task, self._log.for_task(task.id), now, self._budget)
            if verdict:
                await self._intervene(task, verdict, now, attention, report)

        self._check_dev_lock(now, attention, report)
        self._escalate_unread(now, report)
        return report

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

        action = next_action(task.nudges)

        if action in (Action.NUDGE, Action.NUDGE_HARDER):
            # The detector's evidence is the message. Specific beats polite:
            # "you have run gh run watch 40 times" moves it, "please continue"
            # does not.
            message = verdict.evidence
            if action is Action.NUDGE_HARDER:
                message = f"{verdict.evidence}\n\nThis is the second time. Change approach."
            await executor.send(task, message)
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
