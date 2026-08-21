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

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: init?.body ? { "Content-Type": "application/json" } : undefined,
    ...init,
  });
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
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  return new WebSocket(`${scheme}://${location.host}/terminals/${id}/ws`);
}
