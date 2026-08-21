import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The API is the Kestrel server, running in WSL alongside the agent. WSL2
// forwards localhost, so a browser on Windows reaches it without any bridge.
const API = "http://localhost:8099";

const proxied = [
  "/health",
  "/state",
  "/clients",
  "/tasks",
  "/events",
  "/deliveries",
  "/observations",
  "/memory",
  "/tick",
  "/sessions",
  "/hooks",
];

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // ws: true matters — the terminal is a WebSocket, and without it the
      // upgrade never reaches the server.
      "/terminals": { target: API, ws: true },
      ...Object.fromEntries(proxied.map((path) => [path, API])),
    },
  },
});
