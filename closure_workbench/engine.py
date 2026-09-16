"""Closure engine: version transforms, station-move topology, conflict
detection, unique continuation path, and weighted least-squares propagation.

All geometry is 2-D.  A similarity transform is

    X = a*x - b*y + tx
    Y = b*x + a*x + ty      (i.e. Y = b*x + a*y + ty)

so (a, b) encode rotation+scale and (tx, ty) translation.  Vector-only
fits (total-station vectors between two known points) solve (a, b) with
tx = ty = 0, because translation is unobservable from free vectors.
"""
from __future__ import annotations

import datetime as _dt
import itertools
import math
from collections import defaultdict

from db import digest

MISCLOSURE_TOL_M = 0.010          # 10 mm control contradiction limit
RMS_FIT_LIMIT_M = 0.020           # refuse to confirm transforms fitting worse
PROCESS_WEIGHT = 10.0             # weight of the design-axis process edge
RING_STEP_M = 1.5


# ---------------------------------------------------------------- geometry
def _solve(a, b):
    """Gaussian elimination with partial pivot. a is n x n, b n-vector."""
    n = len(a)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-15:
            raise ValueError("singular normal matrix")
        m[col], m[piv] = m[piv], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (m[i][n] - sum(m[i][j] * x[j] for j in range(i + 1, n))) / m[i][i]
    return x


def fit_full(pairs):
    """Fit similarity v2 = T(v1) from [(p1, p2), ...]. Returns params+rms."""
    n = 0.0
    N = [[0.0] * 4 for _ in range(4)]
    rhs = [0.0] * 4
    for (x, y), (X, Y) in pairs:
        for grad, r in (((x, -y, 1.0, 0.0), X), ((y, x, 0.0, 1.0), Y)):
            for i in range(4):
                rhs[i] += grad[i] * r
                for j in range(4):
                    N[i][j] += grad[i] * grad[j]
        n += 1
    a, b, tx, ty = _solve(N, rhs)
    rms = math.sqrt(
        sum(
            (a * p1[0] - b * p1[1] + tx - p2[0]) ** 2
            + (b * p1[0] + a * p1[1] + ty - p2[1]) ** 2
            for p1, p2 in pairs
        )
        / (2 * n)
    )
    return {"a": a, "b": b, "tx": tx, "ty": ty, "rms": rms, "n": n}


def fit_linear(pairs):
    """Rotation+scale only (vectors):  X = a*x - b*y, Y = b*x + a*y.
    tx=ty=0.  The two normal equations decouple (cross terms cancel):
        a*S = sum(X*x + Y*y),  b*S = sum(Y*x - X*y),  S = sum(x^2+y^2).
    """
    s = ra = rb = 0.0
    for (x, y), (X, Y) in pairs:
        s += x * x + y * y
        ra += X * x + Y * y
        rb += Y * x - X * y
    if s < 1e-18:
        raise ValueError("degenerate vector pairs")
    a, b = ra / s, rb / s
    n = len(pairs)
    rms = math.sqrt(
        sum(
            (a * p1[0] - b * p1[1] - p2[0]) ** 2
            + (b * p1[0] + a * p1[1] - p2[1]) ** 2
            for p1, p2 in pairs
        )
        / (2 * n)
    )
    return {"a": a, "b": b, "tx": 0.0, "ty": 0.0, "rms": rms, "n": n}


def compose(t2, t1):
    """t2 ∘ t1."""
    return {
        "a": t2["a"] * t1["a"] - t2["b"] * t1["b"],
        "b": t2["a"] * t1["b"] + t2["b"] * t1["a"],
        "tx": t2["a"] * t1["tx"] - t2["b"] * t1["ty"] + t2["tx"],
        "ty": t2["b"] * t1["tx"] + t2["a"] * t1["ty"] + t2["ty"],
    }


def invert(t):
    d = t["a"] ** 2 + t["b"] ** 2
    ai, bi = t["a"] / d, -t["b"] / d
    return {
        "a": ai, "b": bi,
        "tx": -(ai * t["tx"] - bi * t["ty"]),
        "ty": -(bi * t["tx"] + ai * t["ty"]),
    }


def apply(t, x, y):
    return t["a"] * x - t["b"] * y + t["tx"], t["b"] * x + t["a"] * y + t["ty"]


