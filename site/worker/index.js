// routeai.bais.info: the static showcase, plus the community statistics people send with `send-stats`.
//
// What is stored: benchmark numbers, the model they came from, a hardware bucket, and two hashes -
// sha256 of the install token, and an HMAC of the GitHub account id (or of the install itself when the
// submitter stayed anonymous). No IP address, no name, no email, no machine name.

import { Invalid, aggregate, escapeHtml, isOutlier, renderTable, validatePayload } from "./lib.js";

const SECURITY_HEADERS = {
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "strict-origin-when-cross-origin",
  "X-Frame-Options": "DENY",
  "Permissions-Policy": "geolocation=(), microphone=(), camera=(), interest-cohort=()",
  "Content-Security-Policy":
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
};

const DAY = 86400;
const LIMITS = { registerAnon: 3, registerCertified: 10, submitPerInstall: 10, submitPerIp: 30 };
const MIN_ACCOUNT_AGE_DAYS = 30;
const AUTO_BAN_STRIKES = 3;
const AUTO_BAN_DAYS = 7;
const STRIKE_WINDOW_DAYS = 30;
const PLACEHOLDER = "<!--ROUTEAI_COMMUNITY-->";

const json = (status, body) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", ...SECURITY_HEADERS },
  });

const now = () => Math.floor(Date.now() / 1000);

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function hmacHex(secret, text) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" },
    false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(text));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 32);
}

function bearer(request) {
  const header = request.headers.get("Authorization") || "";
  const match = /^Bearer\s+([A-Za-z0-9._~+/=-]{20,200})$/.exec(header.trim());
  return match ? match[1] : null;
}

function constantTimeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// -- rate limiting: counters keyed by a daily hash, so no address is ever stored ---------------

async function hitLimit(env, kind, value, max) {
  const window = Math.floor(now() / DAY);
  const key = `${kind}:${await hmacHex(env.SUBJECT_SECRET, `${kind}|${value}|${window}`)}`;
  await env.DB.prepare(
    "INSERT INTO rate (key, window, count) VALUES (?1, ?2, 1) " +
    "ON CONFLICT(key) DO UPDATE SET count = count + 1, window = ?2",
  ).bind(key, window).run();
  const row = await env.DB.prepare("SELECT count FROM rate WHERE key = ?").bind(key).first();
  return (row?.count || 0) > max;
}

async function activeBan(env, subject) {
  return env.DB.prepare("SELECT until, reason FROM bans WHERE subject = ? AND (until IS NULL OR until > ?)")
    .bind(subject, now()).first();
}

function banned(ban) {
  const until = ban.until ? new Date(ban.until * 1000).toISOString().slice(0, 10) : null;
  return json(403, {
    error: until ? `blocked until ${until}: ${ban.reason}` : `blocked: ${ban.reason}`,
    until, reason: ban.reason,
  });
}

// -- GitHub identity ---------------------------------------------------------------------------

async function githubSubject(env, token) {
  const api = env.GITHUB_API || "https://api.github.com";
  const resp = await fetch(`${api}/user`, {
    headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json", "User-Agent": "routeai-community" },
  });
  if (!resp.ok) throw new Invalid("GitHub did not accept that sign-in");
  const user = await resp.json();
  if (!user || typeof user.id !== "number" || user.type !== "User") throw new Invalid("unexpected GitHub account");
  const ageDays = (Date.now() - Date.parse(user.created_at || 0)) / (DAY * 1000);
  if (!(ageDays >= MIN_ACCOUNT_AGE_DAYS)) {
    throw new Invalid(`GitHub accounts younger than ${MIN_ACCOUNT_AGE_DAYS} days cannot certify submissions`);
  }
  return `gh:${await hmacHex(env.SUBJECT_SECRET, `github:${user.id}`)}`;
}

// -- endpoints ----------------------------------------------------------------------------------

