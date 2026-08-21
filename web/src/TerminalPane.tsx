import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import { useEffect, useRef } from "react";
import { terminalSocket } from "./api";

// One xterm instance per terminal id, kept alive across tab switches. Tearing
// them down on every switch would lose the viewport and re-request scrollback
// for no reason.
interface Instance {
  term: Terminal;
  fit: FitAddon;
  socket: WebSocket;
  /** Which node this terminal is currently rendered into. Calling open() twice
   *  on the same Terminal re-attaches it and makes the shell redraw its prompt
   *  - which under StrictMode's double-mount looks like a corrupted session. */
  openedIn: HTMLElement | null;
  /** Last size actually sent. A resize that changes nothing still triggers a
   *  SIGWINCH and a prompt redraw, and redraws can nudge layout, which fires
   *  the observer again. That loop is what fills a terminal with garbage. */
  sent: { rows: number; cols: number } | null;
}

const instances = new Map<string, Instance>();

const THEME = {
  background: "#0d0f12",
  foreground: "#d8dee9",
  cursor: "#6ea8fe",
  selectionBackground: "#2f3641",
  black: "#0d0f12",
  red: "#e06c75",
  green: "#7cc98f",
  yellow: "#e0a355",
  blue: "#6ea8fe",
  magenta: "#c48ee0",
  cyan: "#6fc9c0",
  white: "#d8dee9",
};

function ensure(id: string): Instance {
  const existing = instances.get(id);
  if (existing) return existing;

  const term = new Terminal({
    fontFamily: '"JetBrains Mono", "Cascadia Code", ui-monospace, monospace',
    fontSize: 13,
    lineHeight: 1.25,
    cursorBlink: true,
    scrollback: 10000,
    theme: THEME,
  });
  const fit = new FitAddon();
  term.loadAddon(fit);

  const socket = terminalSocket(id);
  const instance: Instance = { term, fit, socket, openedIn: null, sent: null };

  socket.onmessage = (event) => {
    const data = String(event.data);
    // Output arrives as text frames; the exit notice is the one JSON frame.
    if (data.startsWith("{") && data.includes('"type":"exit"')) {
      const { code } = JSON.parse(data);
      term.write(`\r\n\x1b[2m[process exited with ${code}]\x1b[0m\r\n`);
      return;
    }
    term.write(data);
  };
  socket.onclose = () => term.write("\r\n\x1b[2m[detached]\x1b[0m\r\n");

  term.onData((data) => {
    if (socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: "input", data }));
    }
  });

  term.onResize(({ rows, cols }) => {
    if (instance.sent && instance.sent.rows === rows && instance.sent.cols === cols) return;
    instance.sent = { rows, cols };
    if (socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: "resize", rows, cols }));
    }
  });

  instances.set(id, instance);
  return instance;
}

export function disposeTerminal(id: string) {
  const instance = instances.get(id);
  if (!instance) return;
  instance.socket.close();
  instance.term.dispose();
  instances.delete(id);
}

export function TerminalPane({ id }: { id: string }) {
  const host = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const node = host.current;
    if (!node) return;

    const instance = ensure(id);
    if (instance.openedIn !== node) {
      instance.term.open(node);
      instance.openedIn = node;
    }
    instance.fit.fit();
    instance.term.focus();

    // Coalesce bursts of layout change into one fit, so a redraw caused by a
    // resize cannot immediately cause another resize.
    let frame = 0;
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => instance.fit.fit());
    });
    observer.observe(node);

    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [id]);

  return <div ref={host} style={{ height: "100%" }} />;
}