# ---------------------------------------------------------------- state load
class State:
    def __init__(self, conn):
        self.conn = conn
        self.versions = {r["id"]: dict(r) for r in conn.execute(
            "SELECT * FROM coord_versions")}
        self.cps = {r["code"]: dict(r) for r in conn.execute(
            "SELECT * FROM control_points")}
        self.stations = {r["code"]: dict(r) for r in conn.execute(
            "SELECT * FROM stations")}
        self.moves = [dict(r) for r in conn.execute(
            "SELECT * FROM station_moves ORDER BY move_time, id")]
        self.obs = [dict(r) for r in conn.execute(
            "SELECT * FROM observations ORDER BY id")]
        self.transforms = [dict(r) for r in conn.execute(
            "SELECT * FROM transforms")]
        self._build_chains()
        self._canonical_obs()

    def _build_chains(self):
        """BFS chains of confirmed transforms.

        A stored row holds q: from_version -> to_version.  While expanding
        BASE the traversed edge carries p: cur -> nxt, i.e. X_nxt = p(X_cur),
        hence X_cur = p^-1(X_nxt) and
            chain[nxt] = chain[cur] ∘ p^-1
        maps nxt coordinates into BASE.
        """
        adj = defaultdict(list)
        for t in self.transforms:
            q = {"a": t["a"], "b": t["b"], "tx": t["tx"], "ty": t["ty"]}
            adj[t["from_version"]].append((t["to_version"], q))
            adj[t["to_version"]].append((t["from_version"], invert(q)))
        self.tgraph = adj
        self.chain = {"BASE": {"a": 1.0, "b": 0.0, "tx": 0.0, "ty": 0.0}}
        dq = ["BASE"]
        while dq:
            cur = dq.pop(0)
            for nxt, p in adj[cur]:
                if nxt not in self.chain:
                    self.chain[nxt] = compose(self.chain[cur], invert(p))
                    dq.append(nxt)

    def to_base(self, vid, x, y, vector=False):
        t = self.chain.get(vid)
        if t is None:
            return None
        if vector:
            return t["a"] * x - t["b"] * y, t["b"] * x + t["a"] * y
        return apply(t, x, y)
    def _canonical_obs(self):
        """Attach canonical coordinates/vectors to every live observation."""
        for o in self.obs:
            o["live"] = not o["ignored"]
            if not o["live"]:
                continue
            if o["kind"] in ("guide", "segment"):
                p = self.to_base(o["version_id"], o["x"], o["y"])
                o["cx"], o["cy"] = p if p else (None, None)
                o["convertible"] = p is not None
            else:
                p = self.to_base(o["version_id"], o["vx"], o["vy"], vector=True)
                o["cvx"], o["cvy"] = p if p else (None, None)
                o["convertible"] = p is not None

    # ------------------------------------------------------- transform fitting
    def fit_candidates(self, vid):
        """Common point/vector pairs between frame vid and frames that
        already have a chain to BASE.

        Three modes:
          cp_abs : absolute CP coords (control survey) vs BASE CP coords
          vector : resurvey vectors vs BASE station->CP base vectors
                   (a resurvey frame is one frame across ALL stations: the
                    same rotation+scale is fitted jointly from every vector)
          ring   : absolute ring points vs a chained neighbouring frame
        """
        cp_abs = defaultdict(dict)     # cp -> version -> (x,y)
        ring_abs = defaultdict(dict)   # ring -> version -> (x,y)
        vecs = defaultdict(lambda: defaultdict(dict))  # station -> cp -> ver
        for o in self.obs:
            if o["ignored"]:
                continue
            if o["kind"] in ("guide", "segment") and o["target_cp"]:
                cp_abs[o["target_cp"]][o["version_id"]] = (o["x"], o["y"])
            if o["kind"] in ("guide", "segment") and o["ring_no"] is not None:
                ring_abs[o["ring_no"]][o["version_id"]] = (o["x"], o["y"])
            if o["kind"] == "resurvey":
                vecs[o["station"]][o["target_cp"]][o["version_id"]] = (
                    o["vx"], o["vy"])

        proposals = []

        # 1) absolute CP pairs vs BASE
        if vid != "BASE":
            pairs, pts = [], []
            for cp, frames in cp_abs.items():
                if vid in frames and self.cps.get(cp, {}).get("base_x") is not None:
                    pairs.append((frames[vid],
                                  (self.cps[cp]["base_x"], self.cps[cp]["base_y"])))
                    pts.append(cp)
            if len(pairs) >= 2:
                f = fit_full(pairs)
                proposals.append({
                    "to_version": "BASE", "mode": "cp_abs",
                    "points": pts, **f})

        # 2) resurvey vectors vs BASE station->CP vectors (all stations)
        if any(vid in frames for cps_d in vecs.values()
               for frames in cps_d.values()):
            pairs, pts = [], []
            for stn, cps_d in vecs.items():
                s = self.stations.get(stn)
                if not s:
                    continue
                for cp, frames in cps_d.items():
                    if vid in frames and self.cps.get(cp, {}).get("base_x") is not None:
                        pairs.append((frames[vid],
                                      (self.cps[cp]["base_x"] - s["base_x"],
                                       self.cps[cp]["base_y"] - s["base_y"])))
                        pts.append(f"{stn}→{cp}")
            if len(pairs) >= 2:
                f = fit_linear(pairs)
                proposals.append({
                    "to_version": "BASE", "mode": "vector",
                    "points": pts, **f})

        # 3) absolute ring points vs ANY other frame sharing rings.  A
        # relative transform is estimable from common points even when neither
        # side is linked to BASE yet; the BFS chain connects it once an
        # ancestor frame is confirmed.
        neighbours = set()
        for ring, frames in ring_abs.items():
            if vid in frames:
                neighbours.update(ov for ov in frames if ov != vid)
        for ov in sorted(neighbours):
            pairs, pts = [], []
            for ring, frames in ring_abs.items():
                if vid in frames and ov in frames:
                    pairs.append((frames[vid], frames[ov]))
                    pts.append(f"R{ring}")
            if len(pairs) >= 2:
                f = fit_full(pairs)
                proposals.append({
                    "to_version": ov, "mode": "ring",
                    "points": pts, **f})

        proposals.sort(key=lambda p: (p["rms"], -p["n"]))
        return proposals