async function register(request, env) {
  const token = bearer(request);
  if (!token) return json(401, { error: "missing install token" });
  const tokenHash = await sha256Hex(token);
  let body = {};
  try {
    body = await request.json();
  } catch { /* an empty body means anonymous */ }

  const existing = await env.DB.prepare("SELECT subject, certified FROM installs WHERE token_hash = ?")
    .bind(tokenHash).first();
  let subject, certified;
  if (body && body.github_token) {
    try {
      subject = await githubSubject(env, String(body.github_token));
    } catch (err) {
      return json(401, { error: err instanceof Invalid ? err.message : "sign-in failed" });
    }
    certified = 1;
    if (existing && existing.certified && existing.subject !== subject) {
      return json(409, { error: "this install is already certified by another GitHub account" });
    }
  } else {
    subject = existing?.subject || `anon:${await hmacHex(env.SUBJECT_SECRET, `install:${tokenHash}`)}`;
    certified = existing?.certified || 0;
  }

  const ban = await activeBan(env, subject);
  if (ban) return banned(ban);
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  if (await hitLimit(env, "register", ip, certified ? LIMITS.registerCertified : LIMITS.registerAnon)) {
    return json(429, { error: "too many registrations from here today" });
  }

  await env.DB.prepare(
    "INSERT INTO installs (token_hash, subject, certified, created_at, last_seen) VALUES (?1, ?2, ?3, ?4, ?4) " +
    "ON CONFLICT(token_hash) DO UPDATE SET subject = ?2, certified = ?3, last_seen = ?4",
  ).bind(tokenHash, subject, certified, now()).run();
  return json(200, { certified: !!certified });
}

async function submit(request, env) {
  const token = bearer(request);
  if (!token) return json(401, { error: "missing install token" });
  const tokenHash = await sha256Hex(token);
  const install = await env.DB.prepare("SELECT subject, certified FROM installs WHERE token_hash = ?")
    .bind(tokenHash).first();
  if (!install) return json(401, { error: "unknown install: run send-stats again to register" });

  const ban = await activeBan(env, install.subject);
  if (ban) return banned(ban);
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  if (await hitLimit(env, "submit-install", tokenHash, LIMITS.submitPerInstall) ||
      await hitLimit(env, "submit-ip", ip, LIMITS.submitPerIp)) {
    return json(429, { error: "too many submissions today, try again tomorrow" });
  }

  let payload;
  try {
    payload = validatePayload(await request.json());
  } catch (err) {
    return json(400, { error: err instanceof Invalid ? err.message : "invalid submission" });
  }

  const stamp = now();
  let outliers = 0;
  const inserts = [];
  for (const row of payload.rows) {
    const peers = await env.DB.prepare(
      "SELECT r.gen_tps AS tps FROM results r JOIN installs i ON i.token_hash = r.token_hash " +
      "LEFT JOIN bans b ON b.subject = i.subject AND (b.until IS NULL OR b.until > ?4) " +
      "WHERE r.model = ?1 AND r.quant = ?2 AND r.hw = ?3 AND r.outlier = 0 AND b.subject IS NULL " +
      "AND i.subject != ?5 LIMIT 500",
    ).bind(row.model, row.quant, row.hw, stamp, install.subject).all();
    const values = (peers.results || []).map((r) => r.tps);
    const flagged = isOutlier(row.gen_tps, values) ? 1 : 0;
    outliers += flagged;
    inserts.push(env.DB.prepare(
      "INSERT INTO results (token_hash, model, quant, params, num_ctx, hw, category, score, gen_tps, gpu_ratio, " +
      "runs, suite, routeai, os, outlier, submitted_at) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16)",
    ).bind(tokenHash, row.model, row.quant, row.params, row.num_ctx, row.hw, row.category, row.score, row.gen_tps,
      row.gpu_ratio, row.runs, payload.suite, payload.routeai, payload.os, flagged, stamp));
  }

  await env.DB.batch([
    env.DB.prepare("DELETE FROM results WHERE token_hash = ?").bind(tokenHash),
    ...inserts,
    env.DB.prepare("UPDATE installs SET last_seen = ? WHERE token_hash = ?").bind(stamp, tokenHash),
  ]);

  let autoBan = null;
  if (outliers) {
    await env.DB.prepare("INSERT INTO strikes (subject, at) VALUES (?, ?)").bind(install.subject, stamp).run();
    const since = stamp - STRIKE_WINDOW_DAYS * DAY;
    const count = await env.DB.prepare("SELECT COUNT(*) AS n FROM strikes WHERE subject = ? AND at > ?")
      .bind(install.subject, since).first();
    if ((count?.n || 0) >= AUTO_BAN_STRIKES) {
      autoBan = stamp + AUTO_BAN_DAYS * DAY;
      await env.DB.prepare(
        "INSERT INTO bans (subject, until, reason, created_at, auto) VALUES (?1, ?2, ?3, ?4, 1) " +
        "ON CONFLICT(subject) DO UPDATE SET until = ?2, reason = ?3",
      ).bind(install.subject, autoBan, "repeated results far out of scale", stamp).run();
    }
  }

  return json(200, {
    accepted: payload.rows.length, outliers, certified: !!install.certified,
    blocked_until: autoBan ? new Date(autoBan * 1000).toISOString().slice(0, 10) : null,
  });
}

