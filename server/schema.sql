-- Dilmaç line pool (Cloudflare D1). Apply with:
--   npx wrangler d1 execute dilmac-pool --remote --file=schema.sql
-- Lines that no pack had when a player saw them. state: 0 waiting, 1 in a pack, 2 junk (OCR garbage).
CREATE TABLE IF NOT EXISTS lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  game TEXT NOT NULL,          -- normalized game title (textutil.text_key of the glossary's title)
  key TEXT NOT NULL,           -- normalized line (textutil.text_key)
  en TEXT NOT NULL,            -- the line as read (first reading)
  track TEXT NOT NULL DEFAULT 'speech',
  sources INTEGER NOT NULL DEFAULT 1,    -- how many different networks sent it (one person cannot fake several)
  trusted INTEGER NOT NULL DEFAULT 0,    -- sent by one of our own PCs (admin token)
  first_seen INTEGER NOT NULL,
  last_seen INTEGER NOT NULL,
  state INTEGER NOT NULL DEFAULT 0,
  UNIQUE (game, key)
);
CREATE INDEX IF NOT EXISTS lines_waiting ON lines (state, game, id);

-- Which network sent a line, so a line counts once per network. source = HMAC(POOL_SALT, IP): not reversible
-- without the server's secret; no IP, account, name or install id is stored.
CREATE TABLE IF NOT EXISTS sources (
  game TEXT NOT NULL,
  key TEXT NOT NULL,
  source TEXT NOT NULL,
  PRIMARY KEY (game, key, source)
);

-- Voice vectors of game characters (TitaNet, unit length) that players' apps learned on lines whose speaker the pack
-- knew. One row per game, speaker and network: the sum of what that network sent, so one network is one voice among
-- many. No audio, no IP. The nightly job checks each row against our own profile before using it.
CREATE TABLE IF NOT EXISTS voices (
  game TEXT NOT NULL,
  speaker TEXT NOT NULL,
  source TEXT NOT NULL,
  dim INTEGER NOT NULL,
  sum TEXT NOT NULL,           -- JSON array of dim numbers
  n INTEGER NOT NULL,
  updated INTEGER NOT NULL,
  PRIMARY KEY (game, speaker, source)
);

-- Lines per network per day (a flood is cut off); rows older than two days are deleted as new ones come.
CREATE TABLE IF NOT EXISTS quota (
  day TEXT NOT NULL,
  source TEXT NOT NULL,
  n INTEGER NOT NULL,
  PRIMARY KEY (day, source)
);