# ------------------------------------------------------------- topology
def station_topology(st: State):
    """Resurvey edges -> station/CP bipartite graph, handover links derived
    from shared control points, then constrained by declared station moves
    (mileage order) and by ring numbers."""
    edges = [o for o in st.obs if o["kind"] == "resurvey" and not o["ignored"]]
    # stations serving a CP
    cp_stations = defaultdict(list)
    for o in edges:
        cp_stations[o["target_cp"]].append(o)

    move_pairs = {(m["from_station"], m["to_station"]): m for m in st.moves}
    move_into = {m["to_station"]: m["move_time"] for m in st.moves}
    max_ring = defaultdict(int)
    for o in edges:
        if o["ring_no"]:
            max_ring[o["station"]] = max(max_ring[o["station"]], o["ring_no"])

    links = []
    seen = set()
    for cp, olist in cp_stations.items():
        stations_here = sorted({o["station"] for o in olist})
        for a, b in itertools.combinations(stations_here, 2):
            # a handover edge only counts as evidence if BOTH observations
            # were taken after the receiving station came into service
            ev = [o for o in olist
                  if o["station"] in (a, b)
                  and (move_into.get(o["station"]) is None
                       or o["measured_at"] >= move_into[o["station"]])]
            time_ok = {o["station"] for o in ev} == set(stations_here)
            # orient by declared move order
            if (a, b) in move_pairs:
                frm, to = a, b
            elif (b, a) in move_pairs:
                frm, to = b, a
            else:
                frm, to = (a, b) if max_ring[a] <= max_ring[b] else (b, a)
            key = (frm, to, cp)
            if key in seen:
                continue
            seen.add(key)
            declared = (frm, to) in move_pairs
            to_rings = [o["ring_no"] for o in olist
                        if o["station"] == to and o["ring_no"]]
            ring_ok = (not declared) or not to_rings or not max_ring[frm] or \
                min(to_rings) >= max_ring[frm]
            links.append({
                "cp": cp, "from": frm, "to": to,
                "declared": declared, "ring_ok": ring_ok,
                "time_ok": time_ok,
                "from_max_ring": max_ring[frm],
                "to_min_ring": min(to_rings) if to_rings else None,
                "obs_ids": [o["id"] for o in olist],
                "evidence_ids": [o["id"] for o in ev],
            })
    return edges, links, move_pairs, dict(max_ring)


