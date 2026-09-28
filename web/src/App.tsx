import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  clearToken,
  hasToken,
  setOnTokenRejected,
  setToken,
  type TerminalInfo,
  validateToken,
} from "./api";
import { ConversationPane } from "./ConversationPane";
import { PhoneWorkspace } from "./PhoneWorkspace";
import { PushToggle } from "./PushToggle";
import { TerminalPane, disposeTerminal } from "./TerminalPane";
import { PHONE_QUERY, useMediaQuery } from "./useMediaQuery";
import { useKestrelData } from "./useKestrelData";

const DEFAULT_CWD = "~/workspace/github";

// Tabs are capabilities. Conversation is deliberately not one of them - it sits
// alongside, always present, because navigating away from what you are looking
// at to talk to Kestrel is how shared focus dies.
const TABS = [
  { id: "work", label: "Work", ready: true },
  { id: "commitments", label: "Commitments", ready: false },
  { id: "calendar", label: "Calendar", ready: false },
] as const;

/** A notification click deep-links to `/?delivery=<id>` (public/sw.js). Read
 * once on load and strip it back out of the URL so a refresh does not re-focus
 * the same thing forever. */
function useDeepLinkedDelivery(): string | null {
  const [id] = useState<string | null>(() => new URLSearchParams(location.search).get("delivery"));
  useEffect(() => {
    if (id) window.history.replaceState(null, "", location.pathname);
  }, [id]);
  return id;
}

/** The one-time pairing fragment a `kestrel-pair` link carries -
 * `#pair=<token>` - read once on load, never left sitting in the address bar
 * (see docs/design.md §11a on why the token lives in a fragment: it never
 * reaches an HTTP request, so pulling it out of `location.hash` client-side
 * is the only place it is ever visible at all). */
function readPairingFragment(): string | null {
  const hash = location.hash;
  const prefix = "#pair=";
  if (!hash.startsWith(prefix)) return null;
  return decodeURIComponent(hash.slice(prefix.length));
}

type AuthPhase = "checking" | "authed" | "unauthed";

/** The token gate: resolves a `#pair=` fragment (if present) against the
 * server *before* trusting it, strips it from the URL regardless of outcome,
 * and re-opens the gate on any later 401/403 (a stored token the server no
 * longer accepts, e.g. after a rotation - `api.ts`'s `onTokenRejected`) so
 * that always reads as "go pair again", never a silent, permanent failure. */
function useAuthPhase() {
  const [phase, setPhase] = useState<AuthPhase>(() =>
    readPairingFragment() ? "checking" : hasToken() ? "authed" : "unauthed",
  );
  const [pairError, setPairError] = useState<string | null>(null);

  useEffect(() => {
    setOnTokenRejected(() => setPhase("unauthed"));
    return () => setOnTokenRejected(null);
  }, []);

  useEffect(() => {
    const candidate = readPairingFragment();
    if (!candidate) return;
    // Stripped immediately, before validation resolves - a failed pairing
    // attempt must not leave the token sitting in browser history either.
    window.history.replaceState(null, "", location.pathname + location.search);
    validateToken(candidate).then((ok) => {
      if (ok) {
        setToken(candidate);
        setPairError(null);
        setPhase("authed");
      } else {
        setPairError("That pairing link was rejected - it may be stale. Ask for a fresh one.");
        setPhase("unauthed");
      }
    });
  }, []);

  const submitToken = useCallback(async (candidate: string) => {
    setPhase("checking");
    const ok = await validateToken(candidate);
    if (ok) {
      setToken(candidate);
      setPairError(null);
      setPhase("authed");
    } else {
      setPairError("That token was rejected.");
      setPhase("unauthed");
    }
  }, []);

  const unpair = useCallback(() => {
    clearToken();
    setPhase("unauthed");
  }, []);

  return { phase, pairError, submitToken, unpair };
}

export default function App() {
  // The token check has to happen *outside* the component holding the hooks.
  // An early return inside it does not stop effects - React still runs them -
  // so a tokenless window sat there issuing a 401 every three seconds.
  const { phase, pairError, submitToken, unpair } = useAuthPhase();
  if (phase === "checking") return <Pairing />;
  return phase === "authed" ? <Shell onUnpair={unpair} /> : <NoToken error={pairError} onSubmit={submitToken} />;
}

function Pairing() {
  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">Kestrel</span>
      </header>
      <div className="placeholder">
        <p>Pairing this device…</p>
      </div>
    </div>
  );
}

