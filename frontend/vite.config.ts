import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the backend runs separately on :8000; proxy API calls to it.
// In production FastAPI serves the built files from this folder's dist/.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: process.env.VITE_API_URL ?? "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
