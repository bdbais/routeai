// Builds the showcase at routeai.bais.info into site/dist.
// No dependencies: node site/build.mjs
//
// English lives at the root, every other language under /<code>/, the same
// set as the other bais.info showcases. The build fails when a language
// misses a text, carries an unknown one, or the template asks for a key that
// does not exist — so a half-translated page can never be published.
import { copyFileSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const ORIGIN = "https://routeai.bais.info";
const REPO_URL = "https://github.com/bdbais/routeai";
const DEFAULT = "en";
const LANGS = ["en", "it", "es", "fr", "de", "pt", "tr", "ru", "uk", "ar", "zh", "ja", "ko"];

const version = JSON.parse(readFileSync(join(here, "..", ".claude-plugin", "plugin.json"), "utf8")).version;
const shared = {
  version,
  repo_url: REPO_URL,
  install_url: `${REPO_URL}#install`,
  donate_url: "https://paypal.me/bellizia",
};

const template = readFileSync(join(here, "template.html"), "utf8");
const texts = Object.fromEntries(
  LANGS.map((code) => [code, JSON.parse(readFileSync(join(here, "i18n", `${code}.json`), "utf8"))]),
);

const errors = [];
const reference = Object.keys(texts[DEFAULT]);
for (const code of LANGS) {
  const keys = Object.keys(texts[code]);
  for (const k of reference) if (!keys.includes(k)) errors.push(`${code}: missing "${k}"`);
  for (const k of keys) if (!reference.includes(k)) errors.push(`${code}: unknown "${k}"`);
  for (const f of ["lang", "name", "dir", "ogLocale"]) {
    if (!texts[code]._meta?.[f]) errors.push(`${code}: _meta.${f} missing`);
  }
}
if (errors.length) fail(errors);

const pageUrl = (code) => (code === DEFAULT ? `${ORIGIN}/` : `${ORIGIN}/${code}/`);
const pagePath = (code) => (code === DEFAULT ? "/" : `/${code}/`);
const alternates = [
  ...LANGS.map((c) => `<link rel="alternate" hreflang="${texts[c]._meta.lang}" href="${pageUrl(c)}">`),
  `<link rel="alternate" hreflang="x-default" href="${pageUrl(DEFAULT)}">`,
].join("\n");

const dist = join(here, "dist");
rmSync(dist, { recursive: true, force: true });

for (const code of LANGS) {
  const t = texts[code];
  const menu = LANGS.map((c) => {
    const current = c === code ? ' aria-current="page"' : "";
    return `<li><a href="${pagePath(c)}" hreflang="${texts[c]._meta.lang}" lang="${texts[c]._meta.lang}"${current}>${texts[c]._meta.name}</a></li>`;
  }).join("");
  const vars = {
    ...shared,
    ...Object.fromEntries(Object.entries(t).filter(([k]) => k !== "_meta")),
    lang: t._meta.lang,
    dir: t._meta.dir,
    og_locale: t._meta.ogLocale,
    lang_name: t._meta.name,
    lang_menu: menu,
    canonical: pageUrl(code),
    alternates,
  };
  const html = template.replace(/\{\{([a-z0-9_]+)\}\}/g, (m, key) => {
    if (!(key in vars)) {
      errors.push(`${code}: template uses unknown {{${key}}}`);
      return m;
    }
    return vars[key];
  });
  const folder = code === DEFAULT ? dist : join(dist, code);
  mkdirSync(folder, { recursive: true });
  writeFileSync(join(folder, "index.html"), html);
}
if (errors.length) fail(errors);

copyFileSync(join(here, "_headers"), join(dist, "_headers"));
writeFileSync(join(dist, "robots.txt"), `User-agent: *\nAllow: /\nSitemap: ${ORIGIN}/sitemap.xml\n`);
writeFileSync(
  join(dist, "sitemap.xml"),
  `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n` +
    LANGS.map((c) => `  <url><loc>${pageUrl(c)}</loc></url>`).join("\n") +
    `\n</urlset>\n`,
);
console.log(`site built: ${LANGS.length} languages, version ${version} -> ${dist}`);

function fail(list) {
  console.error(list.join("\n"));
  process.exit(1);
}
