import { useEffect, useRef, useState } from "react";
import { terminalCloseMessage, terminalSocket } from "./api";

// eslint-disable-next-line no-control-regex
const ANSI = /\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\r/g;

function readable(chunk: string): string {
  // Claude Code's terminal UI leans on cursor movement and colour a
  // full xterm renders and a phone reading over your shoulder does not need -
  // stripped rather than rendered, so a spinner does not turn into noise.
  return chunk.replace(ANSI, "");
}

interface Props {
  terminalId: string;
  onClose: () => void;
}

/** "A readable view of the terminal output with a message box and quick
 * buttons" per docs/design.md §7 - not xterm.js. Good enough to see what
 * Claude is doing and answer a question from a phone, not to drive vim. */
export function PhoneSession({ terminalId, onClose }: Props) {
  const [text, setText] = useState("");
  const [draft, setDraft] = useState("");
  const [connected, setConnected] = useState(false);
  const socket = useRef<WebSocket | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const ws = terminalSocket(terminalId);
    socket.current = ws;
    ws.onopen = () => setConnected(true);
    ws.onclose = (event) => {
      setConnected(false);
      setText((prev) => prev + `\n[${terminalCloseMessage(event)}]\n`);
    };
    ws.onmessage = (event) => {
      const data = String(event.data);
      if (data.startsWith("{") && data.includes('"type":"exit"')) {
        setText((prev) => prev + "\n[session ended]\n");
        return;
      }
      setText((prev) => (prev + readable(data)).slice(-20000));
    };
    return () => ws.close();
  }, [terminalId]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [text]);

  const send = (data: string) => {
    if (socket.current?.readyState === WebSocket.OPEN) {
      socket.current.send(JSON.stringify({ type: "input", data }));
    }
  };

  const submit = () => {
    if (!draft.trim()) return;
    send(`${draft}\n`);
    setDraft("");
  };

  return (
    <div className="phone-session">
      <div className="phone-session-head">
        <button className="phone-back" onClick={onClose}>
          ‹ Back
        </button>
        <span className={`dot ${connected ? "at_desk" : "away"}`} />
      </div>
      <div className="phone-session-output">
        <pre>{text || "Waiting for output…"}</pre>
        <div ref={bottom} />
      </div>
      <div className="phone-session-quick">
        <button onClick={() => send("\x03")}>Interrupt</button>
        <button onClick={() => send("\x1b")}>Esc</button>
        <button onClick={() => send("y\n")}>Yes</button>
        <button onClick={() => send("n\n")}>No</button>
        <button onClick={() => send("\n")}>Enter</button>
      </div>
      <form
        className="phone-session-input"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Message the session…"
        />
        <button type="submit">Send</button>
      </form>
    </div>
  );
}
