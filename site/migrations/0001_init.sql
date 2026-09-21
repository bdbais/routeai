-- Community statistics. Nothing here identifies a person: an install is a sha256 of a random token,
-- a subject is an HMAC of a GitHub account id (or of the install itself, when anonymous).

CREATE TABLE IF NOT EXISTS installs (
  token_hash TEXT PRIMARY KEY,
  subject    TEXT NOT NULL,
  certified  INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  last_seen  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS installs_subject ON installs (subject);

CREATE TABLE IF NOT EXISTS results (
  token_hash   TEXT NOT NULL,
  model        TEXT NOT NULL,
  quant        TEXT NOT NULL,
  params       TEXT NOT NULL DEFAULT '',
  num_ctx      INTEGER NOT NULL,
  hw           TEXT NOT NULL,
  category     TEXT NOT NULL,
  score        REAL NOT NULL,
  gen_tps      REAL NOT NULL,
  gpu_ratio    REAL,
  runs         INTEGER NOT NULL DEFAULT 1,
  suite        TEXT NOT NULL,
  routeai      TEXT NOT NULL,
  os           TEXT NOT NULL DEFAULT '',
  outlier      INTEGER NOT NULL DEFAULT 0,
  submitted_at INTEGER NOT NULL,
  PRIMARY KEY (token_hash, model, quant, num_ctx, hw, category)
);
CREATE INDEX IF NOT EXISTS results_lookup ON results (model, quant, hw);

CREATE TABLE IF NOT EXISTS bans (
  subject    TEXT PRIMARY KEY,
  until      INTEGER,               -- NULL = permanent
  reason     TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  auto       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS strikes (
  subject TEXT NOT NULL,
  at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS strikes_subject ON strikes (subject, at);

CREATE TABLE IF NOT EXISTS rate (
  key    TEXT PRIMARY KEY,
  window INTEGER NOT NULL,
  count  INTEGER NOT NULL
);
