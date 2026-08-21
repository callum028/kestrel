import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Delivery, type State, type Task, type TerminalInfo } from "./api";
import { TerminalPane, disposeTerminal } from "./TerminalPane";

const POLL_MS = 3000;
const DEFAULT_CWD = "~/workspace/github";

// Tabs are capabilities. Conversation is deliberately not one of them - it sits
// alongside, always present, because navigating away from what you are looking
// at to talk to Kestrel is how shared focus dies.
const TABS = [
  { id: "work", label: "Work", ready: true },
  { id: "commitments", label: "Commitments", ready: false },
  { id: "calendar", label: "Calendar", ready: false },
] as const;

export default function App() {
  const [tab, setTab] = useState<string>("work");
  const [state, setState] = useState<State | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [terminals, setTerminals] = useState<TerminalInfo[]>([]);
  const [deliveries, setDeliveries] = useState<Delivery[]>([]);
  const [activeTerminal, setActiveTerminal] = useState<string | null>(null);
  const [activeTask, setActiveTask] = useState<string | null>(null);
  const lastInput = useRef<number>(Date.now());

  const refresh = useCallback(async () => {
    const [s, t, term, d] = await Promise.all([
      api.state(),
      api.tasks(),
      api.terminals(),
      api.deliveries(),
    ]);
    setState(s);
    setTasks(t);
    setTerminals(term);
    setDeliveries(d);
  }, []);

  useEffect(() => {
    refresh().catch(() => undefined);
    const id = setInterval(() => refresh().catch(() => undefined), POLL_MS);
    return () => clearInterval(id);
  }, [refresh]);

  useEffect(() => {
    const mark = () => (lastInput.current = Date.now());
    window.addEventListener("keydown", mark);
    window.addEventListener("mousemove", mark);
    return () => {
      window.removeEventListener("keydown", mark);
      window.removeEventListener("mousemove", mark);
    };
  }, []);

  // Report attention rather than let the server guess. Focus is the active tab
  // plus what is selected inside it, which is what makes "why did it do that?"
  // resolve without an antecedent.
  useEffect(() => {
    const report = () =>
      api
        .signals({
          app_open: true,
          app_focused: document.hasFocus(),
          last_input_at: new Date(lastInput.current).toISOString(),
          task_handle: activeTask,
          pane: tab === "work" ? (activeTerminal ? "terminal" : "tasks") : tab,
        })
        .catch(() => undefined);
    report();
    const id = setInterval(report, 15000);
    return () => clearInterval(id);
  }, [tab, activeTask, activeTerminal]);

  const openTerminal = async (taskHandle?: string) => {
    const result = await api.openTerminal({ cwd: DEFAULT_CWD, task_handle: taskHandle });
    if (result.status !== "ok") {
      // Every instruction ends in a state; surfacing it beats a dead button.
      window.alert(
        result.status === "not_found"
          ? `Couldn't find ${result.searched_for}${result.looked_in ? ` in ${result.looked_in}` : ""}`
          : result.reason,
      );
      return;
    }
    setActiveTerminal(result.id);
    await refresh();
  };

  const closeTerminal = async (id: string) => {
    await api.closeTerminal(id);
    disposeTerminal(id);
    setActiveTerminal((current) => (current === id ? null : current));
    await refresh();
  };

  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">Kestrel</span>
        {TABS.map((t) => (
          <button
            key={t.id}
            className="tab"
            role="tab"
            aria-selected={tab === t.id}
            disabled={!t.ready}
            title={t.ready ? undefined : "not built yet"}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
        <span className="spacer" />
        <span className="presence">
          <span className={`dot ${state?.presence ?? "away"}`} />
          {state?.presence?.replace("_", " ") ?? "…"}
        </span>
      </header>

      <div className="work">
        <nav className="rail">
          <div className="rail-head">
            <span>Tasks</span>
          </div>
          {tasks.length === 0 ? (
            <p className="empty">Nothing in flight.</p>
          ) : (
            tasks.map((task) => (
              <button
                key={task.handle}
                className="item"
                aria-selected={activeTask === task.handle}
                onClick={() => setActiveTask(task.handle)}
                onDoubleClick={() => openTerminal(task.handle)}
              >
                <span className="row">
                  <span className="handle">{task.handle}</span>
                  <span className={`state ${task.state}`}>{task.state}</span>
                </span>
                <span className="sub">{task.goal}</span>
              </button>
            ))
          )}
        </nav>

        <main className="center">
          <div className="termbar">
            {terminals.map((terminal) => (
              <span
                key={terminal.id}
                className={`termtab${terminal.alive ? "" : " dead"}`}
                role="tab"
                aria-selected={activeTerminal === terminal.id}
              >
                <button onClick={() => setActiveTerminal(terminal.id)}>
                  {terminal.task_id
                    ? (tasks.find((t) => t.handle === activeTask)?.handle ?? "task")
                    : terminal.cwd.split("/").pop() || "shell"}
                </button>
                <button className="close" onClick={() => closeTerminal(terminal.id)}>
                  ×
                </button>
              </span>
            ))}
            <button className="termtab" onClick={() => openTerminal()} title="New terminal">
              +
            </button>
          </div>

          <div className="terminal-host">
            {activeTerminal ? (
              <TerminalPane id={activeTerminal} />
            ) : (
              <p className="placeholder">
                No terminal open. Press + for a shell, or double-click a task to open one
                against its worktree.
              </p>
            )}
          </div>
        </main>

        <aside className="rail right">
          <div className="rail-head">
            <span>Kestrel</span>
          </div>
          <div className="feed">
            {deliveries.length === 0 ? (
              <p className="empty">Nothing to raise.</p>
            ) : (
              deliveries.map((d) => (
                <div key={d.id} className={`said ${d.urgency}`}>
                  <div className="subject">
                    <span>{d.subject}</span>
                    <span>·</span>
                    <span>{d.channel.replace("_", " ")}</span>
                  </div>
                  <div className="body">{d.body}</div>
                  <button
                    className="ack"
                    onClick={async () => {
                      await api.ack(d.id, "desktop");
                      await refresh();
                    }}
                  >
                    acknowledge
                  </button>
                </div>
              ))
            )}
          </div>
          <div className="composer">
            <input disabled placeholder="Talk to Kestrel…" />
            <p className="note">
              No model provider configured yet, so it can report but not converse. Everything
              above is real.
            </p>
          </div>
        </aside>
      </div>
    </div>
  );
}
