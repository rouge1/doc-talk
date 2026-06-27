import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Test-only config, deliberately separate from vite.config.ts so `npm run dev` / `build` never load
// vitest or its deps. jsdom gives the DOM + localStorage the cache and router tests need.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
  },
});
