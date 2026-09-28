import { useEffect, useState } from "react";
import { api } from "./api";
import { currentSubscription, pushSupported, subscribeToPush, unsubscribeFromPush } from "./push";

/** One button: subscribe or unsubscribe this browser install for Web Push.
 * Notification permission is a per-device, per-browser thing - there is no
 * server-side "is this device subscribed" to read on mount without asking the
 * browser first, so this checks locally rather than trusting cached state. */
export function PushToggle() {
  const [subscribed, setSubscribed] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!pushSupported()) return;
    currentSubscription()
      .then((sub) => setSubscribed(sub !== null))
      .catch(() => undefined);
  }, []);

  if (!pushSupported()) return null;

  const toggle = async () => {
    setBusy(true);
    try {
      if (subscribed) {
        await unsubscribeFromPush((endpoint) => api.unsubscribePush(endpoint));
        setSubscribed(false);
      } else {
        const { public_key } = await api.vapidPublicKey();
        const outcome = await subscribeToPush(public_key, (sub) => api.subscribePush(sub));
        if (outcome === "subscribed") setSubscribed(true);
        else if (outcome === "denied") window.alert("Notifications were blocked in the browser.");
      }
    } catch {
      window.alert("Couldn't reach Kestrel to update notifications.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <button className="push-toggle" onClick={toggle} disabled={busy} title="Web Push notifications">
      {subscribed ? "Notifications on" : "Enable notifications"}
    </button>
  );
}