def continuation_paths(st: State, links):
    """Enumerate simple station sequences from the earliest station along
    *declared, ring-valid* handover links.  A succession is a station
    sequence (shared CPs are just different evidence for the same move), so
    duplicates collapse.  0 / 1 / >1 sequences is the decision signal:
    ring-number + mileage constraints must select exactly one succession."""
    order = [st.moves[0]["from_station"]] + [m["to_station"] for m in st.moves]
    start, target = order[0], order[-1]
    adj = defaultdict(list)
    for l in links:
        if l["declared"] and l["ring_ok"] and l["time_ok"]:
            adj[l["from"]].append(l)
    sequences = []
    frontier = [([start], [])]
    while frontier:
        nodes, used = frontier.pop()
        if nodes[-1] == target:
            if nodes not in sequences:
                sequences.append(nodes)
            continue
        for l in adj[nodes[-1]]:
            if l["to"] not in nodes:
                frontier.append((nodes + [l["to"]], used + [l]))
    paths = [{"stations": s,
              "links": [l for s_a, s_b in zip(s, s[1:])
                        for l in links if l["from"] == s_a and l["to"] == s_b
                        and l["declared"] and l["ring_ok"] and l["time_ok"]]}
             for s in sequences]
    return order, start, target, paths


# ------------------------------------------------------------- conflicts
def _simple_cycles(edges):
    """Enumerate simple cycles of the station–CP bipartite graph.

    Johnson-style backtracking: for each node s (ordered) we only accept
    cycles whose smallest node is s, which lists each undirected cycle once.
    A valid cycle alternates S/P and returns to s through an edge whose other
    endpoint is the current node (both S->P and P->S closures allowed).
    """
    adj = defaultdict(list)
    for o in edges:
        s, p = ("S", o["station"]), ("P", o["target_cp"])
        adj[s].append((p, o["id"], +1))   # traversal along stored vector
        adj[p].append((s, o["id"], -1))   # traversal against stored vector
    nodes = sorted(adj)
    cycles = []
    LIMIT = 12

    for si, root in enumerate(nodes):
        # Johnson subgraph: remove nodes strictly smaller than root
        allowed = set(nodes[si:])

        def search(cur, path, eids, signs):
            for nxt, eid, sgn in adj[cur]:
                if nxt not in allowed or eid in eids:
                    continue
                if nxt == root and len(eids) >= 4:
                    cycles.append(list(zip(eids + [eid], signs + [sgn])))
                    continue
                if nxt in path:
                    continue
                if len(eids) + 1 <= LIMIT:
                    search(nxt, path | {nxt}, eids + [eid], signs + [sgn])

        search(root, {root}, [], [])
    return cycles


def contradiction_cycles(st: State, edges, tol=MISCLOSURE_TOL_M):
    """Misclosure of every bipartite cycle using canonical vectors; only
    cycles whose every edge converts can be tested."""
    by_id = {o["id"]: o for o in edges}
    out = []
    seen_sets = set()
    for cyc in _simple_cycles(edges):
        key = frozenset(eid for eid, _ in cyc)
        if key in seen_sets:
            continue
        seen_sets.add(key)
        dx = dy = 0.0
        ok = True
        for eid, sgn in cyc:
            o = by_id[eid]
            if o.get("cvx") is None:
                ok = False
                break
            dx += sgn * o["cvx"]
            dy += sgn * o["cvy"]
        if not ok:
            continue
        mis = math.hypot(dx, dy)
        if mis > tol:
            out.append({"edges": sorted(key), "misclosure": mis,
                        "dx": dx, "dy": dy})
    out.sort(key=lambda c: (len(c["edges"]), -c["misclosure"]))
    return out


