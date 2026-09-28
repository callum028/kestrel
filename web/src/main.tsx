import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { registerServiceWorker } from "./push";
import "@xterm/xterm/css/xterm.css";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);

// Registered unconditionally (it is a no-op without a subscription) so the
// app shell is cached and installable from the first load, before anyone has
// opted into notifications.
registerServiceWorker().catch(() => undefined);
