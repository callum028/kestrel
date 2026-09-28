import { useCallback, useEffect, useRef, useState } from "react";
import { api, type ConversationMessage, type Delivery, type State, type Task } from "./api";

const POLL_MS = 3000;

/** Everything both layouts (desk and phone) poll for. One hook, one set of
 * intervals, so the two UIs are views over identical state rather than
 * drifting copies of the same fetch logic. */
export function useKestrelData() {
  const [state, setState] = useState<State | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [deliveries, setDeliveries] = useState<Delivery[]>([]);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
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
      const fresh = await api.conversation(cursor.current);
      if (fresh.length) {
        setMessages((prev) => [...prev, ...fresh]);
        cursor.current = fresh[fresh.length - 1].id;
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

  return { state, tasks, deliveries, messages, reachable, refresh, sendMessage };
}
