# The pool PC: setting up the pool job (instructions for a Claude session)

The pool job runs on one of our own PCs with a GPU, here a Lenovo Legion laptop (RTX 3060) that is always on. These
are the steps for a Claude Code session on that PC. **Talk to the user in Turkish.** Code, files and names are in
English.

## Why

Dilmaç reads the English subtitles of PS5 games, translates them into Turkish and shows them on the TV. Packs (ready
translations) are collected on the pack server (`server/README.md`). The apps send lines no pack had to the pool;
**every 10 minutes** this PC translates the waiting lines with Gemma on its GPU, publishes them as the signed
`pool-<game>` pack (`pc/nightly.py`) and merges the pool's voice vectors into `voices-pool-<game>`. In the evening
"update the packs" (`PACKS.md` in the top folder) reviews them into the hand-made pack.

No secret is copied onto this PC: it makes its own admin token and its own signing key.

## Do not touch

- Other virtual machines and services on this PC: leave them, their network bridges and power settings alone; ask
  the user before a restart.
- Never print, paste or commit a key or token. `pc/config.json` and `pc/pack-signing-key` are git-ignored.

## Steps

1. **Repos.** Clone outside any synced folder, e.g. `git clone https://github.com/UmutYuksel/Dilmac.git C:\Dilmac`,
   and the private packs into its `pc\packs`: `git clone https://github.com/UmutYuksel/Dilmac-packs.git
   C:\Dilmac\pc\packs`. If git asks for a login (or hangs at the command line), run
   `git credential-manager github login --device --no-ui`; the user enters the code at github.com/login/device.
2. **Python 3.12** (`winget install Python.Python.3.12`), then `py -m pip install -r pc\requirements.txt` and the tests:
   `cd pc; py -m unittest discover -s tests -q` (all must pass; the ones needing Node or a model skip themselves).
3. **Ollama and the model.** `winget install Ollama.Ollama`, then `ollama pull gemma4:e4b-it-qat` (a few GB). Check it
   runs on the GPU (`ollama run gemma4:e4b-it-qat "Merhaba"`, memory in `nvidia-smi`).
4. **Settings.** `pc\config.json`: `{"engine": "local", "local_model": "gemma4:e4b-it-qat"}`. The server address is
   built in (`pack_server.DEFAULT_URL`); no app secret is needed here, the admin token opens the app calls too.
   `"pool_min_sources"`: how many networks must have sent a line before the job takes it (default 2; 1 while only a
   few friends use the app, back to 2 for a public release).
5. **Node + wrangler.** `winget install OpenJS.NodeJS.LTS` (tools Claude installs may land in its sandboxed folder and
   be invisible to the user's shell: use a real folder such as `C:\Users\<user>\dilmac-tools`). `cd server; npm ci`,
   then the **user** runs `npx wrangler login` (browser; run it in the user's terminal tab if it hangs here).
6. **This PC's admin token.** `py pc\pack_tool.py admin-token LEGION`: uploads a random token as the Worker secret
   `ADMIN_TOKEN_LEGION` and stores it DPAPI-encrypted in the config, never printed.
7. **This PC's signing key.** `py pc\pack_tool.py keygen`, then add the printed `"<kid>": "<public key>",` line to
   `PUBLISHER_KEYS` in `pc\pack_server.py`, commit and push (ask the user first). Without it the apps refuse this PC's
   packs. The private key (`pc\pack-signing-key`) never leaves the PC.
8. **A run by hand.** `py pc\nightly.py`; `pc\log.txt` should end with "Gece paketi bitti" and "Ses havuzu".
9. **The scheduled task.** `py pc\nightly.py --install-nightly`: Windows task "Dilmac gece paketi" every 10 minutes
   (`pythonw.exe --quiet`: no window, nothing logged for an empty pool). Re-run it if Python moves.
10. **The evening review (optional).** A scheduled task of the Claude desktop app, every evening:
    `C:\Dilmac klasöründe PACKS.md'yi oku ve "Paketleri güncelle" akışını uygula. Bekleyen satır yoksa hiçbir şey
    değiştirme. Bitince kısa özet yaz.` Run it once by hand ("Run now") to grant its permissions.

## Afterwards

- A short summary for the user: what was installed, the tests, when the task runs next.
- The code runs from the clone: `git pull` (both repos) is all an update needs.
- To shut a PC out: `npx wrangler secret delete ADMIN_TOKEN_<NAME>` (in `server`).
