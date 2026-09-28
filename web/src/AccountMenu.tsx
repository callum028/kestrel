import { useEffect, useRef, useState } from "react";

interface Props {
  onUnpair: () => void;
}

/** Unpairing is rare and destructive (this device stops being able to reach
 * Kestrel at all until paired again) - it belongs behind a deliberate tap in
 * an overflow menu, not sitting fixed on top of whatever else is on screen.
 * Same component on desk (in the topbar) and phone (in the slim header), so
 * there is exactly one place either surface hides it. */
export function AccountMenu({ onUnpair }: Props) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onClickAway = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onClickAway);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClickAway);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="account-menu" ref={root}>
      <button
        type="button"
        className="account-menu-trigger"
        aria-label="Menu"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        ⋯
      </button>
      {open && (
        <div className="account-menu-panel" role="menu">
          <button
            type="button"
            role="menuitem"
            className="account-menu-item"
            onClick={() => {
              setOpen(false);
              if (window.confirm("Unpair this device? You'll need a new pairing link to use it again.")) {
                onUnpair();
              }
            }}
          >
            Unpair this device
          </button>
        </div>
      )}
    </div>
  );
}
