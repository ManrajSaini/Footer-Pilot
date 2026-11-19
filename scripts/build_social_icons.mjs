// Regenerates the social icon PNGs in app/static/social/ (white glyph on a neutral
// grey circle, 48x48 px, displayed at 22 px in signatures).
//
// Only needed when adding a network or restyling the set; the PNGs are committed.
// Glyphs come from the Simple Icons project (CC0). LinkedIn is not in that set, so it
// is drawn as an "in" lettermark.
//
// Run from any folder that has the two packages installed:
//   npm install --no-save simple-icons @resvg/resvg-js
//   node <repo>/scripts/build_social_icons.mjs

import { mkdirSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const require = createRequire(pathToFileURL(join(process.cwd(), "noop.js")));
const icons = require("simple-icons");
const { Resvg } = require("@resvg/resvg-js");

const SIZE = 48;
const BACKGROUND = "#4b5563";
const OUTPUT = join(dirname(fileURLToPath(import.meta.url)), "..", "app", "static", "social");

const glyph = (icon) => `<g transform="translate(12 12)"><path d="${icon.path}" fill="#fff"/></g>`;
const NETWORKS = {
  linkedin: `<text x="24" y="33" font-family="Arial" font-weight="700" font-size="26" text-anchor="middle" fill="#fff">in</text>`,
  x: glyph(icons.siX),
  facebook: glyph(icons.siFacebook),
  instagram: glyph(icons.siInstagram),
  youtube: glyph(icons.siYoutube),
  github: glyph(icons.siGithub),
};

mkdirSync(OUTPUT, { recursive: true });
for (const [name, body] of Object.entries(NETWORKS)) {
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="${SIZE}" height="${SIZE}" viewBox="0 0 ${SIZE} ${SIZE}">` +
    `<circle cx="24" cy="24" r="24" fill="${BACKGROUND}"/>${body}</svg>`;
  const png = new Resvg(svg, { font: { loadSystemFonts: true } }).render().asPng();
  writeFileSync(join(OUTPUT, `${name}.png`), png);
  console.log(`${name}.png ${png.length} bytes`);
}