def minimal_cut(st: State, edges, bad_cycles, tol=MISCLOSURE_TOL_M):
    """Greedy minimum feedback set over bad cycles.  At each step every edge
    participating in a remaining bad cycle is tried; we cut the edge that
    resolves the most bad cycles per unit of collateral damage (cycles that
    disappear although they were healthy), tie-breaking by lowest weight.
    The result is the minimum set of conflict edges to highlight."""
    cut = []
    kept = list(edges)

    def all_cycles(kept_edges):
        return contradiction_cycles(st, kept_edges, tol), \
            _cycle_count_any(st, kept_edges, tol)

    _, healthy0 = all_cycles(kept)
    while True:
        bad_now = contradiction_cycles(st, kept, tol)
        if not bad_now:
            break
        candidates = sorted({eid for c in bad_now for eid in c["edges"]},
                             key=lambda e: next(
                                 (o["weight"] for o in kept if o["id"] == e),
                                 1.0))
        best = None
        for eid in candidates:
            trial = [o for o in kept if o["id"] != eid]
            new_bad = contradiction_cycles(st, trial, tol)
            removed_bad = len(bad_now) - len(new_bad)
            _, healthy = all_cycles(trial)
            damage = healthy0 - healthy - removed_bad  # healthy cycles lost
            score = (damage, -removed_bad,
                     next((o["weight"] for o in kept if o["id"] == eid), 1.0),
                     sum(c["misclosure"] for c in new_bad))
            if best is None or score < best[0]:
                best = (score, eid, new_bad)
        cut.append(best[1])
        kept = [o for o in kept if o["id"] != best[1]]
    return cut


def _cycle_count_any(st: State, edges, tol):
    """Number of testable cycles (of any closure quality) remaining."""
    by_id = {o["id"]: o for o in edges}
    n = 0
    for cyc in _simple_cycles(edges):
        if all(by_id[e].get("cvx") is not None for e, _ in cyc):
            n += 1
    return n


def detect_conflicts(st: State):
    conflicts = []
    edges, links, move_pairs, max_ring = station_topology(st)

    # 1) resurvey earlier than the station move that brought it into service
    move_into = {m["to_station"]: m["move_time"] for m in st.moves}
    for o in edges:
        mt = move_into.get(o["station"])
        if mt and o["measured_at"] < mt:
            conflicts.append({
                "type": "time_inversion", "severity": "block",
                "obs_id": o["id"],
                "message": f"{o['station']}→{o['target_cp']} 复测于 "
                           f"{o['measured_at']}，早于该站换站时刻 {mt}",
            })

    # 2) ring-number inversion on a declared handover link
    for l in links:
        if l["declared"] and not l["ring_ok"]:
            conflicts.append({
                "type": "ring_inversion", "severity": "block",
                "obs_ids": l["obs_ids"],
                "message": f"换站接续 {l['from']}→{l['to']}（{l['cp']}）环号倒灌："
                           f"接管侧最小环 {l['to_min_ring']} < 交出侧最大环 "
                           f"{l['from_max_ring']}",
            })

    # 3) coordinate versions crossing on the same ring without a confirmed
    #    transform chain.  guide + segment frames of the same survey era
    #    share a family and are meant to co-exist; a crossing only arises
    #    between different families (typically across a station move).
    fam = {v: (meta.get("family") or vid) for v, meta in st.versions.items()}
    by_ring = defaultdict(set)
    ring_obs = defaultdict(list)
    for o in st.obs:
        if o["kind"] in ("guide", "segment") and not o["ignored"] \
                and o["ring_no"] is not None:
            by_ring[o["ring_no"]].add(o["version_id"])
            ring_obs[o["ring_no"]].append(o["id"])
    cross_groups = defaultdict(list)   # unresolved family set -> rings
    for ring, vids in sorted(by_ring.items()):
        families = {fam[v] for v in vids}
        if len(families) <= 1:
            continue
        unresolved = tuple(sorted(
            f for f in families
            if not any(v in st.chain for v in vids if fam[v] == f)))
        if unresolved:
            cross_groups[unresolved].append(ring)
    for unresolved, rings in cross_groups.items():
        handover = rings[0]
        conflicts.append({
            "type": "version_cross", "severity": "block",
            "ring": handover, "rings": rings,
            "obs_ids": ring_obs[handover],
            "families": list(unresolved),
            "message": f"坐标版本交叉：族 {'/'.join(unresolved)} 在第 "
                       f"{handover}~{rings[-1]} 环（共 {len(rings)} 环）"
                       f"与已确认版本无变换链；交界环为第 {handover} 环",
        })

    # 4) control-point contradiction cycles
    convertible = [o for o in edges if o.get("cvx") is not None]
    bad = contradiction_cycles(st, convertible)
    cut = minimal_cut(st, convertible, bad) if bad else []
    for c in bad:
        conflicts.append({
            "type": "contradiction_cycle", "severity": "block",
            "obs_ids": c["edges"],
            "misclosure": round(c["misclosure"], 4),
            "cut_edge": c["edges"][0] if len(bad) == 1 else None,
            "message": "控制点矛盾环（"
                       + "-".join(str(i) for i in c["edges"])
                       + f"）闭合差 {c['misclosure']*1000:.1f} mm 超差 "
                       f"{MISCLOSURE_TOL_M*1000:.0f} mm",
        })

    order, start, target, paths = continuation_paths(st, links)
    path_status = "unique"
    path_msg = ""
    if len(paths) == 1:
        path_msg = "唯一接续路径：" + " → ".join(paths[0]["stations"])
    elif len(paths) == 0:
        path_status = "none"
        path_msg = f"里程/环号约束下 {start} 到 {target} 不存在接续路径"
    else:
        path_status = "ambiguous"
        path_msg = (f"存在 {len(paths)} 条接续候选，约束不足以唯一确定："
                    + "；".join(" → ".join(p["stations"]) for p in paths))

    return {
        "conflicts": conflicts,
        "links": links,
        "edges": [_edge_json(o) for o in edges],
        "order": order, "paths": paths,
        "path_status": path_status, "path_message": path_msg,
        "cut_edges": cut,
    }


