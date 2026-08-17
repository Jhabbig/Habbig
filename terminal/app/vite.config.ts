import { defineConfig } from "vite";

// Tauri v2 + plain TypeScript, no framework.
// clearScreen off so tauri-cli output stays visible; strictPort because
// tauri.conf.json build.devUrl pins http://localhost:5173.
export default defineConfig({
  clearScreen: false,
  server: {
    port: 5173,
    strictPort: true,
  },
  build: {
    target: "es2022",
    outDir: "dist",
    // Keep font/logo assets as files (never inlined past 4k) so the
    // print/PDF path and @font-face resolution stay boring.
    assetsInlineLimit: 4096,
  },
});
