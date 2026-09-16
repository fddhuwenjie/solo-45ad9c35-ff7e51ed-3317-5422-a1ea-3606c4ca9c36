"""SQLite schema and connection helpers for the shield closure workbench."""
import hashlib
import json
import os
import sqlite3

DB_PATH = os.environ.get(
    "CLOSURE_DB", os.path.join(os.path.dirname(__file__), "closure.db")
)

SCHEMA = """
PRAGMA foreign_keys = ON;

-- Coordinate-system versions (survey frames). base_version_id is the frame
-- a confirmed version was referenced against; the canonical frame is 'BASE'.
CREATE TABLE IF NOT EXISTS coord_versions (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('base','guide','segment','resurvey')),
    family      TEXT,                 -- guide/segment frames of the same era
    base_version_id TEXT REFERENCES coord_versions(id),
    confirmed   INTEGER NOT NULL DEFAULT 0,
    confirmed_at TEXT,
    note        TEXT
);

-- Control points (CP).  base_x/base_y are their coordinates in the canonical
-- frame; NULL until the frame chain proves them.
CREATE TABLE IF NOT EXISTS control_points (
    code    TEXT PRIMARY KEY,
    base_x  REAL,
    base_y  REAL,
    note    TEXT
);

-- Total-station setups.  base_x/base_y are given/anchored positions in the
-- canonical frame.
CREATE TABLE IF NOT EXISTS stations (
    code    TEXT PRIMARY KEY,
    base_x  REAL NOT NULL,
    base_y  REAL NOT NULL,
    note    TEXT
);

-- Station moves: S(n) takes over from S(n-1) at move_time.
CREATE TABLE IF NOT EXISTS station_moves (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    from_station TEXT NOT NULL REFERENCES stations(code),
    to_station   TEXT NOT NULL REFERENCES stations(code),
    move_time    TEXT NOT NULL,
    UNIQUE(from_station, to_station)
);

-- Observations.
--   kind='guide'/'segment': absolute (x,y) of ring ring_no in frame version_id
--   kind='resurvey': vector (vx,vy) from station -> target control point,
--                    measured in the resurvey frame version_id
-- content_hash makes import idempotent: re-importing the same row never
-- adds a second observation; changed weight re-binds the same row.
CREATE TABLE IF NOT EXISTS observations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash TEXT NOT NULL UNIQUE,
    kind         TEXT NOT NULL CHECK (kind IN ('guide','segment','resurvey')),
    version_id   TEXT NOT NULL REFERENCES coord_versions(id),
    ring_no      INTEGER,
    station      TEXT REFERENCES stations(code),
    target_cp    TEXT REFERENCES control_points(code),
    x            REAL,          -- absolute coords (guide/segment)
    y            REAL,
    vx           REAL,          -- measured vector (resurvey)
    vy           REAL,
    measured_at  TEXT NOT NULL,
    weight       REAL NOT NULL DEFAULT 1.0,
    ignored      INTEGER NOT NULL DEFAULT 0,
    imported_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Confirmed similarity transforms: p_canonical = T p_source
-- a,b = rotation/scale terms; tx,ty = translation; rms = fit residual RMS.
CREATE TABLE IF NOT EXISTS transforms (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    from_version    TEXT NOT NULL REFERENCES coord_versions(id),
    to_version      TEXT NOT NULL REFERENCES coord_versions(id),
    a REAL NOT NULL, b REAL NOT NULL, tx REAL NOT NULL, ty REAL NOT NULL,
    rms REAL NOT NULL,
    fitted_point_hash TEXT NOT NULL,   -- hash of the common points used
    UNIQUE(from_version, to_version)
);

-- Version-confirmation audit trail: old coordinates, transform params and
-- the replayable digest are frozen the moment a frame is confirmed.
CREATE TABLE IF NOT EXISTS confirmations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id    TEXT NOT NULL REFERENCES coord_versions(id),
    to_version    TEXT NOT NULL,
    confirmed_at  TEXT NOT NULL DEFAULT (datetime('now')),
    params_json   TEXT NOT NULL,
    old_coords_json TEXT NOT NULL,
    input_hash    TEXT NOT NULL,
    point_hash    TEXT NOT NULL,
    UNIQUE(version_id, confirmed_at)
);

-- Frozen computation snapshots: replayable digest of every closure run.
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    input_hash    TEXT NOT NULL,       -- hash of canonical inputs + weights
    params_json   TEXT NOT NULL,       -- tolerances, anchors, process weight
    result_json   TEXT NOT NULL,       -- adjustments, residuals, status
    version_snapshot_json TEXT NOT NULL, -- old coords + transform params frozen
    UNIQUE(input_hash, params_json)
);
"""


def connect(db_path=None):
    conn = sqlite3.connect(db_path or DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def content_hash(payload: dict) -> str:
    """Stable hash of the *measured content* of an observation.

    Weight/ignored are user edits and deliberately excluded so that adjusting
    a weight re-binds the same observation instead of duplicating it.
    """
    keys = ("kind", "version_id", "ring_no", "station", "target_cp",
            "x", "y", "vx", "vy", "measured_at")
    canonical = json.dumps(
        {k: payload.get(k) for k in keys},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def digest(obj) -> str:
    canonical = json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