def _edge_json(o):
    return {
        "id": o["id"], "station": o["station"], "cp": o["target_cp"],
        "version": o["version_id"], "ring": o["ring_no"],
        "measured_at": o["measured_at"], "weight": o["weight"],
        "cvx": None if o.get("cvx") is None else round(o["cvx"], 4),
        "cvy": None if o.get("cvy") is None else round(o["cvy"], 4),
        "convertible": o.get("convertible", False),
    }


# ------------------------------------------------------------- LS adjustment
def line_adjustment(st: State, process_weight=PROCESS_WEIGHT):
    """Weighted least squares on the ring chain.

    Unknown per ring (X_i, Y_i).  Observations: every convertible guide /
    segment point; process edges enforce the design step (1.5 m along X);
    the first ring is anchored to its observed mean.  Changing one edge
    weight therefore re-propagates residuals along the whole line.
    """
    rings = sorted({o["ring_no"] for o in st.obs
                    if o["kind"] in ("guide", "segment")
                    and not o["ignored"] and o.get("cx") is not None
                    and o["ring_no"] is not None})
    if not rings:
        return {"status": "no_observations", "rings": [], "obs": []}
    idx = {r: i for i, r in enumerate(rings)}
    n = len(rings)
    size = 2 * n
    N = [[0.0] * size for _ in range(size)]
    u = [0.0] * size
    obs_rows = []

    for o in st.obs:
        if o["kind"] not in ("guide", "segment") or o["ignored"]:
            continue
        if o.get("cx") is None or o["ring_no"] not in idx:
            obs_rows.append({"id": o["id"], "ring": o["ring_no"],
                             "kind": o["kind"], "version": o["version_id"],
                             "weight": o["weight"], "status": "unconverted"})
            continue
        i = 2 * idx[o["ring_no"]]
        w = o["weight"]
        N[i][i] += w
        N[i + 1][i + 1] += w
        u[i] += w * o["cx"]
        u[i + 1] += w * o["cy"]
        obs_rows.append({"id": o["id"], "ring": o["ring_no"],
                         "kind": o["kind"], "version": o["version_id"],
                         "weight": w, "cx": o["cx"], "cy": o["cy"],
                         "status": "ok"})

    pw = process_weight
    for a, b in zip(rings, rings[1:]):
        ia, ib = 2 * idx[a], 2 * idx[b]
        # X_b - X_a = step*(b-a)
        for k, (r0, c0, r1, c1, val) in enumerate((
                (ib, ib, ia, ia, RING_STEP_M * (b - a)),
                (ib + 1, ib + 1, ia + 1, ia + 1, 0.0))):
            N[r0][c0] += pw
            N[r1][c1] += pw
            N[r0][c1] -= pw
            N[r1][c0] -= pw
            u[r0] += pw * val
            u[r1] -= pw * val

    # anchor the first ring to the weighted mean of its own observations
    r0 = rings[0]
    first = [o for o in st.obs if o.get("ring_no") == r0
             and o["kind"] in ("guide", "segment") and not o["ignored"]
             and o.get("cx") is not None]
    sw = sum(o["weight"] for o in first)
    ax = sum(o["weight"] * o["cx"] for o in first) / sw
    ay = sum(o["weight"] * o["cy"] for o in first) / sw
    AW = 1e6
    N[0][0] += AW
    N[1][1] += AW
    u[0] += AW * ax
    u[1] += AW * ay

    sol = _solve(N, u)
    adj = {r: (sol[2 * i], sol[2 * i + 1]) for i, r in enumerate(rings)}

    for row in obs_rows:
        if row["status"] != "ok":
            continue
        X, Y = adj[row["ring"]]
        row["dx"] = X - row["cx"]
        row["dy"] = Y - row["cy"]
        row["residual"] = math.hypot(row["dx"], row["dy"])

    line = []
    for r in rings:
        X, Y = adj[r]
        line.append({
            "ring": r, "x": round(X, 4), "y": round(Y, 4),
            "design_x": r * RING_STEP_M,
            "lat_dev": round(Y, 4),
        })
    used = [row for row in obs_rows if row["status"] == "ok"]
    rms = math.sqrt(sum(r["residual"] ** 2 for r in used) / max(len(used), 1))
    worst = max(used, key=lambda r: r["residual"], default=None)
    return {
        "status": "ok", "rings": line, "obs": [
            {k: (round(v, 4) if isinstance(v, float) else v)
             for k, v in row.items()}
            for row in obs_rows
        ],
        "rms": round(rms, 4),
        "max_residual": round(worst["residual"], 4) if worst else None,
        "max_obs_id": worst["id"] if worst else None,
        "params": {"process_weight": process_weight,
                   "misclosure_tol": MISCLOSURE_TOL_M},
    }


