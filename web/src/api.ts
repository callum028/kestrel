// Thin client over the Kestrel API. Every endpoint returns a state rather than
// an empty 200, so the discriminant is worth preserving all the way up.

export type Outcome<T = unknown> =
  | ({ status: "ok" } & T)
  | { status: "not_found"; searched_for: string; looked_in?: string }
  | { status: "refused"; reason: string }
  // The session host (a separate long-lived process the terminals subsystem
  // is a client of - see agent/kestrel_agent/host_client.py) isn't reachable.
  // Distinct from every other outcome above: those are about *this* request
  // ("no such task", "refused"), this one means the whole terminals
  // subsystem is down, and every /terminals endpoint (and the websocket) can
  // return it - a caller that only handles "ok" vs "not_found" will crash on
  // it, which is exactly what happened before this type existed.
  | { status: "unavailable"; reason: string };

export interface Task {
  handle: string;
  goal: string;
  state: string;
  executor: string;
  nudges: number;
  criteria: string[];
}

// GitHub's own pre-merge check on the PR branch - distinct from `validation`
// below, since a task can be waiting on one without the other having run.
export interface CiStatus {
  pr_number?: number;
  state: string;
  summary: string;
}

// Kestrel's own post-merge authoritative run against dev.
export interface ValidationResult {
  ok: boolean;
  summary: string;
  failing_tests: string[];
}

// GET /tasks/{handle} - everything the summary list doesn't carry.
export interface TaskDetail extends Task {
  time_in_state_seconds: number;
  pr_url: string | null;
  ticket_url: string | null;
  ci: CiStatus | null;
  validation: ValidationResult | null;
  pending_wait: boolean;
  pending_question: string | null;
  final_report: string | null;
}

export interface TerminalInfo {
  id: string;
  cwd: string;
  task_id: string | null;
  alive: boolean;
  command: string[];
}

// GET /terminals: a bare array normally, or the same "unavailable" shape
// every other terminals endpoint can return - never an empty array standing
// in for "can't tell", which would look identical to "there are none".
export type TerminalsList = TerminalInfo[] | { status: "unavailable"; reason: string };

export function isUnavailable(value: unknown): value is { status: "unavailable"; reason: string } {
  return (
    typeof value === "object" &&
    value !== null &&
    (value as { status?: unknown }).status === "unavailable"
  );
}

export interface Delivery {
  id: string;
  subject: string;
  body: string;
  channel: string;
  urgency: "observation" | "normal" | "blocking";
  escalations: number;
}

export interface State {
  block: string;
  presence: "at_desk" | "nearby" | "away";
  focus: string;
  speech_suppressed: boolean;
}

// The Kestrel chat - one conversation, shared across every device. Distinct
// from a Claude chat (raw terminal output, one per session): this is
// Kestrel's own words, and role is what keeps the two visually apart
// wherever this renders.
export interface ConversationMessage {
  id: number;
  role: "user" | "kestrel" | "system";
  text: string;
  created_at: string;
  refs: { kind: string; handle: string }[];
}

// Where the API is depends on how the page was loaded:
//
// - Vite dev: same origin, because the dev server proxies. BASE stays empty.
// - Tauri: the page comes from tauri://localhost, an origin with no API on it.
//   Relative URLs silently go nowhere, which is exactly what happened the first
//   time this was built. The shell injects the real address instead.
// - Served by the Kestrel server itself (web_static.py, on the Pi behind
//   `tailscale serve`): same origin as the API by construction - one process,
//   one tailnet URL - so this already falls out of the `location.protocol`
//   branch below with no change needed.
//
// Injected rather than baked in at build time so the server can move to the Pi
// without rebuilding the app. (CSP connect-src has to allow the host too.)
const FALLBACK_API = "http://localhost:8099";
const BASE =
  window.__KESTREL_API__ ??
  import.meta.env.VITE_API_BASE ??
  (location.protocol.startsWith("http") ? "" : FALLBACK_API);

// The token is a header, never a cookie: browsers attach cookies to cross-site
// requests automatically, which is exactly the hole this is closing.
//
// Resolution order (docs/design.md §11a, §13 - the Tauri shell that used to
// inject this is superseded, and the design explicitly forbids baking a
// token into the bundle):
//
//   1. `window.__KESTREL_TOKEN__` - a desktop shell that still injects one.
//   2. `VITE_KESTREL_TOKEN` - dev only (`import.meta.env.DEV`), never a
//      production build; a released bundle serves any number of devices and
//      cannot carry one device's secret.
//   3. `localStorage` - what pairing (below, and `App.tsx`'s handling of
//      `#pair=<token>`) actually populates for a phone or a browser tab that
//      has no shell to inject anything.
//
// `TOKEN` is a mutable module-level binding rather than a `const`, precisely
// so pairing/unpairing take effect immediately for every subsequent call
// without a page reload - see `setToken`/`clearToken`.
declare global {
  interface Window {
    /** Injected by the desktop shell: where the Kestrel server actually is. */
    __KESTREL_API__?: string;
    __KESTREL_TOKEN__?: string;
    /** Where the desktop shell found the token, or "none". A release build has
     *  no console, so this is the only way a missing token can be explained. */
    __KESTREL_TOKEN_SOURCE__?: string;
  }
}

