import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: proxy /api to the FastAPI server so the SPA and backend share an origin (no CORS).
// Prod: `vite build` emits static assets FastAPI can serve later.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
