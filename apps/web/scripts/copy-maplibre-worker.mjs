// MapLibre GL 6 loads its web worker from a URL. After bundling, the default
// (`./maplibre-gl-worker.mjs` relative to the library module) does not exist, so the worker is served
// from public/ and selected with maplibregl.setWorkerUrl (see src/components/MapView.tsx).
import { copyFileSync, mkdirSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";

const require = createRequire(import.meta.url);
const dist = dirname(require.resolve("maplibre-gl/package.json"));
mkdirSync("public/vendor", { recursive: true });
copyFileSync(join(dist, "dist/maplibre-gl-worker.mjs"), "public/vendor/maplibre-gl-worker.mjs");
console.log("copied maplibre-gl-worker.mjs → public/vendor/");
