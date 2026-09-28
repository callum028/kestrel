import { useEffect, useRef, useState } from "react";
import type { ConversationMessage } from "./api";

interface Props {
  messages: ConversationMessage[];
  onSend: (text: string) => Promise<unknown>;
  /** Phone home screen wants a taller, borderless thread; the desk rail wants
   * it to fit next to the deliveries feed. Same component, one layout knob. */
  compact?: boolean;
}

// The Kestrel chat. Role is what has to stay visually distinct everywhere
// this renders - a Kestrel line and a user line never share an alignment or
// a background, so "whose words are these" never depends on reading them.
export function ConversationPane({ messages, onSend, compact = false }: Props) {
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [messages.length]);

  const submit = async () => {
    const text = draft.trim();
    if (!text || sending) return;
    setDraft("");
    setSending(true);
    try {
      await onSend(text);
    } finally {
      setSending(false);
    }
  };

  return (
    <div className={`conversation${compact ? " compact" : ""}`}>
      <div className="conversation-thread">
        {messages.length === 0 ? (
          <p className="empty">Nothing said yet. Type below.</p>
        ) : (
          messages.map((m) => (
            <div key={m.id} className={`bubble ${m.role}`}>
              <div className="bubble-text">{m.text}</div>
              {m.refs.map((ref) => (
                <span key={`${ref.kind}-${ref.handle}`} className="bubble-ref">
                  {ref.handle}
                </span>
              ))}
            </div>
          ))
        )}
        <div ref={bottom} />
      </div>
      <form
        className="conversation-composer"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Talk to Kestrel…"
          disabled={sending}
        />
        <button type="submit" disabled={sending || !draft.trim()}>
          Send
        </button>
      </form>
    </div>
  );
}
