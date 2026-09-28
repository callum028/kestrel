import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  isUnavailable,
  type ConversationMessage,
  type Delivery,
  type State,
  type Task,
  type TerminalInfo,
} from "./api";

const POLL_MS = 3000;

/** Everything both layouts (desk and phone) poll for. One hook, one set of
 * intervals, so the two UIs are views over identical state rather than
 * drifting copies of the same fetch logic. */
export function useKestrelData() {
  const [state, setState] = useState<State | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [deliveries, setDeliveries] = useState<Delivery[]>([]);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [thinking, setThinking] = useState(false);
  const [terminals, setTerminals] = useState<TerminalInfo[]>([]);
  // Set from GET /terminals's "unavailable" shape (the session host - a
  // separate long-lived process - isn't reachable), null while it is. Polled
  // here rather than only from the desk's terminal rail so a phone that
  // hasn't opened a session yet still gets the banner instead of finding out
  // only when "Open session" quietly fails.
  const [sessionHostReason, setSessionHostReason] = useState<string | null>(null);
  // Distinct from "loading": this is "the last poll failed", which is the
  // fact an offline state has to surface rather than just showing whatever
  // was fetched last as if it were still current.
  const [reachable, setReachable] = useState(true);
  const cursor = useRef(0);

  const refresh = useCallback(async () => {
    try {
      const [s, t, d] = await Promise.all([api.state(), api.tasks(), api.deliveries()]);
      setState(s);
      setTasks(t);
      setDeliveries(d);
      setReachable(true);
    } catch {
      setReachable(false);
    }
  }, []);

  const pollMessages = useCallback(async () => {
    try {
      const [fresh, status] = await Promise.all([
        api.conversation(cursor.current),
        api.conversationStatus(),
      ]);
      if (fresh.length) {
        setMessages((prev) => [...prev, ...fresh]);
        cursor.current = fresh[fresh.length - 1].id;
      }
      setThinking(status.thinking);
      setReachable(true);
    } catch {
      setReachable(false);
    }
  }, []);

  // A missing session host is not "no terminals" - it is a different, more
  // serious thing (kestrel/api.py's `_unavailable`), and it must not look
  // like an empty list here either.
  const refreshTerminals = useCallback(async () => {
    try {
      const result = await api.terminals();
      if (isUnavailable(result)) {
        setTerminals([]);
        setSessionHostReason(result.reason);
      } else {
        setTerminals(result);
        setSessionHostReason(null);
      }
      setReachable(true);
    } catch {
      setReachable(false);
    }
  }, []);

  useEffect(() => {
    refresh().catch(() => undefined);
    const id = setInterval(() => refresh().catch(() => undefined), POLL_MS);
    return () => clearInterval(id);
  }, [refresh]);

  useEffect(() => {
    refreshTerminals().catch(() => undefined);
    const id = setInterval(() => refreshTerminals().catch(() => undefined), POLL_MS);
    return () => clearInterval(id);
  }, [refreshTerminals]);

  useEffect(() => {
    pollMessages().catch(() => undefined);
    const id = setInterval(() => pollMessages().catch(() => undefined), POLL_MS);
    return () => clearInterval(id);
  }, [pollMessages]);

  const sendMessage = useCallback(
    async (text: string) => {
      const outcome = await api.sendMessage(text);
      await pollMessages();
      return outcome;
    },
    [pollMessages],
  );

  return {
    state,
    tasks,
    deliveries,
    messages,
    thinking,
    terminals,
    sessionHostReason,
    reachable,
    refresh,
    refreshTerminals,
    sendMessage,
  };
}