function NoToken({
  error,
  onSubmit,
}: {
  error: string | null;
  onSubmit: (token: string) => void;
}) {
  const [pasted, setPasted] = useState("");
  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">Kestrel</span>
      </header>
      <div className="placeholder">
        <div style={{ maxWidth: 520, textAlign: "left", lineHeight: 1.6 }}>
          <p style={{ color: "var(--warn)", marginTop: 0 }}>
            {error ?? "This device isn't paired with Kestrel yet."}
          </p>
          <p>
            On the machine running the server: <code>kestrel-pair</code> (or{" "}
            <code>python -m kestrel.pair</code>) prints a one-time link. Open it on this device to
            pair it - the token is never baked into the app itself, so this is the only way in.
          </p>
          <p style={{ color: "var(--text-faint)" }}>
            No link handy? Paste a token directly instead:
          </p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (pasted.trim()) onSubmit(pasted.trim());
            }}
            style={{ display: "flex", gap: 8 }}
          >
            <input
              type="password"
              value={pasted}
              onChange={(e) => setPasted(e.target.value)}
              placeholder="paste token"
              style={{ flex: 1 }}
            />
            <button type="submit" disabled={!pasted.trim()}>
              Pair
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}

function OfflineBanner({ reachable }: { reachable: boolean }) {
  // A failed poll must never look like an empty-but-fine state: silently
  // showing stale tasks/deliveries is indistinguishable from "nothing is
  // happening", which is the one lie an always-on assistant cannot tell.
  if (reachable) return null;
  return <div className="offline-banner">Can't reach Kestrel. Retrying…</div>;
}

function UnpairControl({ onUnpair }: { onUnpair: () => void }) {
  // Deliberately unobtrusive - unpairing is rare and destructive enough
  // (this device stops being able to reach Kestrel at all until paired
  // again) that it shouldn't sit next to anything reached in normal use.
  return (
    <button
      type="button"
      onClick={() => {
        if (window.confirm("Unpair this device? You'll need a new pairing link to use it again.")) {
          onUnpair();
        }
      }}
      title="Unpair this device"
      style={{
        position: "fixed",
        bottom: 8,
        right: 8,
        opacity: 0.4,
        fontSize: 11,
        zIndex: 100,
      }}
    >
      Unpair
    </button>
  );
}

function Shell({ onUnpair }: { onUnpair: () => void }) {
  const isPhone = useMediaQuery(PHONE_QUERY);
  const data = useKestrelData();
  const focusDeliveryId = useDeepLinkedDelivery();

  return (
    <>
      <UnpairControl onUnpair={onUnpair} />
      <OfflineBanner reachable={data.reachable} />
      {isPhone ? (
        <PhoneWorkspace
          state={data.state}
          tasks={data.tasks}
          deliveries={data.deliveries}
          messages={data.messages}
          thinking={data.thinking}
          sendMessage={data.sendMessage}
          refresh={data.refresh}
          focusDeliveryId={focusDeliveryId}
        />
      ) : (
        <DeskWorkspace
          state={data.state}
          tasks={data.tasks}
          deliveries={data.deliveries}
          messages={data.messages}
          thinking={data.thinking}
          sendMessage={data.sendMessage}
          refresh={data.refresh}
          focusDeliveryId={focusDeliveryId}
        />
      )}
    </>
  );
}

interface WorkspaceProps {
  state: ReturnType<typeof useKestrelData>["state"];
  tasks: ReturnType<typeof useKestrelData>["tasks"];
  deliveries: ReturnType<typeof useKestrelData>["deliveries"];
  messages: ReturnType<typeof useKestrelData>["messages"];
  thinking: ReturnType<typeof useKestrelData>["thinking"];
  sendMessage: ReturnType<typeof useKestrelData>["sendMessage"];
  refresh: ReturnType<typeof useKestrelData>["refresh"];
  focusDeliveryId: string | null;
}

function DeskWorkspace({
  state,
  tasks,
  deliveries,
  messages,
  thinking,
  sendMessage,
  refresh,
  focusDeliveryId,
}: WorkspaceProps) {
  const [tab, setTab] = useState<string>("work");
  const [terminals, setTerminals] = useState<TerminalInfo[]>([]);
  const [activeTerminal, setActiveTerminal] = useState<string | null>(null);
  const [activeTask, setActiveTask] = useState<string | null>(null);
  const lastInput = useRef<number>(Date.now());

  const refreshTerminals = useCallback(async () => {
    setTerminals(await api.terminals());
  }, []);

  useEffect(() => {
    refreshTerminals().catch(() => undefined);
    const id = setInterval(() => refreshTerminals().catch(() => undefined), 3000);
    return () => clearInterval(id);
  }, [refreshTerminals]);

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
    await refreshTerminals();
  };

  const closeTerminal = async (id: string) => {
    await api.closeTerminal(id);
    disposeTerminal(id);
    setActiveTerminal((current) => (current === id ? null : current));
    await refreshTerminals();
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
        <PushToggle />
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
                <div key={d.id} className={`said ${d.urgency}${d.id === focusDeliveryId ? " highlight" : ""}`}>
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
          <ConversationPane messages={messages} thinking={thinking} onSend={sendMessage} compact />
        </aside>
      </div>
    </div>
  );
}
