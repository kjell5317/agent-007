import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite writes the build into ../src/app/static. FastAPI serves index.html
// from there and mounts /assets onto the same directory. `emptyOutDir` is
// explicit because the outDir is outside Vite's project root.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  build: {
    outDir: path.resolve(__dirname, "../src/app/static"),
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/health": "http://localhost:8000",
      "/auth": "http://localhost:8000",
      "/tasks": "http://localhost:8000",
      "/inputs": "http://localhost:8000",
      "/sources": "http://localhost:8000",
      "/oauth": "http://localhost:8000",
      "/labels": "http://localhost:8000",
      "/settings": "http://localhost:8000",
    },
  },
});