const STORAGE_KEY = "kestrel:token";

function readStoredToken(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) || "";
  } catch {
    // Private browsing / blocked storage: pairing simply can't persist here,
    // which surfaces as "no token" - not a crash.
    return "";
  }
}

let TOKEN =
  window.__KESTREL_TOKEN__ ||
  (import.meta.env.DEV ? import.meta.env.VITE_KESTREL_TOKEN : undefined) ||
  readStoredToken() ||
  "";

export const tokenSource = window.__KESTREL_TOKEN_SOURCE__;
export function hasToken(): boolean {
  return TOKEN.length > 0;
}

/** Called once a candidate token (from a pairing link or the paste-token
 * fallback) has been checked against the server - see `validateToken`. */
export function setToken(token: string): void {
  TOKEN = token;
  try {
    localStorage.setItem(STORAGE_KEY, token);
  } catch {
    // Nothing to fall back to - the token still works for this page load,
    // it just won't survive a refresh. Not worth surfacing as an error.
  }
}

/** The "unpair this device" action: forgets the token everywhere this module
 * knows about it. The next call will 401/403, which `json()` below turns
 * into a call to `onTokenRejected` - the same path a server-side rotation
 * takes - so callers don't need a separate "go back to pairing" case. */
export function clearToken(): void {
  TOKEN = "";
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Already effectively cleared for this session (TOKEN above); a stale
    // value left in storage after a blocked removal is a private-browsing
    // edge case, not a security issue on its own.
  }
}

export interface ValidateResult {
  ok: boolean;
  /** Set on a 403 specifically - "the token would work, but this origin
   * isn't allowed" - as opposed to a 401 or a network failure, both of
   * which just mean "no, try a different token/link". Lets the pairing UI
   * say the actually-true thing instead of "that link was rejected" for a
   * problem a fresh link can't fix. */
  originRejected?: string;
}

/** Checks a candidate token against a real endpoint before it's ever stored -
 * a mistyped paste or a stale pairing link must never make it into
 * localStorage looking valid. `/pair/validate` requires no state of its own;
 * any authenticated endpoint would do, this one just names the purpose.
 * Deliberately bypasses `json()`/`onOriginRejected` above: a candidate that
 * hasn't been trusted yet must never trigger the same global "show an error
 * banner over the app" path a rejected *stored* token does. */
export async function validateToken(candidate: string): Promise<ValidateResult> {
  try {
    const response = await fetch(`${BASE}/pair/validate`, {
      headers: { Authorization: `Bearer ${candidate}` },
    });
    if (response.ok) return { ok: true };
    if (response.status === 403) {
      const reason = (await readReason(response)) ?? "origin not allowed";
      return { ok: false, originRejected: `${reason} - set KESTREL_ALLOWED_ORIGINS on the server.` };
    }
    return { ok: false };
  } catch {
    return { ok: false };
  }
}

// Set by App.tsx's auth gate. A 401 on *any* call - not just pairing's own
// validation - means the stored token itself was rejected (typically a
// rotation on the server, e.g. after `~/.kestrel/token` was restored from
// backup) and the device needs to be re-paired rather than quietly failing
// forever. See docs/design.md §11a's "can't reach Kestrel" table, which this
// is deliberately distinct from - a 401 is "wrong token", not "no server".
//
// A 403, below, is a *different* fact - "this token would work, but the
// server doesn't recognise the origin it arrived from" - and must not be
// treated the same way: unpairing over it would throw away a perfectly good
// token and send the user back to a pairing screen that can't fix the real
// problem (a missing/wrong KESTREL_ALLOWED_ORIGINS on the server). See
// `setOnOriginRejected`.
let onTokenRejected: (() => void) | null = null;
export function setOnTokenRejected(callback: (() => void) | null): void {
  onTokenRejected = callback;
}

/** Called on a 403 - "origin not allowed" - with the server's own reason
 * text appended to a hint about the fix. Distinct from `onTokenRejected`:
 * this is a visible error banner, not a trip back to the pairing screen -
 * the stored token is fine, the server's origin allowlist is what needs
 * attention (docs/design.md §11a). */
