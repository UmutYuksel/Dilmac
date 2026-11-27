// SPDX-License-Identifier: AGPL-3.0-only
// The Worker in wrangler's local mode (workerd + local D1/R2 in a temporary folder), driven over HTTP.
import { spawn, spawnSync } from "node:child_process";
import { createHash, createHmac } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync, existsSync, unlinkSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync, gunzipSync } from "node:zlib";
import { test, before, after } from "node:test";
import assert from "node:assert/strict";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const PORT = 8799;
const BASE = `http://127.0.0.1:${PORT}`;
const APP_SECRET = "test-app-secret";
const ADMIN = "test-admin-token-main-pc-0123456789";
const ADMIN2 = "test-admin-token-legion-0123456789";
const persist = mkdtempSync(join(tmpdir(), "dilmac-server-"));
const vars = join(ROOT, ".dev.vars");
const hadVars = existsSync(vars) ? readFileSync(vars) : null;
let dev;

const wrangler = (args, opts = {}) =>
  spawnSync(process.execPath, [join(ROOT, "node_modules", "wrangler", "bin", "wrangler.js"), ...args], { cwd: ROOT, encoding: "utf8", ...opts });

function signed(method, path, extra = {}) {
  const time = String(Math.floor(Date.now() / 1000));
  const sig = createHmac("sha256", APP_SECRET).update(`${method}\n${path}\n${time}`).digest("hex");
  return { "x-dilmac-time": time, "x-dilmac-signature": sig, ...extra };
}
const admin = (extra = {}) => ({ authorization: `Bearer ${ADMIN}`, ...extra });

before(async () => {
  writeFileSync(vars, `APP_SECRET=${APP_SECRET}\nADMIN_TOKEN=${ADMIN}\nADMIN_TOKEN_LEGION=${ADMIN2}\nADMIN_TOKEN_SHORT=short\n`);
  const r = wrangler(["d1", "execute", "dilmac-pool", "--local", "--persist-to", persist, "--file", "schema.sql"]);
  assert.equal(r.status, 0, r.stderr || r.stdout);
  dev = spawn(process.execPath, [join(ROOT, "node_modules", "wrangler", "bin", "wrangler.js"), "dev", "--local", "--port", String(PORT),
    "--persist-to", persist, "--ip", "127.0.0.1", "--show-interactive-dev-session=false"], { cwd: ROOT });
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("wrangler dev did not start")), 60000);
    const check = async () => {
      try {
        if ((await fetch(`${BASE}/v1/health`)).ok) { clearTimeout(timer); return resolve(); }
      } catch { /* not up yet */ }
      setTimeout(check, 500);
    };
    check();
  });
});

after(() => {
  dev?.kill();
  if (hadVars) writeFileSync(vars, hadVars); else if (existsSync(vars)) unlinkSync(vars);
  try { rmSync(persist, { recursive: true, force: true }); } catch { /* workerd may still hold files */ }
});

test("unsigned and wrongly signed requests are refused", async () => {
  assert.equal((await fetch(`${BASE}/v1/packs`)).status, 403);
  const bad = signed("GET", "/v1/packs");
  bad["x-dilmac-signature"] = "0".repeat(64);
  assert.equal((await fetch(`${BASE}/v1/packs`, { headers: bad })).status, 403);
  const old = signed("GET", "/v1/packs");
  old["x-dilmac-time"] = String(Math.floor(Date.now() / 1000) - 3600);
  assert.equal((await fetch(`${BASE}/v1/packs`, { headers: old })).status, 403);
  assert.equal((await fetch(`${BASE}/v1/admin/lines`)).status, 403);
  assert.equal((await fetch(`${BASE}/v1/admin/lines`, { headers: { authorization: "Bearer nope" } })).status, 403);
  assert.equal((await fetch(`${BASE}/v1/admin/lines`, { headers: { authorization: "Bearer short" } })).status, 403);
});

test("every PC's own admin token (ADMIN_TOKEN_<PC>) is accepted, for the app calls too", async () => {
  for (const token of [ADMIN, ADMIN2]) {
    const authorization = `Bearer ${token}`;
    assert.equal((await fetch(`${BASE}/v1/admin/lines?game=grandtheftautovi`, { headers: { authorization } })).status, 200);
    assert.equal((await fetch(`${BASE}/v1/packs`, { headers: { authorization } })).status, 200);  // no app secret needed
  }
  assert.equal((await fetch(`${BASE}/v1/packs`, { headers: { authorization: "Bearer short" } })).status, 403);
});

const GAME = "grandtheftautovi";
const postLines = (lines, { ip = "198.51.100.1", trusted = false, game = GAME } = {}) => fetch(`${BASE}/v1/lines`, {
  method: "POST",
  headers: signed("POST", "/v1/lines", { "content-type": "application/json", "cf-connecting-ip": ip, ...(trusted ? admin() : {}) }),
  body: JSON.stringify({ game, lines }),
});
const waitingLines = async (q = "") => (await (await fetch(`${BASE}/v1/admin/lines?game=${GAME}${q}`, { headers: admin() })).json()).lines;

