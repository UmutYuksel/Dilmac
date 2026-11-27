# Dilmaç pack server

A Cloudflare Worker (free plan) that is the one place packs are collected:

- **Pool (D1):** lines no pack had when someone played go here (`POST /v1/lines`), once per install. Only the English
  text, its normalized key, the game and a random install id are stored; no IP, account or name.
- **Packs (R2):** `index.json` and `packs/<id>.json.gz`. The app downloads changed packs at every start (at most every
  10 minutes) and checks them with SHA-256 (`pc/pack_server.py`).
- **Nightly job (on our own PC, not here):** the Legion runs `Dilmac.exe --nightly`. It reads the waiting lines with
  the admin token, translates them with Gemma (Ollama, RTX 3060) and publishes `pool-<game>` (`pc/nightly.py`).
  Hand-made packs (`pack_tool.py publish gta6`) stay ahead of the pool pack: for a line the first file wins, and
  `gta6.json` sorts before `pool-…`. "Update the packs" (`PACKS.md` in the top folder) has Claude review the pool's
  lines (Gemma's translation as a draft) into the hand-made pack, and `prune-pool` takes them out of the pool pack.
- **Voice profiles:** `voice_tool.py publish` puts the main PC's character profiles up as `voices-<game>` (a signed
  pack with no lines, `"voices": {speaker: {"sum", "n"}}`). Players' apps send the voice vector (192 numbers, not
  audio) of lines whose speaker the pack knew (`POST /v1/voices`, off with `"share_voices": false`; our PCs send
  none). The D1 table `voices` keeps one sum per game, speaker and network. Every pool run the Legion takes a
  network's row only if its average is close to our profile of that character and closer than to any other
  (`nightly.VOICE_ACCEPT`), counts one network as at most 50 clips, and publishes `voices-pool-<game>`. The app
  uses, per character, the profile with more clips (its own or a shared one), never their sum.

## Security

- **Packs are signed by us, not by the server.** Every pack carries an Ed25519 signature over
  `dilmac-pack-v1\n<id>\n<sha256 of the .json.gz>` made with a publisher key that only our own PCs have
  (`pc\pack-signing-key`, encrypted with DPAPI). The app accepts a pack only if a key in `pack_server.PUBLISHER_KEYS`
  signed it (and checks the sha256, the unpacked size and the content's shape). Someone who gets the Cloudflare account
  or the admin token still cannot put text on anyone's TV. The server refuses unsigned uploads too.
- **The pool cannot be flooded into a pack.** A line counts once per network (HMAC of the IP with `POOL_SALT`; no IP,
  account or install id is stored). The nightly job only takes lines at least two networks sent (`pool_min_sources`)
  or our own PCs sent (with the admin token). 3000 lines a day per network, 256 KB per request, 200 lines per request,
  keys and lines checked; the nightly job drops translations that bring links or addresses the English did not have.
- **The app's requests** are signed with a shared secret (`x-dilmac-time`, `x-dilmac-signature` =
  hex(HMAC-SHA256(APP_SECRET, METHOD + "\n" + path + "\n" + time)), ±5 min). The secret is in the app, so it only
  keeps casual callers out. HTTPS only (plain http only to 127.0.0.1 for tests). Admin calls need
  `Authorization: Bearer <admin token>`, compared in constant time. Each of our PCs has its own token, the Worker
  secret `ADMIN_TOKEN_<PC>` (`ADMIN_TOKEN_DESKTOP`, `ADMIN_TOKEN_LEGION`); `npx wrangler secret delete ADMIN_TOKEN_<PC>`
  shuts one PC out without touching the others.

**No English text goes out.** Every pack has two copies: the apps' copy is hashed (`pack.to_hashed`, `"format":
"hashed-v1"`: the Turkish under a hash of the normalized English line, MinHash of its letter trigrams for misread
lines) at `packs/<id>.json.gz`; the full copy with the English is at `full/<id>.json.gz`, given only with an admin
token (`GET /v1/admin/packs/:id/full`) and signed as `<id>#full`, so it cannot pass for the apps' copy. The app
secret is not in git: a build gets it at build time (`pc\_build_config.py`, git-ignored). Our own PCs need none;
their admin token also opens the app calls.

## API

