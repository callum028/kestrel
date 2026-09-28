// Web Push registration - the browser-side half of kestrel.push. Subscribing
// needs a user gesture in most browsers, so this is called from a button, not
// on load; the service worker (public/sw.js) handles the actual `push` event
// once a subscription exists, including with the tab closed.

function urlBase64ToUint8Array(base64: string): BufferSource {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const base64Safe = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64Safe);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes.buffer;
}

export function pushSupported(): boolean {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

export async function registerServiceWorker(): Promise<ServiceWorkerRegistration | null> {
  if (!("serviceWorker" in navigator)) return null;
  return navigator.serviceWorker.register("/sw.js");
}

export async function currentSubscription(): Promise<PushSubscription | null> {
  if (!pushSupported()) return null;
  const registration = await navigator.serviceWorker.ready;
  return registration.pushManager.getSubscription();
}

/** Asks for notification permission (if not already decided), then subscribes
 * with the server's VAPID public key and hands the subscription to the API.
 * Returns the outcome so a caller can show why it did not work - permission
 * denied and "not supported here" look identical from the button's point of
 * view but are different facts worth keeping apart. */
export async function subscribeToPush(
  vapidPublicKey: string,
  subscribe: (sub: PushSubscriptionJSON) => Promise<unknown>,
): Promise<"subscribed" | "denied" | "unsupported"> {
  if (!pushSupported()) return "unsupported";
  const permission = await Notification.requestPermission();
  if (permission !== "granted") return "denied";

  const registration = await navigator.serviceWorker.ready;
  const existing = await registration.pushManager.getSubscription();
  const subscription =
    existing ??
    (await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(vapidPublicKey),
    }));
  await subscribe(subscription.toJSON() as PushSubscriptionJSON);
  return "subscribed";
}

export async function unsubscribeFromPush(
  unsubscribe: (endpoint: string) => Promise<unknown>,
): Promise<void> {
  const subscription = await currentSubscription();
  if (!subscription) return;
  await unsubscribe(subscription.endpoint);
  await subscription.unsubscribe();
}