test("a line counts once per network; only lines several networks (or our PCs) sent reach the nightly job", async () => {
  const a = { key: "weneedtogo", en: "We need to go.", track: "speech" };
  const b = { key: "losethecops", en: "Lose the cops.", track: "objective" };
  const c = { key: "ourownline", en: "Our own line.", track: "speech" };
  const json = async (r) => { assert.equal(r.status, 200); return r.json(); };
  assert.deepEqual(await json(await postLines([a, b])), { ok: true, added: 2 });
  assert.deepEqual(await json(await postLines([a])), { ok: true, added: 0 });            // same network again
  assert.deepEqual(await json(await postLines([a], { ip: "203.0.113.9" })), { ok: true, added: 1 });
  assert.deepEqual(await json(await postLines([c], { trusted: true })), { ok: true, added: 1 });
  const waiting = await waitingLines();  // default: at least 2 networks, or trusted
  assert.deepEqual(waiting.map((l) => l.en).sort(), ["Our own line.", "We need to go."]);
  assert.equal(waiting.find((l) => l.en === "We need to go.").sources, 2);
  assert.equal(waiting.find((l) => l.en === "Our own line.").trusted, 1);
  assert.equal((await waitingLines("&min_sources=1")).length, 3);  // "Lose the cops." was sent by one network only
  const go = waiting.find((l) => l.en === "We need to go.");
  const r = await fetch(`${BASE}/v1/admin/lines/state`, { method: "POST", headers: admin({ "content-type": "application/json" }), body: JSON.stringify({ ids: [go.id], state: 1 }) });
  assert.equal(r.status, 200);
  assert.deepEqual((await waitingLines()).map((l) => l.en), ["Our own line."]);
});

test("malformed, oversized and flooding senders are cut off", async () => {
  assert.equal((await postLines([{ key: "a b", en: "x" }], { game: "Bad Game!" })).status, 400);
  const skipped = await (await postLines([{ key: "has space", en: "x" }, { key: "ok", en: "y" }, { key: "fine", en: "z".repeat(500) }], { ip: "192.0.2.50" })).json();
  assert.equal(skipped.added, 0);  // bad key, too short key, too long line
  const big = await fetch(`${BASE}/v1/lines`, {
    method: "POST", headers: signed("POST", "/v1/lines", { "content-type": "application/json", "cf-connecting-ip": "192.0.2.51" }),
    body: JSON.stringify({ game: GAME, lines: [], pad: "x".repeat(300 * 1024) }),
  });
  assert.equal(big.status, 413);
  const batch = (n) => Array.from({ length: 200 }, (_, i) => ({ key: `flood${n}x${i}`, en: `Flood ${n} ${i}.` }));
  let last;
  for (let n = 0; n < 16 && (!last || last.status === 200); n++) last = await postLines(batch(n), { ip: "192.0.2.77" });
  assert.equal(last.status, 429);  // 3000 lines a day per network
  assert.equal((await postLines(batch(99), { ip: "192.0.2.77", trusted: true })).status, 200);  // our PCs: no limit
});

test("voice vectors are summed per network and speaker; bad ones are dropped", async () => {
  const unit = (i) => { const v = Array(192).fill(0); v[i] = 1; return v; };
  const send = (voices, ip) => fetch(`${BASE}/v1/voices`, {
    method: "POST", headers: signed("POST", "/v1/voices", { "content-type": "application/json", "cf-connecting-ip": ip }),
    body: JSON.stringify({ game: GAME, voices }),
  }).then((r) => r.json());
  assert.deepEqual(await send([{ speaker: "Lucia", v: unit(0) }, { speaker: "Lucia", v: unit(1) }, { speaker: "Jason", v: unit(2) }], "198.51.100.20"), { ok: true, added: 3 });
  assert.deepEqual(await send([{ speaker: "Lucia", v: unit(0) }], "198.51.100.20"), { ok: true, added: 1 });   // same network: same row
  assert.deepEqual(await send([{ speaker: "Lucia", v: unit(3) }], "203.0.113.20"), { ok: true, added: 1 });    // another network
  const bad = [{ speaker: "Lucia", v: unit(0).map((x) => x * 5) }, { speaker: "<b>x</b>", v: unit(0) }, { speaker: "Lucia", v: [1, 2] }, { speaker: "Lucia", v: unit(0).map(() => "x") }];
  assert.deepEqual(await send(bad, "198.51.100.21"), { ok: true, added: 0 });
  assert.equal((await fetch(`${BASE}/v1/admin/voices?game=${GAME}`)).status, 403);
  const rows = (await (await fetch(`${BASE}/v1/admin/voices?game=${GAME}`, { headers: admin() })).json()).voices;
  const lucia = rows.filter((r) => r.speaker === "Lucia").sort((a, b) => b.n - a.n);
  assert.equal(lucia.length, 2);
  assert.equal(lucia[0].n, 3);
  assert.deepEqual(lucia[0].sum.slice(0, 2), [2, 1]);
  assert.equal(rows.find((r) => r.speaker === "Jason").n, 1);
});