# ------------------------------------------------------------- full assembly
def assemble(conn, process_weight=PROCESS_WEIGHT, include_ls=True):
    st = State(conn)
    topo = detect_conflicts(st)
    blocked = bool(topo["conflicts"]) or topo["path_status"] != "unique"
    ls = line_adjustment(st, process_weight) if include_ls else None

    versions = []
    for v in st.versions.values():
        t = st.chain.get(v["id"])
        versions.append({
            "id": v["id"], "name": v["name"], "kind": v["kind"],
            "family": v["family"],
            "confirmed": bool(v["confirmed"]),
            "confirmed_at": v["confirmed_at"],
            "chained": t is not None,
            "chain": None if t is None else {k: round(t[k], 7) for k in
                                             ("a", "b", "tx", "ty")},
            "fit_preview": st.fit_candidates(v["id"])
            if not v["confirmed"] else [],
        })

    obs_out = []
    for o in st.obs:
        obs_out.append({
            "id": o["id"], "kind": o["kind"], "version": o["version_id"],
            "ring": o["ring_no"], "station": o["station"],
            "target_cp": o["target_cp"],
            "x": o["x"], "y": o["y"], "vx": o["vx"], "vy": o["vy"],
            "measured_at": o["measured_at"], "weight": o["weight"],
            "ignored": bool(o["ignored"]),
            "cx": None if o.get("cx") is None else round(o["cx"], 4),
            "cy": None if o.get("cy") is None else round(o["cy"], 4),
            "cvx": None if o.get("cvx") is None else round(o["cvx"], 4),
            "cvy": None if o.get("cvy") is None else round(o["cvy"], 4),
            "convertible": o.get("convertible", False),
            "content_hash": o["content_hash"][:12],
        })

    input_obj = {
        "versions": {v: st.versions[v]["confirmed"] for v in sorted(st.versions)},
        "transforms": sorted(
            (t["from_version"], t["to_version"], round(t["a"], 9),
             round(t["b"], 9), round(t["tx"], 9), round(t["ty"], 9))
            for t in st.transforms),
        "obs": sorted(
            (o["id"], o["kind"], o["version_id"], o["ring_no"], o["station"],
             o["target_cp"], o["x"], o["y"], o["vx"], o["vy"],
             o["measured_at"], round(o["weight"], 6), o["ignored"])
            for o in st.obs),
    }

    return {
        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "blocked": blocked,
        "status_text": "阻断：必须先消除全部冲突" if blocked else "闭合条件满足，可执行全线平差与确认",
        "topology": topo,
        "versions": versions,
        "observations": obs_out,
        "stations": list(st.stations.values()),
        "cps": list(st.cps.values()),
        "moves": st.moves,
        "adjustment": ls,
        "tolerance": MISCLOSURE_TOL_M,
        "input_hash": digest(input_obj)[:16],
    }
