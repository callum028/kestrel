# Kestrel — System Design

Canonical design document. Holds **decisions, contracts, and boundaries** — not implementation
detail. If a statement here could be invalidated by a refactor, it is written at the wrong altitude
and should be moved into code.

Supersedes the v1 design (`callum028/kestrel`, archived). See [Superseded decisions](#superseded-decisions).

---

## 1. What Kestrel is

Kestrel is an always-on personal assistant built around a **capability host**. Its first and most
valuable capability is supervising Claude Code, but that is not its identity. The identity is a
persistent thing that knows Callum, knows his work, holds state across time, and can reach him or be
reached on whatever surface he is near.

It is **not** a coding agent — Claude Code is. It is **not** a voice assistant — voice is one output
channel. It is **not** a notifier — alerting is what it does when it has failed to deliver.

**The unique property it has that Claude Code cannot:** it exists when Callum is not at the keyboard.
Work progresses in his absence. Everything else is friction-shaving.

### What earns its keep

1. Ambiguity surfaced at minute one instead of hour two
2. Validation catching false completion ("done" when it isn't)
3. Continuity — it knows what happened yesterday without being told
4. Persistence — a stalled agent gets pushed, not left for nine hours

---

## 2. Governing principles

These are load-bearing. Most design questions resolve by applying one of them.

**Bounded autonomy — attention is unbounded, action is bounded.** Inside the scope given, it acts
freely and proactively. To leave that scope, it asks. It may decide *how*; it may never decide
*what*.

But staying in scope is a constraint on *acting*, not on *looking*. It should actively scan outside
the brief for what has not been considered — adjacent bugs, dead code, a convention broken elsewhere,
a missing test, a risk nobody named. Noticing is the job; acting on what it notices is a separate
decision that belongs to Callum.

The failure mode this guards against is the tempting one: fixing the small thing while you are
already in there. A change that does more than its ticket is harder to review, harder to revert, and
harder to reason about later. **Notice, record, propose — never quietly do.**

**Every instruction ends in a state.** Done, queued with the condition that releases it, blocked with
a reason, or a question. Silence is never an outcome. This applies at every layer — conversation,
tool results, task lifecycle.

**Nothing enters context by accumulation.** Every token in a prompt is static, computed, or retrieved.
If something grows on its own, it is a bug.

**Anything a model is notorious for forgetting is not a model's job.** Board updates, time
arithmetic, validation, state transitions — all deterministic side-effects in code.

**Claims are validated, not trusted.** "Done" is a claim. "Blocked" is a claim. Both get checked.

**Claude supplies the capability; Kestrel supplies the persistence.** Most intervention is one
well-timed sentence, not a restart.

**Failures must be legible.** For a single-user system, a visible failure beats an invisible
recovery. Silent wrongness is the worst outcome available.

---

## 3. Topology

| | Always-on host (Pi) | PC | Phone / browser |
|---|---|---|---|
| Role | The system | Executor (opt-in) | View |
| Always on | Yes | No — on or off, deliberately | Yes |
| Durable state | All of it | None | None |
| Networking | Tailscale + Funnel ingress | Windows host owns the tailnet link | Tailscale + FCM |

### Always-on host — owns everything durable

- Event log (SQLite, WAL) — the spine; all state is a projection of it
- Task records, decisions, curated memory, the single conversation
- Attention state (presence + focus), computed
- Scheduler and durable jobs
- Channel router — deterministic; decides surface and modality
- Model interface
- Piper TTS — one voice for every surface
- Notion and GitHub integration
- Webhook ingress via Tailscale Funnel
- FCM sender

It never builds, tests, or executes code, and never holds a session transcript in context.

### PC — two halves

**Windows side:** the web UI, plus a host process that owns the Tailscale connection, spawns the WSL
agent over stdio, and provides tray, OS notifications, window focus, and idle time. This is the only
side with networking.

**WSL side:** the agent — git worktrees, Claude Code sessions, validation commands. Communicates
over stdin/stdout only (the VS Code Remote-WSL model), so WSL's NAT never enters the picture. Holds
no durable state.

The PC is either on or off. There is no Wake-on-LAN. If it is off, work queues and Kestrel says so.

### Clients are views

The always-on host owns the conversation. No client holds history, and there is no sync or merge.
Speaking to the phone and reading the reply on the desktop is a consequence of this, not a feature
that needs building.

---

## 4. Core substrate

### 4.1 Event log

Append-only, in SQLite. Everything is already events — hooks, focus changes, presence, task
transitions, CI results, nudges. State is a projection. Replay, debugging, "what happened yesterday",
and the UI's "why did it do that" view all fall out of it for free.

### 4.2 Tasks and executors

A **task** is a unit of delegated work: goal, acceptance criteria, scope bounds, escalation policy,
state, and a pointer to an executor. The lifecycle, escalation, the how/what boundary, and
record-writing live at this layer and are written once.

The **executor** interface is small: take a brief, emit events, accept input, report needs-input /
done / failed.

| Executor | Used by |
|---|---|
| Claude Code | Code supervision |
| Kestrel (model + tools) | Portfolio awareness, calendar/email, infra admin |
| Human | Commitments and reminders |
| Phone | Phone actions |

Four executor kinds is what makes this abstraction real rather than Claude-Code-shaped with
decoration.

### 4.3 Attention (presence + focus)

One subsystem, no model in it.

**Inputs:** desktop heartbeat and idle time, focus object from the active client, phone on the
tailnet, open voice session, audio route, calendar busy state.

**Output:** one computed struct, consumed by both the prompt assembler *and* the channel router.

The focus object identifies what is on screen — e.g. `KES-31 / diff / src/auth/token.ts:40-72` — so
"why did it do that?" resolves without an antecedent. It works in both directions: Kestrel can also
drive the view.

This is the useful half of the v1 screen-vision idea, done structurally: exact rather than
approximate, thirty lines of client code, no vision model.

### 4.4 Memory

Two halves, split by where truth lives:

**Derived** — tickets, PRs, code, Notion decisions. Changes without telling you. Indexed, never
copied. Read fresh.

**Durable** — personal facts, preferences, conventions, decisions whose reasoning exists nowhere
else. Stored as markdown in a git repo on the Pi: one fact per file, every change a commit, editable
in any editor, greppable.

**Written by exactly four sources:** explicit instruction, correction, a *what*-decision made during
task work, or a proxy answer. **Never by inference over conversation.** Every durable memory has a
visible moment of creation — if something was written, Callum saw it happen.

**Entry shape:** the fact, category, created-at, source (with a link back to the task or turn), and
scope (global, or one project). Scope matters: a convention leaking between repos is hard to trace.

**Contradiction:** never keep both. Ask which wins, mark the old superseded with a pointer, and only
the live one enters context.

**Expiry: none, automatically.** Auto-expiry deletes true things. Instead, memories announce
themselves when used — "using exponential backoff, per your call on KES-31" — so wrong ones are
caught in flight. Plus a review surface listing anything unused in 90 days.

### 4.5 Context assembly

A **pure function**, callable and dumpable to a file, so two turns can be diffed. Context problems
are invisible when the prompt is assembled implicitly and trivial when it is not.

| Layer | Source | Rough size |
|---|---|---|
| Identity | Hand-written, versioned | ~2k tokens |
| Durable memory core | Curated, hard-capped | ~500 |
| Volatile state | Computed | ~300 |
| Conversation | Last N turns + rolling summary | Bounded |
| Retrieved memory | Automatic semantic + entity match | Variable |
| Everything else | Tools, on demand | 0 baseline |

**Retrieval is automatic, not tool-triggered.** Every turn: embed the incoming message, pull matches
above a threshold, plus a keyword pass for named entities. The model never has to decide to look,
because it does not know what it does not know. Embeddings run locally; cost is zero.

**Volatile state never enters conversation history.** History is immutable and timeless; "I'm heading
out" reads as equally true three hours later. Volatile facts go in a block regenerated every turn
with the arithmetic already done:

```
now: 2026-08-20 14:38
presence: AWAY (stated 47m ago; PC heartbeat silent 45m)
KES-31: running 22m, last output 4m ago, 2 unanswered questions
focus: none (app closed)
```

The model reads facts. It never subtracts timestamps.

The "too little context" failure is not missing content — it is missing **pointers**. The state block
carries an index (task handles, project names, activity headlines) and the handles are the exact
arguments the tools take, so retrieval is one hop rather than a search.

### 4.6 Channels and modality

Modality is a property of the channel, not a decision the model makes.

| Attention | Non-urgent | Blocking |
|---|---|---|
| App focused | Inline, silent | Inline |
| Unfocused, recent input | Desktop notification | Desktop notification |
| Unfocused, idle | Phone notification | Phone notification |
| App closed / PC off | Phone notification | **Call** |

**Escalate on silence, never fan out.** Duplicate notifications train you to ignore both. If
unacknowledged within a couple of minutes, it moves up a rung. Acknowledging anywhere dismisses
everywhere.

**Content differs by channel, not just rendering.** Voice gets two sentences and stops; the full
version lands in the desktop chat simultaneously. Never read out code, diffs, or errors.

**Kestrel speaks; Claude Code never does.** Speech stays a signal because most keyboard time is not
addressed to Kestrel. Hard suppression during calls and meetings, plus a mute toggle.

### 4.7 Model interface

One interface, ~50 lines. Not a gateway. Its value is having one place to change providers when a
free tier disappears — which it will.

Routed by class of call, not by preferred provider:

| Class | Provider |
|---|---|
| Conversation | Groq / Gemini Flash free tier |
| Routing, classification, summarising | Cheapest available |
| Judgement | Claude Code headless, on the existing subscription |

Judgement calls only arise while work is running, and work only runs when the PC is on — which is
exactly when Claude Code headless is available. Cap those calls so a supervisor cannot exhaust the
limits real work needs.

Local inference is **not** a planned capability. Both machines are AMD; it was never going to pay off.
The interface leaves the door open if that changes.

### 4.8 Identity layer

Hand-written, version-controlled, injected into **every** model call regardless of tier. The single
biggest cause of personality drift is a cheap routing model answering without it.

Defined by worked examples, not adjectives — "dry and understated" is unfalsifiable; twenty sample
exchanges pin it exactly.

Rules, roughly as they appear:

- **Stance.** You run Callum's work with him, not for him. Peer-level. Never explain fundamentals,
  never pad, never praise the question.
- **Every instruction ends in a state.** If you cannot do something, say so immediately and propose
  the nearest thing you can do.
- **Decide *how*, ask *what*.** Naming, structure, approach, test strategy — decide and note it.
  Scope, completion, doing more than was asked, anything visible to other people — ask.
- **Never guess about anything retrievable.** Look it up or say you do not know.
- **Voice replies are two sentences.** The full version goes to the desktop chat.
- **Interrupt in proportion to where he is.** At the desk, ask freely. Away, only what cannot wait.
  Ringing him is for work that has stopped dead.
- **Speak first when you have something.** Finished work, a stalled task, a ticket untouched for two
  weeks, an unanswered review comment. Not to check in.
- **When corrected, record it and move on.** Say what was stored. No apologising, no restating.
- **Report failure plainly.** What broke, what was tried, what is needed.
- **Do not perform.** No enthusiasm, no character voice, no catchphrases. Presence comes from
  remembering, noticing, and following through.

---

## 5. Task lifecycle

1. **Intake.** "Pick up KES-31." Kestrel resolves the handle, reads back scope and acceptance
   criteria, and asks about anything ambiguous **now**, while Callum is present. Front-loading
   ambiguity is most of what makes unattended work survivable.
2. **Brief.** Ticket body, acceptance criteria, conventions, relevant past decisions, repo and target
   branch. Three words in, a full brief out. **The brief differs by presence:** unattended, Claude is
   told to decide *how* questions, document the choice, and keep going.
3. **Execution.** The agent creates a worktree on a task branch and starts a session. PTY plus hooks
   when watched; Agent SDK when unattended.
4. **Supervision.** See §6.
5. **Questions.** *How* answered from conventions and logged; *what* escalated per the channel
   ladder. A parked task never blocks others.
6. **Landing.** A PR opens. **Merge is authorised on green CI** — there is no localhost, so merging
   and deploying to dev is how the work becomes testable at all. Merge is therefore part of the test
   loop, not the end of it, and is not treated as a *what* decision.
7. **Validation.** Playwright against the deployed dev environment, after merge. Green CI is
   necessary but never sufficient; it gates the merge, it does not mean the work is done.
   Non-executable criteria escalate to Callum; that is a legitimate escalation, not a gap.

   Claude runs Playwright while working — that is how it iterates, and it should. But **Kestrel's own
   run is the authoritative one**: same command, triggered by the agent outside the session, exit
   code and report artefact read directly rather than taken from Claude's summary. It costs one
   command and preserves the independence that makes validation worth anything.

   The failure being guarded against is not a fabricated pass. It is the ordinary ones: the suite not
   run, a subset run, a run that errored reported as green — and most commonly, **the test edited
   until it passes instead of the code being fixed**. So the diff is checked for changes to spec
   files, and any that the acceptance criteria did not call for are flagged.

   A failed validation on dev reopens the task with the evidence attached. It does not revert — dev
   is the testing ground, and a follow-up fix is the normal path.
8. **Closure.** Task record written: goal, criteria, decisions, diff, outcome. Notion updated by the
   lifecycle. Only genuinely durable learnings are promoted to memory. Transcript archived and
   searchable; never in context.
9. **Report.** Next time Callum is present, briefly.

### The dev environment is a singleton

Validation runs against a shared deployed environment, which makes it a **lock**, not just a step.
Two tasks cannot validate at once — the second would be testing the first one's code.

Consequences:

- Tasks run concurrently in isolated worktrees, but serialise at merge *and stay serialised through
  deploy and validation*. The lock is held from merge until validation completes or fails.
- A task waiting on the lock is not blocked and does not escalate. It queues.
- Anything still in flight rebases and revalidates after each landing, because dev has moved.
- A stuck deploy holds the lock. It needs its own timeout, and a held lock past that timeout is worth
  reporting — it stalls every other task behind it.

This is the real cap on useful concurrency, well before subscription rate limits.

---

## 6. Supervision

The reason the system exists. All detection is deterministic — no model judgement, so none of it
inherits the unreliability it is supervising away.

### Stall detection

Activity is not progress; a polling loop produces plenty of activity.

- **Progress proxy:** hash of the worktree diff. Active session, unchanged hash for ~15 minutes ⇒ not
  progressing.
- **Repetition detector:** same tool, same arguments, k times running, over the event log.
- **Budgets:** wall-clock cap and turn cap per task.

### Intervention ladder

Killing is the last rung, not the first.

1. **Nudge, evidence-based.** The detector's own output is the prompt: *"you've run `gh run watch`
   forty times in twelve minutes with no change — the subagent isn't going to report back, stop
   waiting and continue."* Far more effective than "please continue", and it costs one message.
2. **Nudge harder,** with more specific instruction.
3. **Restart from the brief.** The worktree persists; nothing is lost.
4. **Park and move to the next task.**

Budget the nudges — three per stall, then escalate a rung — or the nudge loop becomes its own
overnight failure. Every nudge is logged, so the morning report reads "nudged twice on KES-32,
recovered" rather than looking like a clean run.

Reclaiming the night matters more than asking permission. Waking to four done and one parked beats
one done and nine hours burned.

### Claim validation

"Blocked" is a claim like "done" is a claim.

- A reported blocker must **name** the thing. Kestrel checks it against the repo and ticket before
  accepting. A session deleting code for the dependency it claims to be waiting on is caught by
  cross-referencing the stated blocker against its own diff.
- Grep the session's diff for `TODO`, `waiting on`, `blocked`, `disabled` markers it introduced, and
  surface them.

### Observations

The other half of supervision. While a task runs, Kestrel looks past the brief for what was not
considered, and records what it finds as **observations** against the task — never as changes.

Sources are the same material it already has: the session diff, the files touched and their
neighbours, test coverage of the changed paths, conventions in the durable memory, the ticket's own
history.

An observation is a first-class record — what, where, why it matters, and which task surfaced it —
and it has exactly four fates: **fixed in place** (see below), promoted to a ticket (an ask), folded
into the current task as real work (an ask), or dismissed. Dismissal is durable: the same observation
is not raised twice unless the underlying facts change.

#### Fixing in place

Trivial fixes may be made without asking. The justification is *not* PR review — PRs merge
automatically on green CI, so a PR is not a reliable human gate. It rests on the bounds below being
tight enough that nothing behavioural can reach dev, and on disclosure being loud enough that an
unrequested change is never discovered by accident.

That is a weaker gate than review, which is why the bounds are absolute rather than a judgement call.

"Trivial" is defined by bounds, not judgement. **All** must hold:

- Confined to files the task already touches
- No behaviour change — typos, dead imports, stale comments, formatting, obviously wrong docstrings
- Small enough to read at a glance
- No new dependencies; no change to public API, config, schema, or test expectations
- Validation still passes afterwards
- At most three per task — three unrelated small fixes is scope creep however small each one is

Anything failing any bound is an observation, and observations ask.

Disclosure is mandatory and goes three places: a line in the PR description, a comment on the Notion
ticket, and the next report. The PR is the one that matters — it is where the change is actually
reviewable.

They surface through the proactive channel, queued if outside the current mode window. They are never
urgent — an observation has never earned a phone call.

This is the mechanism behind wanting Kestrel to be strategic as well as operational. Without it,
"proactive" degrades into reporting task status.

### Board and PR drift

Split by whether Kestrel already knows the fact for certain.

**Mechanical — code, as lifecycle side-effects.** In Progress, In Review, PR link, CI status. These
cannot be forgotten because no model is involved. Claude never touches the board.

**Factual content — delegated to the agent with evidence.** Ticket bodies describing behaviour that
no longer exists, criteria referencing a deprecated flag, stale PR descriptions. Kestrel detects,
the agent writes the correction.

Detection:

- **Symbol existence** — identifiers, flags, and paths mentioned in ticket and PR bodies checked
  against the repo
- **Scope drift** — files touched versus what the ticket claims to be about
- **Staleness** — In Progress with no activity, open PR with no commits, Done with an open PR
- **Change events** — GitHub webhooks via Funnel; Notion polled on `last_edited_time`

Marking a ticket Done still asks. Kestrel's writes are attributed to its integration identity.

---

## 7. The desk experience

Kestrel is where work happens, not a layer around it.

**Asymmetric: it interposes on input, never filters output.** "Pick up KES-31" is three words
expanding into a full brief — compression, not a hop. The session streams raw to the UI, and the
terminal can be typed into directly at any time.

**The session lives in the WSL agent, not the UI.** Consequences: closing the app does not kill it,
the phone can attach to the same session, Kestrel injects relayed answers into the *same* PTY (one
session, not a parallel channel), and recording comes free. Input arbitration needs a write lock so
Kestrel cannot inject mid-keystroke.

**The terminal is first-class, not a session viewer.** Callum uses VS Code almost solely for its
terminal, so Kestrel's has to be good enough to replace it or he will keep both open and Kestrel
becomes the second window. That means the agent hosts *N* PTYs rather than one per task:

- **task terminals**, bound to a task and its worktree, which Kestrel can also write into
- **free terminals**, just a shell in a project directory, which Kestrel never touches

Both are the same primitive. The difference is only whether a task owns it. Tabs, scrollback,
colours, resize, copy/paste and a remembered working directory are all requirements, not polish —
a terminal that is 90% as good as the one he has is a terminal he will not use.

File editing stays outside: the diff pane covers review, and an editor covers the rest. GitHub and
Notion panes are worth having and are explicitly v2.

**The session never waits.** Get up for coffee, Claude asks a question, it routes to the phone,
answer in a sentence, work continues. Callum stops being the bottleneck for a session at his own
desk.

**Shared focus** makes it a collaborator rather than a chat box next to a terminal.

**No mic on the desktop** — type a line, hear the answer. Speech in comes from the phone if wanted,
and the reply renders as text on the desktop because there is one conversation.

---

## 8. Activation

No wake word anywhere. Nothing holds the microphone open in the background. Wake-word capture damage
— clipped starts, early VAD cutoff — was the root cause of most of v1's "voice doesn't work".

| Context | Trigger |
|---|---|
| Desk | Type. No activation problem. |
| Driving | Aux/USB-C adapter attach opens a session; continuous listening inside a bounded window is fine |
| On the ice | Knock code — pause-play-pause on the one AirPods gesture, observed via `MediaSessionManager`. Non-destructive; no session hijack |
| Phone in pocket | Volume-key chord via `AccessibilityService` |
| Phone in hand | App button (tap = one exchange, hold = open session), QS tile, notification action |
| Anywhere | Kestrel initiates — the phone rings |

Escalation calls use a self-managed `ConnectionService`: real full-screen call UI over the lock
screen, audio on the call route, and the AirPods answer gesture works because the OS believes it is a
call. VoIP over the tailnet, so no carrier and no per-minute cost.

---

## 9. Capabilities and build order

| Capability | Executor | Order |
|---|---|---|
| Code supervision | Claude Code | 1 |
| Commitments and reminders | Human | 2 |
| Portfolio awareness + briefing | Kestrel | 3 |
| Calendar and email | Kestrel | 4 (gated on Graph consent) |
| Infra admin | Kestrel | 5 |
| Phone actions | Phone | 6 |

The spec covers all six and the core is designed against all six. Only the order is staged, because
**the executor seam is only proven by the second capability**. Commitments are second precisely
because the human executor is the shape most unlike Claude Code.

Phone actions are last deliberately: it is where v1 failed, it is the least valuable of the six, and
rebuilding it on the discriminated-result contract only makes sense once that contract is proven.

Proactive output is gated by a **derived** mode — active task, agent connected, working hours, at the
desk — with manual override. A mode you have to remember to set is a mode you will forget. It gates
what Kestrel *volunteers*, never what it can do, and monitoring keeps running regardless. Findings
outside the window queue rather than drop.

---

## 10. Infrastructure

**Always-on host:** Pi 4B (8GB), moved to USB SSD boot. Currently on SD (`/dev/mmcblk0p2`) — a
continuous-write workload on an SD card is the classic way a Pi home server dies silently. Upgrade
path to an x86 mini PC is a file copy, not a redesign; the trigger for moving is Piper latency.

**Repo:** one GitHub repo, two checkouts. WSL clone on the Linux filesystem for `server/` and
`agent/` (worktree-per-task on `/mnt/c` is painfully slow). Windows clone for anything that must
build natively.

**UI: web app, not Tauri.** The PTY already lives in the WSL agent, which was Tauri's main draw.
Everything else native — tray, notifications, window focus, idle time — is provided by the Windows
host process that exists anyway. Removes a dependency known to be awkward from Linux. Aesthetics are
unaffected: Tauri *is* a webview. Chrome app mode or a PWA install closes the window-shell gap.

**Deployment:** Docker Compose on the Pi, so upgrades are atomic and rollback is one command. The
current `git pull && restart` has no way back from a bad deploy.

**Observability:** the event log. No Prometheus for a single-user system.

**Secrets:** `.env` with file permissions. No vault.

**Everything is free:** Tailscale (mesh + Funnel ingress), FCM, Groq/Gemini free tiers, the existing
Claude subscription, Notion, GitHub, Piper, on-device Android STT.

---

## 11. Failure modes

Ranked by damage.

**Silent wrongness.** No single mitigation — it is the through-line: visible memory writes,
attributed board updates, the event log, discriminated tool results, every instruction ending in a
state.

**Claude looping unattended.** Per-task wall-clock and turn caps, plus the stall detector. Reserve
subscription headroom so overnight work cannot exhaust the limits the next morning needs.

**Pi down.** Total outage; everything else is a view. Accept the single point of failure — do not
build HA for one user. Make recovery fast instead: restart policies, nightly SQLite backup, memory
repo pushed to a private GitHub repo, rebuild onto new hardware inside an hour. Clients must show
"can't reach Kestrel" plainly rather than appearing to work.

**Agent dies mid-task.** State is in the event log and the worktree survives on disk. On reconnect
the agent reports what exists and the Pi reconciles; interrupted tasks are flagged for resume. Never
infer a task is healthy from having last seen it running — per-task heartbeat plus stall detection.

**Internet down.** Hosted inference unreachable. The tailnet still works, so status and deterministic
functions continue. It says what it cannot do.

**Prompt injection.** Real once tickets, PR comments, and email flow into context. External content
is data, never instructions. Anything scope-changing asks regardless of where the idea originated —
the how/what boundary gives this for free.

**Notification storms.** Rate limit per task and globally, with a circuit breaker that mutes and
reports itself.

---

## 12. Spikes

Throwaway, timeboxed, ordered by what they would invalidate. The first two require a physical device
and are Callum's to run.

1. **Call path.** Self-managed `ConnectionService` + high-priority FCM to a dozing app on One UI.
   Prove: full-screen UI over the lock screen, audio routed to AirPods, answer gesture works, cold
   delivery latency, whether a battery-optimisation exemption is needed.
2. **The knock.** What the older AirPods actually emit on Android, and whether a notification-listener
   service can observe Spotify playback state changes with enough timing fidelity to detect
   pause-play-pause.
3. **The WSL chain.** Agent spawned via `wsl.exe` over stdio, PTY inside it, streamed to `xterm.js`,
   typed into from both ends.
4. **Piper on the Pi.** Real latency for a two-sentence reply. Decides whether the mini PC is
   necessary.
5. **Claude Code integration.** Hooks firing to HTTP with useful payloads; Agent SDK streaming for
   unattended runs.

Cheap ones affecting sequencing only: volume-key accessibility shortcut with the screen locked;
whether a USB-C audio adapter fires `ACTION_HEADSET_PLUG` or appears as a USB audio device; whether
the Microsoft tenant permits self-consent to Graph.

---

## 13. Superseded decisions

From v1, with reasons.

| v1 | Now | Why |
|---|---|---|
| Wake word (OpenWakeWord → microWakeWord) | Deleted entirely | Monopolised the mic, damaged capture, unreliable activation |
| Phone as the primary audio client | One client among several; text-first | Voice is an escalation and hands-busy channel, not an interface |
| Task router: simple / phone_action / agentic | Generic task + four executor kinds | Routing was Claude-Code-shaped; capabilities need a real seam |
| Life vault (Pi) + work vault (PC) | Durable curated + derived indexed, all on the Pi | Split by *where truth lives*, not by device |
| PC-off = hard refusal | Report and offer to queue | Silent or blunt refusal is what makes it feel like an API |
| Local model for triage / work-vault retrieval | Removed | Both machines are AMD; it was never going to pay off |
| Desktop app "not a core dependency" | The primary desk surface, hosting the terminal | It is where the work happens |
| Tauri | Web UI + Windows helper | PTY moved to the agent; Tauri from Linux is awkward |
| Repo on `C:\` edited via WSL | Linux filesystem, two checkouts | Worktree-per-task over `/mnt/c` is slow |
| Wake-on-LAN | Removed | The PC is on or off deliberately |
| Screen vision / observation layer | Structured focus object | Exact, cheap, no vision model |
| Personality as a subsystem | Identity layer, injected everywhere | Consistency requires it on every call, including cheap ones |
| `kestrel.service` on the Pi | Replaced outright by the new service | v1 and v2 both binding :8099 would look like a working deploy and silently bind nothing |

---

## 14. Open questions

- Piper voice selection
- Event and task record schemas (design during implementation, not upfront)
- Whether Microsoft Graph self-consent is available on the work tenant
- Whether the old spare PC is worth its idle draw (needs measuring; it is AMD, so the local-inference
  argument is weak)
