// SPDX-License-Identifier: AGPL-3.0-only
// Dilmaç pack server (Cloudflare Worker, D1 + R2). See README.md.
//
// App (signed with APP_SECRET, see sign(), or with an admin token):
//   GET  /v1/packs            {"packs":[{id,game,title,version,sha256,size,entries,updated,auto}]}  (R2 index.json)
//   GET  /v1/packs/:id        the pack (packs/<id>.json.gz), headers x-pack-sha256 and etag; If-None-Match -> 304
//   POST /v1/lines            {"install","game","lines":[{"key","en","track"}]}: lines no pack had (the pool)
//   POST /v1/voices           {"game","voices":[{"speaker","v":[...]}]}: voice vectors of known speakers
// Nightly job and pack_tool (Authorization: Bearer <admin token>). Each of our PCs has its own token, a secret
// named ADMIN_TOKEN or ADMIN_TOKEN_<PC> (e.g. ADMIN_TOKEN_LEGION); deleting one secret shuts that PC out.
//   GET  /v1/admin/lines?game=&after=&limit=&state=   waiting lines (state 0), oldest first
//   GET  /v1/admin/voices?game=                        voice rows: {speaker, source, n, sum}
//   POST /v1/admin/lines/state                         {"ids":[...],"state":1|2}  in a pack / junk
//   PUT  /v1/admin/packs/:id                           body: the .json.gz; headers x-pack-game, x-pack-title,
//                                                      x-pack-entries, x-pack-auto ("1" for pool packs);
//                                                      x-pack-variant: full -> the copy with the English text
//   GET  /v1/admin/packs/:id/full                      that copy (only our PCs; the apps get the hashed one)
//   DELETE /v1/admin/packs/:id
// GET /v1/health -> "ok"

const MAX_LINES = 200;         // per POST /v1/lines
const MAX_EN = 400;            // characters of one line
const MAX_BODY = 256 * 1024;   // bytes of one POST /v1/lines
const DAILY_LINES = 3000;      // lines per network per day (our own PCs, with the admin token, have no limit)
const CONTROL = /[\u0000-\u001f\u007f‪-‮⁦-⁩]/g;
const MAX_PACK = 20 * 1024 * 1024;
const SKEW = 5 * 60;           // accepted clock difference of a signed request (s)
const MAX_VOICES = 50;          // vectors per POST /v1/voices
const DAILY_VOICES = 600;      // vectors per network per day
const VOICE_DIM = [64, 1024];  // length of one vector
const SPEAKER = /^[\p{L}\p{N}' .-]{1,40}$/u;
const LIMITS = { packs: 120, lines: 60, voices: 60 };  // requests per IP per hour (per Worker instance; a rough brake)
const hits = new Map();

const json = (data, status = 200, headers = {}) =>
  new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json; charset=utf-8", ...headers } });
const text = (body, status) => new Response(body, { status, headers: { "content-type": "text/plain; charset=utf-8" } });

function hex(buf) {
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function hmac(secret, message) {
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return hex(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message)));
}

async function sha256(bytes) {
  return hex(await crypto.subtle.digest("SHA-256", bytes));
}

