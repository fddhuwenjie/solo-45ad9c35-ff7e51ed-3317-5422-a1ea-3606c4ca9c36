"""Seed a synthetic shield-drive dataset for demonstration.

Layout: design axis = +X, ring spacing 1.5 m, rings 1..120.
Stations S0..S3 move forward; control points CP1..CP6.
Frames: BASE, G1/G2 (guidance), SG1/SG2 (segment), R1..R4 (resurvey).

Seeded blockers (resolvable from the UI):
  * S1->CP2 resurvey carries a 12 mm blunder -> control contradiction cycle
  * G2 is not confirmed                 -> version crossing at the handover
  * one resurvey timestamp predates its station move -> time inversion
Re-running the seeder is idempotent (content hashes dedupe observations).
"""
import math
import sqlite3

from db import connect, init_db, content_hash, digest
import engine

RINGS = 120
STEP = 1.5


def hnoise(key, amp=0.0008):
    """Deterministic sub-millimetre synthetic noise (sub-mm)."""
    h = digest(str(key))
    v = int(h[:8], 16) / 0xFFFFFFFF          # 0..1
    h2 = digest(str(key) + "#y")
    v2 = int(h2[:8], 16) / 0xFFFFFFFF
    return (2 * v - 1) * amp, (2 * v2 - 1) * amp


# true frame similarities vs BASE
G1 = {"a": math.cos(0.002), "b": math.sin(0.002), "tx": 0.05, "ty": -0.03}
G2 = {"a": math.cos(-0.003), "b": math.sin(-0.003), "tx": -0.12, "ty": 0.08}
SG1 = {"a": math.cos(0.001), "b": math.sin(0.001), "tx": 0.01, "ty": 0.005}
SG2 = {"a": math.cos(-0.0015), "b": math.sin(-0.0015), "tx": -0.005, "ty": 0.01}
R0 = {"a": math.cos(0.0008), "b": math.sin(0.0008), "tx": 0.0, "ty": 0.0}
R1 = {"a": math.cos(-0.0011), "b": math.sin(-0.0011), "tx": 0.0, "ty": 0.0}
R2 = {"a": math.cos(0.0015), "b": math.sin(0.0015), "tx": 0.0, "ty": 0.0}
R3 = {"a": math.cos(-0.0011), "b": math.sin(-0.0011), "tx": 0.0, "ty": 0.0}
R4 = {"a": math.cos(-0.0006), "b": math.sin(-0.0006), "tx": 0.0, "ty": 0.0}


def inv_sim(t):
    return engine.invert(t)


def base_ring(r):
    """True tunnel position in BASE: axis +X with gentle lateral drift."""
    x = r * STEP
    y = 0.010 * math.sin(r / 14.0) + 0.0015 * (r - 60) / 60.0
    return x, y


CPS = {
    "CP1": (10.0, 6.0),
    "CP2": (55.0, -8.0),
    "CP3": (95.0, 7.0),
    "CP4": (130.0, -6.0),
    "CP5": (170.0, 8.0),
}
STATIONS = {
    "S0": (5.0, -4.0),
    "S1": (48.0, 4.0),
    "S2": (88.0, -4.0),
    "S3": (125.0, 4.0),
}
MOVES = [
    ("S0", "S1", "2026-08-04T08:00"),
    ("S1", "S2", "2026-08-21T08:00"),
    ("S2", "S3", "2026-09-07T08:00"),
]


def put_obs(cur, payload):
    h = content_hash(payload)
    cur.execute(
        """INSERT INTO observations
           (content_hash, kind, version_id, ring_no, station, target_cp,
            x, y, vx, vy, measured_at, weight)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(content_hash) DO NOTHING""",
        (h, payload.get("kind"), payload.get("version_id"), payload.get("ring_no"),
         payload.get("station"), payload.get("target_cp"),
         payload.get("x"), payload.get("y"),
         payload.get("vx"), payload.get("vy"), payload.get("measured_at"),
         payload.get("weight", 1.0)),
    )
    return cur.rowcount


