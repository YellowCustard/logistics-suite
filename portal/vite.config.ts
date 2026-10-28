/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Built into the Django app's static folder; apps/portal/views.py reads the manifest to link the hashed files.
export default defineConfig({
  plugins: [react()],
  base: "/static/portal/",
  build: {
    outDir: "../apps/portal/static/portal",
    emptyOutDir: true,
    manifest: true,
    sourcemap: true,
  },
  server: {
    // `npm run dev` against `manage.py runserver` on :8000: the API and the session cookie stay same-origin.
    proxy: { "/api": "http://127.0.0.1:8000", "/static/brand": "http://127.0.0.1:8000" },
  },
  test: {
    environment: "happy-dom",
    environmentOptions: { happyDOM: { url: "http://localhost/portal/" } },
    setupFiles: ["src/test/setup.ts"],
  },
});
