import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite writes the build into ../backend/app/static. FastAPI serves index.html
// from there and mounts /assets onto the same directory. `emptyOutDir` is
// explicit because the outDir is outside Vite's project root.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  build: {
    outDir: path.resolve(__dirname, "../backend/app/static"),
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/health": "http://localhost:8001",
      "/auth": "http://localhost:8001",
      "/tasks": "http://localhost:8001",
      "/inputs": "http://localhost:8001",
      "/sources": "http://localhost:8001",
      "/oauth": "http://localhost:8001",
      "/labels": "http://localhost:8001",
      "/notifications": "http://localhost:8001",
      "/settings": "http://localhost:8001",
    },
  },
});