def seed(conn):
    init_db(conn)
    cur = conn.cursor()
    versions = [
        ("BASE", "施工控制网(基准)", "base", "BASE", None, 1),
        ("G1", "盾构导向系统v1(1-60环)", "guide", "F1", "BASE", 1),
        ("G2", "盾构导向系统v2(61-120环)", "guide", "F2", "BASE", 0),
        ("SG1", "管片姿态系统v1(1-60环)", "segment", "F1", "G1", 1),
        ("SG2", "管片姿态系统v2(61-120环)", "segment", "F2", "G2", 1),
        ("R0", "S0测站复测坐标", "resurvey", "R0", "BASE", 1),
        ("R1", "S1测站复测坐标", "resurvey", "R1", "BASE", 1),
        ("R2", "S2测站复测坐标(早期)", "resurvey", "R2", "BASE", 1),
        ("R3", "S2测站复测坐标(新)", "resurvey", "R3", "BASE", 1),
        ("R4", "S3测站复测坐标", "resurvey", "R4", "BASE", 1),
    ]
    for vid, name, kind, family, base, conf in versions:
        cur.execute(
            "INSERT INTO coord_versions(id,name,kind,family,base_version_id,"
            "confirmed) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
            " kind=excluded.kind, family=excluded.family,"
            " base_version_id=excluded.base_version_id",
            (vid, name, kind, family, base, conf),
        )
    for code, (x, y) in CPS.items():
        cur.execute("INSERT INTO control_points(code,base_x,base_y)"
                    " VALUES (?,?,?) ON CONFLICT(code) DO UPDATE SET"
                    " base_x=excluded.base_x, base_y=excluded.base_y",
                    (code, x, y))
    for code, (x, y) in STATIONS.items():
        cur.execute("INSERT INTO stations(code,base_x,base_y) VALUES (?,?,?)"
                    " ON CONFLICT(code) DO UPDATE SET base_x=excluded.base_x,"
                    " base_y=excluded.base_y", (code, x, y))
    for f, t, ts in MOVES:
        cur.execute("INSERT INTO station_moves(from_station,to_station,move_time)"
                    " VALUES (?,?,?) ON CONFLICT(from_station,to_station)"
                    " DO NOTHING", (f, t, ts))

    added = 0

    # ---- guide points: G1 for rings 1..60, G2 for 61..120; the new G2
    # frame also re-shoots the handover ring 60 (real overlapping control),
    # which creates the same-ring version cross until G2 is confirmed ----
    for r in range(1, RINGS + 1):
        vid = "G1" if r <= 60 else "G2"
        sim = G1 if r <= 60 else G2
        bx, by = base_ring(r)
        lx, ly = engine.apply(inv_sim(sim), bx, by)
        nx, ny = hnoise(("guide", r))
        put_obs(cur, {"kind": "guide", "version_id": vid, "ring_no": r,
                      "x": lx + nx, "y": ly + ny,
                      "measured_at": _ring_time(r), "weight": 1.0})
        added += 1
    bx, by = base_ring(60)
    lx, ly = engine.apply(inv_sim(G2), bx, by)
    nx, ny = hnoise(("guide", "g2-overlap-60"))
    put_obs(cur, {"kind": "guide", "version_id": "G2", "ring_no": 60,
                  "x": lx + nx, "y": ly + ny,
                  "measured_at": "2026-08-20T15:00", "weight": 1.0})
    added += 1

    # ---- segment poses: SG1 (rings 1..60, every 3rd ring), SG2 (61..) ----
    for r in range(3, RINGS + 1, 3):
        vid = "SG1" if r <= 60 else "SG2"
        sim = SG1 if r <= 60 else SG2
        bx, by = base_ring(r)
        # segment coordinate is measured in its own frame, whose parent is G.
        lx, ly = engine.apply(inv_sim(sim), bx, by)
        nx, ny = hnoise(("seg", r), amp=0.001)
        put_obs(cur, {"kind": "segment", "version_id": vid, "ring_no": r,
                      "x": lx + nx, "y": ly + ny,
                      "measured_at": _ring_time(r, hour=20), "weight": 0.8})
        added += 1

    # ---- guide control survey: absolute CP coordinates in G1/G2 frames ----
    for cp in ("CP1", "CP2", "CP3"):
        bx, by = CPS[cp]
        lx, ly = engine.apply(inv_sim(G1), bx, by)
        nx, ny = hnoise(("g1cp", cp))
        put_obs(cur, {"kind": "guide", "version_id": "G1", "ring_no": None,
                      "target_cp": cp, "x": lx + nx, "y": ly + ny,
                      "measured_at": "2026-07-28T09:00", "weight": 1.0})
        added += 1
    for cp in ("CP3", "CP4", "CP5"):
        bx, by = CPS[cp]
        lx, ly = engine.apply(inv_sim(G2), bx, by)
        nx, ny = hnoise(("g2cp", cp))
        put_obs(cur, {"kind": "guide", "version_id": "G2", "ring_no": None,
                      "target_cp": cp, "x": lx + nx, "y": ly + ny,
                      "measured_at": "2026-08-20T09:00", "weight": 1.0})
        added += 1

    # ---- resurvey vectors (each station has its own resurvey frame) ----
    FRAME_SIM = {"R0": R0, "R1": R1, "R2": R2, "R3": R3, "R4": R4}

    def vec(frame, station, cp, ring, ts, blunder=0.0, key=None):
        bx, by = CPS[cp]
        sx, sy = STATIONS[station]
        bx_v, by_v = bx - sx, by - sy
        vx, vy = engine.apply(inv_sim(FRAME_SIM[frame]), bx_v, by_v)
        nx, ny = hnoise(key or (frame, station, cp))
        put_obs(cur, {"kind": "resurvey", "version_id": frame,
                      "ring_no": ring, "station": station, "target_cp": cp,
                      "vx": vx + nx + blunder, "vy": vy + ny,
                      "measured_at": ts, "weight": 1.0})
        return 1

    # S0 with R0 (rings <=10, before first move). CP1/CP3 are clean control
    # ties used to fit the frame; edge S0->CP2 carries the 18 mm blunder and
    # lies on every S0/S2 control cycle.
    added += vec("R0", "S0", "CP1", 5, "2026-07-30T10:00", key=("r", 1))
    added += vec("R0", "S0", "CP2", 10, "2026-08-01T10:00", blunder=0.018,
                 key=("r", 2))
    added += vec("R0", "S0", "CP3", 8, "2026-07-31T10:00", key=("r", 0))
    # S1 with R1 (forward rings)
    added += vec("R1", "S1", "CP1", 15, "2026-08-05T10:00", key=("r", 3))
    added += vec("R1", "S1", "CP2", 20, "2026-08-07T10:00", key=("r", 4))
    added += vec("R1", "S1", "CP3", 30, "2026-08-10T10:00", key=("r", 5))

    # S2 resurvey: early R2 then newer R3 (handover rings >= 40/70)
    added += vec("R2", "S2", "CP2", 40, "2026-08-22T10:00", key=("r", 6))
    added += vec("R2", "S2", "CP3", 45, "2026-08-23T10:00", key=("r", 7))
    added += vec("R3", "S2", "CP3", 70, "2026-09-02T10:00", key=("r", 8))
    added += vec("R3", "S2", "CP4", 75, "2026-09-04T10:00", key=("r", 9))
    # intentional time inversion: resurvey before S2 exists
    added += vec("R3", "S2", "CP2", 42, "2026-08-19T06:00", key=("r", 10))

    # S3 with R4 (handover rings >= 80)
    added += vec("R4", "S3", "CP4", 80, "2026-09-08T10:00", key=("r", 11))
    added += vec("R4", "S3", "CP5", 90, "2026-09-10T10:00", key=("r", 12))

    conn.commit()
    _store_confirmed_transforms(conn)
    conn.commit()
    return added


