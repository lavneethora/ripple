import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5180,
    strictPort: true,
    // The API owns the Relate connection, so the browser never talks to the
    // store directly.
    proxy: { "/api": { target: "http://localhost:8010", changeOrigin: true } },
  },
});
