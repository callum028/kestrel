import { useEffect, useState } from "react";

/** Re-renders on cross, not just on resize - a phone rotated or a desktop
 * window dragged narrow both have to flip layouts without a reload. */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);

  useEffect(() => {
    const mql = window.matchMedia(query);
    const onChange = () => setMatches(mql.matches);
    onChange();
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);

  return matches;
}

// One breakpoint, matching the product split: desk gets tabs and terminals,
// phone gets the chat-first layout. There is no tablet-specific layout in v1.
export const PHONE_QUERY = "(max-width: 720px)";