def _ring_time(r, hour=18):
    """~10 rings per day from 2026-07-27."""
    day = (r - 1) // 10
    hh = hour + ((r - 1) % 10)
    if hh >= 24:
        day += 1
        hh -= 24
    d = 27 + day
    month = 7
    if d > 31:
        d -= 31
        month = 8
    if d > 31:
        d -= 31
        month = 9
    return f"2026-{month:02d}-{d:02d}T{hh:02d}:30"


def _store_confirmed_transforms(conn):
    """Derive and persist similarity params for all seeded-confirmed frames
    from the actual noisy observations (same path the UI confirm uses)."""
    st = engine.State(conn)
    wanted = [("G1", "BASE", "cp_abs"), ("R0", "BASE", "vector"),
              ("R1", "BASE", "vector"),
              ("R2", "BASE", "vector"), ("R3", "BASE", "vector"),
              ("R4", "BASE", "vector")]
    for vid, to, mode in wanted:
        if conn.execute("SELECT 1 FROM transforms WHERE from_version=? AND"
                        " to_version=?", (vid, to)).fetchone():
            continue
        prop = next((p for p in st.fit_candidates(vid)
                     if p["to_version"] == to and p["mode"] == mode), None)
        if prop is None:
            continue
        ph = digest(sorted(prop["points"]))
        conn.execute(
            "INSERT INTO transforms(from_version,to_version,a,b,tx,ty,rms,"
            "fitted_point_hash) VALUES (?,?,?,?,?,?,?,?)",
            (vid, to, prop["a"], prop["b"], prop["tx"], prop["ty"],
             prop["rms"], ph))

    # segment frames chain through G frames: re-load then fit again
    st = engine.State(conn)
    for vid, to in (("SG1", "G1"), ("SG2", "G2")):
        if conn.execute("SELECT 1 FROM transforms WHERE from_version=? AND"
                        " to_version=?", (vid, to)).fetchone():
            continue
        prop = next((p for p in st.fit_candidates(vid)
                     if p["to_version"] == to and p["mode"] == "ring"), None)
        if prop is None:
            continue
        ph = digest(sorted(prop["points"]))
        conn.execute(
            "INSERT INTO transforms(from_version,to_version,a,b,tx,ty,rms,"
            "fitted_point_hash) VALUES (?,?,?,?,?,?,?,?)",
            (vid, to, prop["a"], prop["b"], prop["tx"], prop["ty"],
             prop["rms"], ph))


if __name__ == "__main__":
    conn = connect()
    n = seed(conn)
    print(f"seed prepared (new rows attempted: {n})")