async function remove(request, env) {
  const token = bearer(request);
  if (!token) return json(401, { error: "missing install token" });
  const tokenHash = await sha256Hex(token);
  const result = await env.DB.prepare("DELETE FROM results WHERE token_hash = ?").bind(tokenHash).run();
  return json(200, { deleted: result.meta?.changes ?? 0 });
}

async function readAggregates(env) {
  const rows = await env.DB.prepare(
    "SELECT i.certified AS certified, i.subject AS subject, r.model, r.quant, r.params, r.hw, r.category, " +
    "r.score, r.gen_tps FROM results r JOIN installs i ON i.token_hash = r.token_hash " +
    "LEFT JOIN bans b ON b.subject = i.subject AND (b.until IS NULL OR b.until > ?1) " +
    "WHERE r.outlier = 0 AND b.subject IS NULL LIMIT 20000",
  ).bind(now()).all();
  const groups = aggregate(rows.results || []);
  return {
    certified: groups.filter((g) => g.certified),
    uncertified: groups.filter((g) => !g.certified),
  };
}

async function communityJson(env) {
  const data = await readAggregates(env);
  return new Response(JSON.stringify({ generated_at: new Date().toISOString(), ...data }), {
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "public, max-age=300", ...SECURITY_HEADERS },
  });
}

async function communityPage(request, env) {
  const asset = await env.ASSETS.fetch(request);
  if (!asset.ok) return asset;
  const html = await asset.text();
  const labels = JSON.parse(/<!--ROUTEAI_LABELS (.*?)-->/s.exec(html)?.[1] || "{}");
  const data = await readAggregates(env);
  const body = `<h3 class="sub">${escapeHtml(labels.certified_title || "Certified")}</h3>` +
    `<p class="note">${escapeHtml(labels.certified_text || "")}</p>` +
    renderTable(data.certified, labels) +
    `<h3 class="sub">${escapeHtml(labels.uncertified_title || "Not certified")}</h3>` +
    `<p class="note">${escapeHtml(labels.uncertified_text || "")}</p>` +
    renderTable(data.uncertified, labels);
  return new Response(html.replace(PLACEHOLDER, body), {
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "public, max-age=120", ...SECURITY_HEADERS },
  });
}

async function admin(request, env, path) {
  if (!env.ADMIN_TOKEN || !constantTimeEqual(bearer(request) || "", env.ADMIN_TOKEN)) {
    return json(401, { error: "admin token required" });
  }
  if (path === "/api/admin/submissions") {
    const rows = await env.DB.prepare(
      "SELECT i.subject, i.certified, r.model, r.quant, r.hw, r.category, r.score, r.gen_tps, r.outlier, " +
      "r.submitted_at FROM results r JOIN installs i ON i.token_hash = r.token_hash " +
      "ORDER BY r.submitted_at DESC LIMIT 200",
    ).all();
    return json(200, { submissions: rows.results || [] });
  }
  const body = await request.json().catch(() => ({}));
  const subject = String(body.subject || "");
  if (!/^(gh|anon):[0-9a-f]{8,64}$/.test(subject)) return json(400, { error: "expected a subject id" });
  if (path === "/api/admin/ban") {
    const until = body.days ? now() + Math.round(Number(body.days)) * DAY : null;
    await env.DB.prepare(
      "INSERT INTO bans (subject, until, reason, created_at, auto) VALUES (?1, ?2, ?3, ?4, 0) " +
      "ON CONFLICT(subject) DO UPDATE SET until = ?2, reason = ?3, auto = 0",
    ).bind(subject, until, String(body.reason || "manual").slice(0, 200), now()).run();
    return json(200, { subject, until });
  }
  if (path === "/api/admin/unban") {
    await env.DB.prepare("DELETE FROM bans WHERE subject = ?").bind(subject).run();
    await env.DB.prepare("DELETE FROM strikes WHERE subject = ?").bind(subject).run();
    return json(200, { subject, banned: false });
  }
  return json(404, { error: "unknown admin action" });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";
    try {
      if (path === "/api/register" && request.method === "POST") return await register(request, env);
      if (path === "/api/stats" && request.method === "POST") return await submit(request, env);
      if (path === "/api/stats" && request.method === "DELETE") return await remove(request, env);
      if (path === "/api/community.json" && request.method === "GET") return await communityJson(env);
      if (path.startsWith("/api/admin/")) return await admin(request, env, path);
      if (path === "/api" || path.startsWith("/api/")) return json(404, { error: "unknown endpoint" });
      if (/^(\/[a-z-]{2,5})?\/community$/.test(path) && request.method === "GET") {
        return await communityPage(request, env);
      }
    } catch (err) {
      return json(500, { error: `server error: ${err && err.message ? err.message : err}` });
    }
    return env.ASSETS.fetch(request);
  },
};