test("a pack's full copy is kept apart and given only with an admin token", async () => {
  const gz = gzipSync(Buffer.from(JSON.stringify({ game: "G", format: "hashed-v1", lines: {}, h: { aaaa: "x" } })));
  const fullGz = gzipSync(Buffer.from(JSON.stringify({ game: "G", lines: { "Secret English line.": "x" } })));
  const sig = "A".repeat(86) + "==";
  const put = (body, extra = {}) => fetch(`${BASE}/v1/admin/packs/fulltest`, {
    method: "PUT", body, headers: admin({ "x-pack-game": "G", "x-pack-entries": "1", "x-pack-key": "c1a34893f549810f", "x-pack-signature": sig, ...extra }),
  });
  assert.equal((await put(fullGz, { "x-pack-variant": "full" })).status, 409);  // the app's copy comes first
  assert.equal((await put(gz)).status, 200);
  const r = await (await put(fullGz, { "x-pack-variant": "full" })).json();
  assert.equal(r.pack.version, 1);  // the full copy does not count as a new version
  assert.equal(r.pack.full.sha256, createHash("sha256").update(fullGz).digest("hex"));
  const app = await fetch(`${BASE}/v1/packs/fulltest`, { headers: signed("GET", "/v1/packs/fulltest") });
  assert.ok(!gunzipSync(Buffer.from(await app.arrayBuffer())).toString().includes("Secret English"));
  assert.equal((await fetch(`${BASE}/v1/admin/packs/fulltest/full`, { headers: signed("GET", "/v1/admin/packs/fulltest/full") })).status, 403);
  const adm = await fetch(`${BASE}/v1/admin/packs/fulltest/full`, { headers: admin() });
  assert.ok(gunzipSync(Buffer.from(await adm.arrayBuffer())).toString().includes("Secret English"));
  assert.equal((await (await put(gz)).json()).pack.full.sha256, r.pack.full.sha256);  // kept on re-upload
  assert.equal((await fetch(`${BASE}/v1/admin/packs/fulltest`, { method: "DELETE", headers: admin() })).status, 200);
  assert.equal((await fetch(`${BASE}/v1/admin/packs/fulltest/full`, { headers: admin() })).status, 404);
});

test("a published pack is listed, downloaded and checked; unchanged upload keeps the version", async () => {
  const pack = { game: "Grand Theft Auto VI", source: "pool", lines: { "We need to go.": "Gitmemiz lazım." } };
  const gz = gzipSync(Buffer.from(JSON.stringify(pack)));
  const put = () => fetch(`${BASE}/v1/admin/packs/pool-gta6`, {
    method: "PUT", body: gz,
    headers: admin({ "x-pack-game": "Grand Theft Auto VI", "x-pack-title": encodeURIComponent("GTA VI (havuz, Türkçe)"), "x-pack-entries": "1", "x-pack-auto": "1",
      "x-pack-key": "c1a34893f549810f", "x-pack-signature": "A".repeat(86) + "==" }),
  }).then((r) => r.json());
  const unsigned = await fetch(`${BASE}/v1/admin/packs/pool-gta6`, { method: "PUT", body: gz, headers: admin() });
  assert.equal(unsigned.status, 400);  // never stored without a publisher signature
  assert.equal((await (await fetch(`${BASE}/v1/packs`, { headers: signed("GET", "/v1/packs") })).json()).packs.length, 0);
  const first = await put();
  assert.equal(first.pack.version, 1);
  assert.equal((await put()).pack.version, 1);
  const index = await (await fetch(`${BASE}/v1/packs`, { headers: signed("GET", "/v1/packs") })).json();
  const entry = index.packs.find((p) => p.id === "pool-gta6");
  assert.equal(entry.auto, true);
  assert.equal(entry.title, "GTA VI (havuz, Türkçe)");
  assert.equal(entry.entries, 1);
  assert.equal(entry.key, "c1a34893f549810f");
  assert.equal(entry.signature.length, 88);
  const res = await fetch(`${BASE}/v1/packs/pool-gta6`, { headers: signed("GET", "/v1/packs/pool-gta6") });
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("x-pack-sha256"), entry.sha256);
  const body = Buffer.from(await res.arrayBuffer());
  assert.deepEqual(JSON.parse(gunzipSync(body).toString("utf8")), pack);
  const again = await fetch(`${BASE}/v1/packs/pool-gta6`, { headers: signed("GET", "/v1/packs/pool-gta6", { "if-none-match": res.headers.get("etag") }) });
  assert.equal(again.status, 304);
  const bad = await fetch(`${BASE}/v1/admin/packs/x`, { method: "PUT", body: "not gzip", headers: admin() });
  assert.equal(bad.status, 400);
  await fetch(`${BASE}/v1/admin/packs/pool-gta6`, { method: "DELETE", headers: admin() });
  const after = await (await fetch(`${BASE}/v1/packs`, { headers: signed("GET", "/v1/packs") })).json();
  assert.equal(after.packs.length, 0);
});