let onOriginRejected: ((message: string) => void) | null = null;
export function setOnOriginRejected(callback: ((message: string) => void) | null): void {
  onOriginRejected = callback;
}

async function readReason(response: Response): Promise<string | null> {
  try {
    const body = (await response.json()) as { reason?: unknown };
    return typeof body.reason === "string" ? body.reason : null;
  } catch {
    return null;
  }
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { Authorization: `Bearer ${TOKEN}` };
  if (init?.body) headers["Content-Type"] = "application/json";

  const response = await fetch(`${BASE}${path}`, { ...init, headers });
  if (response.status === 401) {
    clearToken();
    onTokenRejected?.();
    throw new Error(`${path} → 401: token rejected`);
  }
  if (response.status === 403) {
    const reason = (await readReason(response)) ?? "origin not allowed";
    onOriginRejected?.(`${reason} - set KESTREL_ALLOWED_ORIGINS on the server.`);
    throw new Error(`${path} → 403: ${reason}`);
  }
  if (!response.ok) throw new Error(`${init?.method ?? "GET"} ${path} → ${response.status}`);
  return response.json() as Promise<T>;
}

export const api = {
  state: () => json<State>("/state"),
  tasks: () => json<Task[]>("/tasks"),
  taskDetail: (handle: string) => json<Outcome<TaskDetail>>(`/tasks/${handle}`),
  deliveries: () => json<Delivery[]>("/deliveries"),

  terminals: () => json<TerminalsList>("/terminals"),

  openTerminal: (body: { cwd: string; task_handle?: string; command?: string[] }) =>
    json<Outcome<{ id: string; cwd: string; task_id: string | null }>>("/terminals", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  closeTerminal: (id: string) =>
    json<Outcome>(`/terminals/${id}`, { method: "DELETE" }),

  ack: (id: string, on: string) =>
    json<Outcome>(`/deliveries/${id}/ack`, {
      method: "POST",
      body: JSON.stringify({ on }),
    }),

  // Focus is a sensor, not a belief: the client reports what is on screen and
  // the server decides what that means. It also drives where questions land -
  // one about the task you are looking at arrives inline instead of buzzing.
  signals: (body: Record<string, unknown>) =>
    json<{ presence: string }>("/clients/signals", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  conversation: (after = 0) =>
    json<ConversationMessage[]>(`/conversation/messages?after=${after}&limit=200`),

  // The brain step's visible "thinking" state - a reply is being generated
  // in the background, so there is nothing new in `conversation()` yet, but
  // the UI still has something to show for it.
  conversationStatus: () => json<{ thinking: boolean }>("/conversation/status"),

  sendMessage: (text: string) =>
    json<Outcome<ConversationMessage>>("/conversation/messages", {
      method: "POST",
      body: JSON.stringify({ text }),
    }),

  vapidPublicKey: () => json<{ public_key: string }>("/push/vapid-public-key"),

  subscribePush: (subscription: PushSubscriptionJSON) =>
    json<Outcome>("/push/subscriptions", {
      method: "POST",
      body: JSON.stringify(subscription),
    }),

  unsubscribePush: (endpoint: string) =>
    json<Outcome>("/push/subscriptions", {
      method: "DELETE",
      body: JSON.stringify({ endpoint }),
    }),
};

// The websocket handshake has no room for a JSON body, so a terminal socket
// that can't be served closes with one of these application-defined codes
// instead (kestrel/api.py's `terminal_ws`) - a plain 1000/1006 would look the
// same as "you closed the laptop lid", which is not what "the session host
// just isn't running" or "this terminal is gone" mean. Real browsers only
// guarantee `event.reason` for a close sent *after* a completed handshake -
// closing pre-accept (4401/4404/4503 all do, so the client can never read a
// stale terminal's output) can arrive as a bare 1006 with no reason instead,
// so this always has a message for that too.
export function terminalCloseMessage(event: { code: number; reason: string }): string {
  switch (event.code) {
    case 4503:
      return `session host isn't running${event.reason ? ` - ${event.reason}` : ""}`;
    case 4404:
      return "this terminal no longer exists";
    case 4401:
      return "not authorized - re-pair this device";
    default:
      return event.reason || "detached";
  }
}

export function terminalSocket(id: string): WebSocket {
  // The browser WebSocket API cannot set headers, so the token rides in the
  // query string here - the one place it does.
  const origin = BASE || location.origin;
  const url = new URL(`/terminals/${id}/ws`, origin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("token", TOKEN);
  return new WebSocket(url);
}
