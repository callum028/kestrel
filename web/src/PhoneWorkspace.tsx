import { useState } from "react";
import { api, type ConversationMessage, type Delivery, type State, type Task } from "./api";
import { ConversationPane } from "./ConversationPane";
import { PhoneSession } from "./PhoneSession";

type View =
  | { kind: "chat" }
  | { kind: "tasks" }
  | { kind: "task"; handle: string }
  | { kind: "session"; terminalId: string; handle: string };

interface Props {
  state: State | null;
  tasks: Task[];
  deliveries: Delivery[];
  messages: ConversationMessage[];
  thinking: boolean;
  sendMessage: (text: string) => Promise<unknown>;
  refresh: () => Promise<void>;
  /** Set once, from a notification's deep link (public/sw.js -> /?delivery=id) -
   * scrolled into view in the strip rather than driving navigation, since a
   * delivery is not a screen of its own. */
  focusDeliveryId: string | null;
}

// Phone view per docs/design.md §7: Kestrel chat is the home screen, a strip
// surfaces what needs Callum only when something does, and tasks are a
// separate screen reached by a simple tab bar - no terminal tabs, no rail
// layout, because none of that fits a phone width.
export function PhoneWorkspace({
  state,
  tasks,
  deliveries,
  messages,
  thinking,
  sendMessage,
  refresh,
  focusDeliveryId,
}: Props) {
  const [view, setView] = useState<View>({ kind: "chat" });

  const needsInput = tasks.filter((t) => t.state === "needs_input");
  const urgentDeliveries = deliveries.filter((d) => d.urgency !== "observation");
  const needsYouCount = needsInput.length + urgentDeliveries.length;

  const openTaskSession = async (task: Task) => {
    const result = await api.openTerminal({ cwd: "~/workspace/github", task_handle: task.handle });
    if (result.status !== "ok") {
      window.alert(result.status === "not_found" ? `Couldn't find ${result.searched_for}` : result.reason);
      return;
    }
    setView({ kind: "session", terminalId: result.id, handle: task.handle });
  };

  if (view.kind === "session") {
    return (
      <PhoneSession
        terminalId={view.terminalId}
        onClose={() => setView({ kind: "task", handle: view.handle })}
      />
    );
  }

  const activeTask = view.kind === "task" ? tasks.find((t) => t.handle === view.handle) : undefined;

  return (
    <div className="phone-app">
      {needsYouCount > 0 && (
        <div className="needs-you-strip">
          <span className="needs-you-label">Needs you</span>
          {needsInput.map((t) => (
            <button key={t.handle} className="needs-you-item" onClick={() => setView({ kind: "task", handle: t.handle })}>
              {t.handle} is waiting on you
            </button>
          ))}
          {urgentDeliveries.map((d) => (
            <button
              key={d.id}
              className={`needs-you-item${d.id === focusDeliveryId ? " highlight" : ""}`}
              onClick={async () => {
                await api.ack(d.id, "phone");
                await refresh();
              }}
            >
              {d.subject}
            </button>
          ))}
        </div>
      )}

      <main className="phone-body">
        {view.kind === "chat" && (
          <ConversationPane messages={messages} thinking={thinking} onSend={sendMessage} />
        )}

        {view.kind === "tasks" && (
          <div className="phone-task-list">
            {tasks.length === 0 ? (
              <p className="empty">Nothing in flight.</p>
            ) : (
              tasks.map((t) => (
                <button key={t.handle} className="phone-task-row" onClick={() => setView({ kind: "task", handle: t.handle })}>
                  <span className="row">
                    <span className="handle">{t.handle}</span>
                    <span className={`state ${t.state}`}>{t.state}</span>
                  </span>
                  <span className="sub">{t.goal}</span>
                </button>
              ))
            )}
          </div>
        )}

        {view.kind === "task" && activeTask && (
          <div className="phone-task-detail">
            <button className="phone-back" onClick={() => setView({ kind: "tasks" })}>
              ‹ Tasks
            </button>
            <h2>{activeTask.handle}</h2>
            <p className={`state ${activeTask.state}`}>{activeTask.state}</p>
            <p>{activeTask.goal}</p>
            {activeTask.criteria.length > 0 && (
              <>
                <h3>Criteria</h3>
                <ul>
                  {activeTask.criteria.map((c) => (
                    <li key={c}>{c}</li>
                  ))}
                </ul>
              </>
            )}
            <p className="sub">
              {activeTask.executor} · {activeTask.nudges} nudge{activeTask.nudges === 1 ? "" : "s"}
            </p>
            <button className="open-session" onClick={() => openTaskSession(activeTask)}>
              Open session
            </button>
          </div>
        )}
      </main>

      <nav className="phone-tabs">
        <button aria-selected={view.kind === "chat"} onClick={() => setView({ kind: "chat" })}>
          Chat
        </button>
        <button aria-selected={view.kind === "tasks" || view.kind === "task"} onClick={() => setView({ kind: "tasks" })}>
          Tasks{tasks.length > 0 ? ` (${tasks.length})` : ""}
        </button>
        <span className="presence">
          <span className={`dot ${state?.presence ?? "away"}`} />
        </span>
      </nav>
    </div>
  );
}
