import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
// Relative base so the built assets work when FastAPI serves them from any path.
export default defineConfig({
    plugins: [react()],
    base: "./",
    build: { outDir: "dist", emptyOutDir: true },
    server: {
        port: 5173,
        proxy: {
            // In dev, forward API + SSE calls to the FastAPI backend.
            "/api": {
                target: "http://127.0.0.1:8765",
                changeOrigin: true,
            },
        },
    },
});
