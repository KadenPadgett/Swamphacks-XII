import { build } from "esbuild";
import { readFile, writeFile } from "node:fs/promises";
const result = await build({
  entryPoints: ["src/main.jsx"],
  bundle: true,
  format: "iife",
  minify: true,
  write: false,
  outfile: "preview.js",
  loader: { ".png": "dataurl", ".svg": "dataurl" },
  define: { "process.env.NODE_ENV": '"production"' },
});
const js = result.outputFiles.find((f) => f.path.endsWith(".js")).text;
const css = result.outputFiles.find((f) => f.path.endsWith(".css")).text;
const land = await readFile("public/land.json", "utf8");
const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#172e27"><title>PlanIT Control · Interactive preview</title><style>${css}</style></head><body><div id="root"></div><script>window.__PLANIT_LAND__=${land};</script><script>${js.replaceAll("</script", "<\\/script")}</script></body></html>`;
await writeFile("PlanIT-Preview.html", html);
console.log(
  `Standalone preview: ${(Buffer.byteLength(html) / 1024 / 1024).toFixed(2)} MiB`,
);
