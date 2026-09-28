// Thin client over the Kestrel API. Every endpoint returns a state rather than
// an empty 200, so the discriminant is worth preserving all the way up.

export type Outcome<T = unknown> =
  | ({ status: "ok" } & T)
  | { status: "not_found"; searched_for: string; looked_in?: string }
  | { status: "refused"; reason: string };

export interface Task {
  handle: string;
  goal: string;
  state: string;
  executor: string;
  nudges: number;
  criteria: string[];
}

export interface TerminalInfo {
  id: string;
  cwd: string;
  task_id: string | null;
  alive: boolean;
  command: string[];
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
//
// Injected rather than baked in at build time so the server can move to the Pi
// without rebuilding the app. (CSP connect-src has to allow the host too.)
const FALLBACK_API = "http://localhost:8099";
const BASE =
  window.__KESTREL_API__ ??
  import.meta.env.VITE_API_BASE ??
  (location.protocol.startsWith("http") ? "" : FALLBACK_API);

// The token is a header, never a cookie: browsers attach cookies to cross-site
// requests automatically, which is exactly the hole this is closing. Tauri
// injects it at startup after reading it off disk, so it is never baked into
// the bundle; the dev server passes it through the environment instead.
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

const TOKEN = window.__KESTREL_TOKEN__ || import.meta.env.VITE_KESTREL_TOKEN || "";

export const tokenSource = window.__KESTREL_TOKEN_SOURCE__;
export const hasToken = TOKEN.length > 0;

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { Authorization: `Bearer ${TOKEN}` };
  if (init?.body) headers["Content-Type"] = "application/json";

  const response = await fetch(`${BASE}${path}`, { ...init, headers });
  if (response.status === 401 || response.status === 403) {
    throw new Error(`${path} → ${response.status}: token rejected`);
  }
  if (!response.ok) throw new Error(`${init?.method ?? "GET"} ${path} → ${response.status}`);
  return response.json() as Promise<T>;
}

export const api = {
  state: () => json<State>("/state"),
  tasks: () => json<Task[]>("/tasks"),
  deliveries: () => json<Delivery[]>("/deliveries"),

  terminals: () => json<TerminalInfo[]>("/terminals"),

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

export function terminalSocket(id: string): WebSocket {
  // The browser WebSocket API cannot set headers, so the token rides in the
  // query string here - the one place it does.
  const origin = BASE || location.origin;
  const url = new URL(`/terminals/${id}/ws`, origin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("token", TOKEN);
  return new WebSocket(url);
}