/** Constant-time comparison of two hex strings. */
function same(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/** x-dilmac-signature = hex(HMAC-SHA256(APP_SECRET, METHOD + "\n" + path + "\n" + x-dilmac-time)). */
async function signedOk(request, env, path) {
  if (!env.APP_SECRET) return false;
  const time = request.headers.get("x-dilmac-time") || "";
  const signature = request.headers.get("x-dilmac-signature") || "";
  const t = Number(time);
  if (!Number.isInteger(t) || Math.abs(Date.now() / 1000 - t) > SKEW) return false;
  return same(signature, await hmac(env.APP_SECRET, `${request.method}\n${path}\n${time}`));
}

const ADMIN_SECRET = /^ADMIN_TOKEN(_[A-Z0-9]{1,32})?$/;

function adminOk(request, env) {
  const auth = request.headers.get("authorization") || "";
  let ok = false;
  for (const [name, value] of Object.entries(env)) {
    // every token is compared (no early return), each in constant time; short tokens are not accepted
    if (ADMIN_SECRET.test(name) && typeof value === "string" && value.length >= 32 && same(auth, `Bearer ${value}`)) ok = true;
  }
  return ok;
}

function limited(request, kind) {
  const ip = request.headers.get("cf-connecting-ip") || "local";
  const hour = Math.floor(Date.now() / 3600000);
  const k = `${kind}:${ip}:${hour}`;
  const n = (hits.get(k) || 0) + 1;
  hits.set(k, n);
  if (hits.size > 5000) for (const old of hits.keys()) if (!old.endsWith(`:${hour}`)) hits.delete(old);
  return n > LIMITS[kind];
}

async function readIndex(env) {
  const obj = await env.PACKS.get("index.json");
  if (!obj) return { packs: [] };
  try {
    return JSON.parse(await obj.text());
  } catch {
    return { packs: [] };
  }
}

const PACK_ID = /^[a-z0-9][a-z0-9-]{0,63}$/;

// ------------------------------------------------------------ app

async function getIndex(env) {
  return json(await readIndex(env), 200, { "cache-control": "no-cache" });
}

async function getPack(request, env, id) {
  if (!PACK_ID.test(id)) return text("not found", 404);
  const obj = await env.PACKS.get(`packs/${id}.json.gz`);
  if (!obj) return text("not found", 404);
  const sha = obj.customMetadata?.sha256 || "";
  const etag = `"${sha}"`;
  if (request.headers.get("if-none-match") === etag) return new Response(null, { status: 304, headers: { etag } });
  return new Response(obj.body, {
    headers: { "content-type": "application/gzip", "x-pack-sha256": sha, etag, "cache-control": "no-cache" },
  });
}

/** The sender's network, not reversible without POOL_SALT: a line counts once per network, and one person cannot
 * make a line look popular by making up install ids. */
async function sourceOf(request, env) {
  const ip = request.headers.get("cf-connecting-ip") || "local";
  return (await hmac(env.POOL_SALT || env.ADMIN_TOKEN || "", `ip:${ip}`)).slice(0, 32);
}

async function postLines(request, env, trusted) {
  const raw = await request.arrayBuffer();
  if (raw.byteLength > MAX_BODY) return text("too large", 413);
  let body;
  try {
    body = JSON.parse(new TextDecoder().decode(raw));
  } catch {
    return text("bad json", 400);
  }
  const game = String(body.game || "").slice(0, 120);
  const lines = Array.isArray(body.lines) ? body.lines.slice(0, MAX_LINES) : [];
  if (!/^[\p{L}\p{N}]{1,120}$/u.test(game)) return text("bad request", 400);
  const source = await sourceOf(request, env);
  const now = Math.floor(Date.now() / 1000);
  const day = new Date().toISOString().slice(0, 10);
  if (!trusted) {
    const q = await env.DB.prepare(
      `INSERT INTO quota (day, source, n) VALUES (?, ?, ?)
       ON CONFLICT (day, source) DO UPDATE SET n = n + excluded.n RETURNING n`,
    ).bind(day, source, lines.length).first();
    if (q && q.n > DAILY_LINES) return text("daily limit", 429);
    if (Math.random() < 0.02) {
      const old = new Date(Date.now() - 2 * 86400000).toISOString().slice(0, 10);
      await env.DB.prepare("DELETE FROM quota WHERE day < ?").bind(old).run();
    }
  }
  let added = 0;
  for (const line of lines) {
    const key = String(line.key || "");
    const en = String(line.en || "").replace(CONTROL, "").trim();
    const track = line.track === "objective" ? "objective" : "speech";
    if (!/^[\p{L}\p{N}]{3,400}$/u.test(key) || !en || en.length > MAX_EN) continue;
    const seen = await env.DB.prepare("INSERT OR IGNORE INTO sources (game, key, source) VALUES (?, ?, ?)")
      .bind(game, key, source).run();
    if (!seen.meta.changes && !trusted) continue;  // this network already sent it
    await env.DB.prepare(
      `INSERT INTO lines (game, key, en, track, sources, trusted, first_seen, last_seen) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
       ON CONFLICT (game, key) DO UPDATE SET sources = sources + ?, trusted = MAX(trusted, excluded.trusted),
       last_seen = excluded.last_seen`,
    ).bind(game, key, en, track, trusted ? 1 : 0, now, now, seen.meta.changes ? 1 : 0).run();
    added++;
  }
  return json({ ok: true, added });
}

/** Voice vectors: summed per (game, speaker, network); unit-length check so one request cannot outweigh others. */
async function postVoices(request, env) {
  const raw = await request.arrayBuffer();
  if (raw.byteLength > MAX_BODY) return text("too large", 413);
  let body;
  try {
    body = JSON.parse(new TextDecoder().decode(raw));
  } catch {
    return text("bad json", 400);
  }
  const game = String(body.game || "").slice(0, 120);
  if (!/^[\p{L}\p{N}]{1,120}$/u.test(game)) return text("bad request", 400);
  const items = Array.isArray(body.voices) ? body.voices.slice(0, MAX_VOICES) : [];
  const bySpeaker = new Map();
  for (const it of items) {
    const speaker = String(it?.speaker || "").replace(CONTROL, "").trim();
    const v = it?.v;
    if (!SPEAKER.test(speaker) || !Array.isArray(v) || v.length < VOICE_DIM[0] || v.length > VOICE_DIM[1]) continue;
    if (!v.every((x) => typeof x === "number" && Number.isFinite(x))) continue;
    const norm = Math.sqrt(v.reduce((a, x) => a + x * x, 0));
    if (norm < 0.9 || norm > 1.1) continue;  // the app sends unit vectors
    const cur = bySpeaker.get(speaker);
    if (cur && cur.sum.length !== v.length) continue;
    if (cur) { cur.sum = cur.sum.map((a, i) => a + v[i]); cur.n++; } else bySpeaker.set(speaker, { sum: [...v], n: 1 });
  }
  const count = [...bySpeaker.values()].reduce((a, x) => a + x.n, 0);
  if (!count) return json({ ok: true, added: 0 });
  const source = await sourceOf(request, env);
  const day = new Date().toISOString().slice(0, 10);
  const q = await env.DB.prepare(
    `INSERT INTO quota (day, source, n) VALUES (?, ?, ?)
     ON CONFLICT (day, source) DO UPDATE SET n = n + excluded.n RETURNING n`,
  ).bind(day, `v:${source}`, count).first();
  if (q && q.n > DAILY_VOICES) return text("daily limit", 429);
  const now = Math.floor(Date.now() / 1000);
  for (const [speaker, add] of bySpeaker) {
    const row = await env.DB.prepare("SELECT dim, sum, n FROM voices WHERE game = ? AND speaker = ? AND source = ?")
      .bind(game, speaker, source).first();
    let sum = add.sum, n = add.n;
    if (row) {
      if (row.dim !== sum.length) continue;
      const old = JSON.parse(row.sum);
      sum = old.map((a, i) => a + sum[i]);
      n += row.n;
    }
    await env.DB.prepare(
      `INSERT INTO voices (game, speaker, source, dim, sum, n, updated) VALUES (?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT (game, speaker, source) DO UPDATE SET sum = excluded.sum, n = excluded.n, updated = excluded.updated`,
    ).bind(game, speaker, source, sum.length, JSON.stringify(sum.map((x) => Math.round(x * 1e5) / 1e5)), n, now).run();
  }
  return json({ ok: true, added: count });
}

// ------------------------------------------------------------ admin

async function adminVoices(url, env) {
  const game = url.searchParams.get("game") || "";
  const q = game
    ? env.DB.prepare("SELECT game, speaker, source, n, sum FROM voices WHERE game = ? ORDER BY speaker, source").bind(game)
    : env.DB.prepare("SELECT game, speaker, source, n, sum FROM voices ORDER BY game, speaker, source");
  const { results } = await q.all();
  return json({ voices: results.map((r) => ({ ...r, sum: JSON.parse(r.sum) })) });
}

async function adminLines(url, env) {
  const game = url.searchParams.get("game") || "";
  const after = Number(url.searchParams.get("after") || 0);
  const limit = Math.min(500, Math.max(1, Number(url.searchParams.get("limit") || 200)));
  const state = Number(url.searchParams.get("state") || 0);
  // Only lines several networks sent, or our own PCs sent: a single sender cannot get text into a pack.
  const minSources = Math.max(1, Number(url.searchParams.get("min_sources") || 2));
  const cols = "id, game, en, track, sources, trusted, first_seen";
  const q = game
    ? env.DB.prepare(`SELECT ${cols} FROM lines WHERE state = ? AND game = ? AND id > ? AND (trusted = 1 OR sources >= ?) ORDER BY id LIMIT ?`).bind(state, game, after, minSources, limit)
    : env.DB.prepare(`SELECT ${cols} FROM lines WHERE state = ? AND id > ? AND (trusted = 1 OR sources >= ?) ORDER BY id LIMIT ?`).bind(state, after, minSources, limit);
  const { results } = await q.all();
  return json({ lines: results, next: results.length === limit ? results[results.length - 1].id : null });
}

async function adminState(request, env) {
  const body = await request.json().catch(() => ({}));
  const ids = (Array.isArray(body.ids) ? body.ids : []).map(Number).filter(Number.isInteger).slice(0, 1000);
  const state = Number(body.state);
  if (![0, 1, 2].includes(state)) return text("bad state", 400);
  for (let i = 0; i < ids.length; i += 90) {
    const part = ids.slice(i, i + 90);
    await env.DB.prepare(`UPDATE lines SET state = ? WHERE id IN (${part.map(() => "?").join(",")})`).bind(state, ...part).run();
  }
  return json({ ok: true, updated: ids.length });
}

/** The full copy of a pack (with the English lines): kept apart, given only with an admin token. */
async function putFullPack(request, env, id, bytes, keyId, signature) {
  const index = await readIndex(env);
  const entry = index.packs.find((p) => p.id === id);
  if (!entry) return text("publish the app's copy first", 409);
  const sha = await sha256(bytes);
  await env.PACKS.put(`full/${id}.json.gz`, bytes, { customMetadata: { sha256: sha } });
  // Signed over "dilmac-pack-v1\n<id>#full\n<sha256>", so it cannot be passed off as the app's copy.
  entry.full = { sha256: sha, size: bytes.byteLength, key: keyId, signature };
  await env.PACKS.put("index.json", JSON.stringify(index));
  return json({ ok: true, pack: entry });
}

async function getFullPack(env, id) {
  if (!PACK_ID.test(id)) return text("not found", 404);
  const obj = await env.PACKS.get(`full/${id}.json.gz`);
  if (!obj) return text("not found", 404);
  return new Response(obj.body, {
    headers: { "content-type": "application/gzip", "x-pack-sha256": obj.customMetadata?.sha256 || "", "cache-control": "no-store" },
  });
}

async function putPack(request, env, id) {
  if (!PACK_ID.test(id)) return text("bad id", 400);
  const bytes = await request.arrayBuffer();
  if (!bytes.byteLength || bytes.byteLength > MAX_PACK) return text("bad size", 400);
  const head = new Uint8Array(bytes, 0, 2);
  if (head[0] !== 0x1f || head[1] !== 0x8b) return text("not gzip", 400);
  const keyId = request.headers.get("x-pack-key") || "";
  const signature = request.headers.get("x-pack-signature") || "";
  if (!/^[0-9a-f]{16}$/.test(keyId) || !/^[A-Za-z0-9+/=]{80,100}$/.test(signature)) return text("unsigned pack", 400);
  if (request.headers.get("x-pack-variant") === "full") return putFullPack(request, env, id, bytes, keyId, signature);
  const sha = await sha256(bytes);
  await env.PACKS.put(`packs/${id}.json.gz`, bytes, { customMetadata: { sha256: sha } });
  const index = await readIndex(env);
  const old = index.packs.find((p) => p.id === id);
  // Header values are percent-encoded by the client (titles may have non-ASCII letters).
  const header = (name) => {
    const v = request.headers.get(name);
    try { return v ? decodeURIComponent(v) : ""; } catch { return v || ""; }
  };
  const entry = {
    id,
    game: header("x-pack-game") || old?.game || "",
    title: header("x-pack-title") || old?.title || id,
    version: (old?.version || 0) + (old?.sha256 === sha ? 0 : 1),
    sha256: sha,
    size: bytes.byteLength,
    entries: Number(request.headers.get("x-pack-entries") || 0),
    updated: new Date().toISOString(),
    auto: request.headers.get("x-pack-auto") === "1",
    // The publisher's Ed25519 signature over "dilmac-pack-v1\n<id>\n<sha256>"; the apps check it, not the server.
    key: keyId,
    signature,
  };
  if (old?.full) entry.full = old.full;  // replaced by the full copy's own upload right after
  index.packs = [...index.packs.filter((p) => p.id !== id), entry].sort((a, b) => a.id.localeCompare(b.id));
  await env.PACKS.put("index.json", JSON.stringify(index));
  return json({ ok: true, pack: entry });
}

async function deletePack(env, id) {
  if (!PACK_ID.test(id)) return text("bad id", 400);
  await env.PACKS.delete(`packs/${id}.json.gz`);
  await env.PACKS.delete(`full/${id}.json.gz`);
  const index = await readIndex(env);
  index.packs = index.packs.filter((p) => p.id !== id);
  await env.PACKS.put("index.json", JSON.stringify(index));
  return json({ ok: true });
}

// ------------------------------------------------------------ routing

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;
    const m = request.method;
    try {
      if (path === "/v1/health") return text("ok", 200);
      if (path.startsWith("/v1/admin/")) {
        if (!adminOk(request, env)) return text("forbidden", 403);
        if (m === "GET" && path === "/v1/admin/lines") return adminLines(url, env);
        if (m === "GET" && path === "/v1/admin/voices") return adminVoices(url, env);
        if (m === "POST" && path === "/v1/admin/lines/state") return adminState(request, env);
        const full = path.match(/^\/v1\/admin\/packs\/([^/]+)\/full$/);
        if (full && m === "GET") return getFullPack(env, full[1]);
        const pack = path.match(/^\/v1\/admin\/packs\/([^/]+)$/);
        if (pack && m === "PUT") return putPack(request, env, pack[1]);
        if (pack && m === "DELETE") return deletePack(env, pack[1]);
        return text("not found", 404);
      }
      // the app signs with APP_SECRET; our own PCs may use their admin token instead (they need no app secret)
      if (!(await signedOk(request, env, path)) && !adminOk(request, env)) return text("forbidden", 403);
      if (m === "GET" && path === "/v1/packs") {
        if (limited(request, "packs")) return text("too many requests", 429);
        return getIndex(env);
      }
      const pack = path.match(/^\/v1\/packs\/([^/]+)$/);
      if (m === "GET" && pack) {
        if (limited(request, "packs")) return text("too many requests", 429);
        return getPack(request, env, pack[1]);
      }
      if (m === "POST" && path === "/v1/lines") {
        const trusted = adminOk(request, env);  // our own PCs send with the admin token as well
        if (!trusted && limited(request, "lines")) return text("too many requests", 429);
        return postLines(request, env, trusted);
      }
      if (m === "POST" && path === "/v1/voices") {
        if (limited(request, "voices")) return text("too many requests", 429);
        return postVoices(request, env);
      }
      return text("not found", 404);
    } catch (err) {
      console.log(JSON.stringify({ error: String(err), path }));
      return text("server error", 500);
    }
  },
};
