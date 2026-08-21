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

// In dev the Vite proxy makes the API same-origin. In a Tauri build the page is
// served from tauri://localhost, so it has to be addressed directly.
const BASE = import.meta.env.VITE_API_BASE ?? "";

// The token is a header, never a cookie: browsers attach cookies to cross-site
// requests automatically, which is exactly the hole this is closing. Tauri
// injects it at startup after reading it off disk, so it is never baked into
// the bundle; the dev server passes it through the environment instead.
declare global {
  interface Window {
    __KESTREL_TOKEN__?: string;
  }
}

const TOKEN = window.__KESTREL_TOKEN__ ?? import.meta.env.VITE_KESTREL_TOKEN ?? "";

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