| | Path | |
|---|---|---|
| app | `GET /v1/packs` (app signature or an admin token, for all three app calls) | `{"packs":[{id,game,title,version,sha256,size,entries,updated,auto}]}` |
| app | `GET /v1/packs/:id` | the `.json.gz`, `x-pack-sha256`, `etag` (`If-None-Match` → 304) |
| app | `POST /v1/lines` | `{"game","lines":[{"key","en","track"}]}`, at most 200 lines / 256 KB; with the admin token: trusted, no daily limit |
| app | `POST /v1/voices` | `{"game","voices":[{"speaker","v":[…]}]}`, unit vectors of 64–1024 numbers, at most 50 per request and 600 per network a day |
| admin | `GET /v1/admin/voices?game=` | `{"voices":[{game,speaker,source,n,sum}]}` |
| admin | `GET /v1/admin/lines?game=&after=&limit=&state=&min_sources=` | waiting lines (state 0) sent by `min_sources` networks (default 2) or trusted |
| admin | `POST /v1/admin/lines/state` | `{"ids":[…],"state":1\|2}` in a pack / junk |
| admin | `PUT /v1/admin/packs/:id` | body: `.json.gz`; headers `x-pack-game`, `x-pack-title` (percent-encoded), `x-pack-entries`, `x-pack-auto`, `x-pack-key`, `x-pack-signature` (required) |
| admin | `GET /v1/admin/packs/:id/full` | the full copy (`x-pack-variant: full` on the PUT uploads it, after the apps' copy) |
| admin | `DELETE /v1/admin/packs/:id` | both copies |

## Setting it up (once)

Node.js is needed for Cloudflare's `wrangler` tool (`npm install` in this folder installs the pinned version).

1. Create a free account at https://dash.cloudflare.com and sign in here: `npx wrangler login` (opens the browser).
   R2 must be turned on once in the dashboard (R2 Object Storage; the free plan asks for a payment method).
2. Create the database and the bucket; put the `database_id` it prints into `wrangler.toml`:
   ```powershell
   npx wrangler d1 create dilmac-pool
   npx wrangler r2 bucket create dilmac-packs
   npx wrangler d1 execute dilmac-pool --remote --file=schema.sql
   ```
3. `npx wrangler deploy` prints the address (`https://dilmac.<account>.workers.dev`; ours:
   https://dilmac.dilmac-server.workers.dev). A new workers.dev address needs a few minutes for its certificate.
4. Two random secrets: `npx wrangler secret put APP_SECRET` and `npx wrangler secret put POOL_SALT`. The address is
   `pack_server.DEFAULT_URL`. The app secret stays out of git: `"pack_secret"` in a PC's `pc\config.json` (encrypted
   on first start), or put into `DEFAULT_SECRET` only in a release build.
5. On each of our own PCs (needs `wrangler login` there): `py pc\pack_tool.py admin-token <PC>` makes that PC's admin
   token, uploads it as `ADMIN_TOKEN_<PC>` and stores it in its `config.json` (DPAPI); it is never printed.
6. Publish the hand-made pack once (and after every "update the pack"): `py pc\pack_tool.py publish gta6`. It is signed
   with this PC's key (`py pc\pack_tool.py keygen` made it; its public key is already in `PUBLISHER_KEYS`).

## The Legion (nightly translation)

Step by step for a Claude session on the Legion: `LEGION.md` in this folder. In short:

1. Ollama with the model in `local_model` (`ollama pull gemma4:e4b-it-qat`), `"engine": "local"` in its config.
2. A clone of the repo (or `Dilmac.exe` with its `pc\` folder) and `py pc\pack_tool.py admin-token LEGION`. Nothing
   secret is copied from another PC (DPAPI values only open for the Windows user that wrote them).
3. `"Dilmac.exe" --keygen` makes the Legion's own signing key and writes its public line to `pc\log.txt`. Add
   that line to `pack_server.PUBLISHER_KEYS` and rebuild the exe: without it the apps refuse the Legion's pool packs.
4. `"Dilmac.exe" --install-nightly` registers the Windows task "Dilmac gece paketi" (every 10 minutes, windowless
   and silent when the pool is empty; runs later if the laptop was off). `--nightly` runs it by hand; the result is in
   `pc\log.txt`.

## Development

```powershell
npm install
npm test          # the Worker in wrangler's local mode (workerd + local D1/R2), driven over HTTP
npm run dev       # local server; secrets from .dev.vars (APP_SECRET=…, ADMIN_TOKEN=… or ADMIN_TOKEN_<PC>=…, 32+ chars)
```

`pc\tests\test_pool_e2e.py` runs the whole chain against the local Worker: app queue → pool → nightly job (OPUS-MT) →
pool pack → another PC downloads and uses it.
