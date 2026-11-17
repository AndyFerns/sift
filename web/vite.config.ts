import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The dev server proxies API calls to a running `sift serve`.
// `npm run build` writes straight into the Python package so `sift serve` can serve it.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8765" },
  },
  build: {
    outDir: "../src/sift/api/static",
    emptyOutDir: true,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    restoreMocks: true,
    // jsdom role queries are slow on a busy machine; the default 5 s is too tight.
    testTimeout: 30_000,
  },
});
