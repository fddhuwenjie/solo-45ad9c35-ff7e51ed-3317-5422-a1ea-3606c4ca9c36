"""HTTP workbench server (Python stdlib only).

Endpoints
  GET  /                       web UI
  GET  /api/state[?pw=...]     full assembled workbench state
  POST /api/observations       import one record or a list (idempotent)
  PATCH /api/observations/<id> {weight?, ignored?, measured_at?}
  POST /api/versions/<id>/confirm  {to_version, mode}
  POST /api/runs               freeze a replayable snapshot of current state
  GET  /api/runs               list snapshots
  POST /api/reseed             reset database to the synthetic seed dataset
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from db import connect, init_db, content_hash, digest
import engine
import seed

WEB = os.path.join(os.path.dirname(__file__), "web")
_LOCK = threading.Lock()


def _freeze_version_snapshot(conn):
    """Everything needed to replay a closure run later: raw observations,
    old coordinates of confirmed frames, and all transform parameters."""
    obs = [dict(r) for r in conn.execute(
        "SELECT id,kind,version_id,ring_no,station,target_cp,x,y,vx,vy,"
        "measured_at,weight,ignored,content_hash FROM observations ORDER BY id")]
    transforms = [dict(r) for r in conn.execute(
        "SELECT from_version,to_version,a,b,tx,ty,rms,"
        "fitted_point_hash FROM transforms ORDER BY id")]
    versions = [dict(r) for r in conn.execute(
        "SELECT id,name,kind,family,confirmed,confirmed_at"
        " FROM coord_versions ORDER BY id")]
    confirmations = [dict(r) for r in conn.execute(
        "SELECT version_id,to_version,confirmed_at,params_json,"
        "old_coords_json,input_hash FROM confirmations ORDER BY id")]
    return {"versions": versions, "transforms": transforms,
            "confirmations": confirmations, "observations": obs}


def confirm_version(conn, vid, to_version, mode):
    """Fit -> quality gate -> persist transform + freeze old coordinates.
    Old coordinates are deliberately snapshotted before the frame joins the
    confirmed chain, so any later re-confirmation can be diffed/replayed."""
    st = engine.State(conn)
    if vid not in st.versions or vid == "BASE":
        raise ValueError("unknown or base version")
    prop = next((p for p in st.fit_candidates(vid)
                 if p["to_version"] == to_version and p["mode"] == mode), None)
    if prop is None:
        raise ValueError(
            f"无法用模式 {mode} 将 {vid} 配准到 {to_version}（公共点不足）")
    if prop["rms"] > engine.RMS_FIT_LIMIT_M:
        raise ValueError(
            f"拟合 RMS {prop['rms']*1000:.1f} mm 超过确认门限 "
            f"{engine.RMS_FIT_LIMIT_M*1000:.0f} mm，拒绝确认；"
            "请先剔除粗差观测")

    point_hash = digest(sorted(prop["points"]))
    old_coords = [{
        "id": o["id"], "kind": o["kind"], "ring_no": o["ring_no"],
        "station": o["station"], "target_cp": o["target_cp"],
        "x": o["x"], "y": o["y"], "vx": o["vx"], "vy": o["vy"],
        "version_id": o["version_id"], "measured_at": o["measured_at"],
    } for o in st.obs if o["version_id"] == vid]
    params = {"a": prop["a"], "b": prop["b"], "tx": prop["tx"],
              "ty": prop["ty"], "rms": prop["rms"], "n": prop["n"],
              "points": prop["points"], "mode": mode,
              "to_version": to_version}

    with _LOCK:
        conn.execute(
            "INSERT INTO transforms(from_version,to_version,a,b,tx,ty,rms,"
            "fitted_point_hash) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(from_version,to_version) DO UPDATE SET a=excluded.a,"
            " b=excluded.b, tx=excluded.tx, ty=excluded.ty, rms=excluded.rms,"
            " fitted_point_hash=excluded.fitted_point_hash",
            (vid, to_version, prop["a"], prop["b"], prop["tx"], prop["ty"],
             prop["rms"], point_hash))
        conn.execute(
            "UPDATE coord_versions SET confirmed=1,"
            " confirmed_at=COALESCE(confirmed_at, datetime('now'))"
            " WHERE id=?", (vid,))
        pre = engine.assemble(conn, include_ls=False)
        conn.execute(
            "INSERT INTO confirmations(version_id,to_version,params_json,"
            "old_coords_json,input_hash,point_hash) VALUES (?,?,?,?,?,?)",
            (vid, to_version, json.dumps(params, ensure_ascii=False),
             json.dumps(old_coords, ensure_ascii=False),
             pre["input_hash"], point_hash))
        conn.commit()
    return params


def import_observations(conn, records):
    """Idempotent import: identical measured content never duplicates; a
    re-import carrying a different weight re-binds the same observation."""
    if isinstance(records, dict):
        records = [records]
    added, rebound, duplicates = [], 0, 0
    required = ("kind", "version_id", "measured_at")
    with _LOCK:
        for rec in records:
            for k in required:
                if not rec.get(k):
                    raise ValueError(f"记录缺少必填字段 {k}: {rec}")
            if rec["kind"] not in ("guide", "segment", "resurvey"):
                raise ValueError(f"未知观测类型 {rec['kind']}")
            payload = {
                "kind": rec["kind"], "version_id": rec["version_id"],
                "ring_no": rec.get("ring_no"), "station": rec.get("station"),
                "target_cp": rec.get("target_cp"),
                "x": _num(rec.get("x")), "y": _num(rec.get("y")),
                "vx": _num(rec.get("vx")), "vy": _num(rec.get("vy")),
                "measured_at": rec["measured_at"],
            }
            h = content_hash(payload)
            weight = float(rec.get("weight", 1.0))
            exists = conn.execute(
                "SELECT weight FROM observations WHERE content_hash=?",
                (h,)).fetchone()
            if exists:
                if abs(exists["weight"] - weight) > 1e-9:
                    conn.execute(
                        "UPDATE observations SET weight=? WHERE content_hash=?",
                        (weight, h))
                    rebound += 1
                else:
                    duplicates += 1
                continue
            conn.execute(
                "INSERT INTO observations(content_hash,kind,version_id,"
                "ring_no,station,target_cp,x,y,vx,vy,measured_at,weight)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (h, payload["kind"], payload["version_id"],
                 payload["ring_no"], payload["station"], payload["target_cp"],
                 payload["x"], payload["y"], payload["vx"], payload["vy"],
                 payload["measured_at"], weight))
            row = conn.execute(
                "SELECT id FROM observations WHERE content_hash=?",
                (h,)).fetchone()
            added.append(row["id"])
        conn.commit()
    return {"added": len(added), "added_ids": added,
            "rebound": rebound, "duplicates": duplicates}


def _num(v):
    return None if v is None or v == "" else float(v)


class Handler(BaseHTTPRequestHandler):
    server_version = "ClosureWorkbench/1.0"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/":
            return self._file(os.path.join(WEB, "index.html"),
                              "text/html; charset=utf-8")
        if path in ("/app.js",):
            return self._file(os.path.join(WEB, "app.js"),
                              "application/javascript; charset=utf-8")
        if path in ("/app.css",):
            return self._file(os.path.join(WEB, "app.css"),
                              "text/css; charset=utf-8")
        if path == "/api/state":
            pw = engine.PROCESS_WEIGHT
            for kv in query.split("&"):
                if kv.startswith("pw="):
                    try:
                        pw = float(kv[3:])
                    except ValueError:
                        pass
            with _LOCK:
                return self._json(engine.assemble(
                    self.server.conn, process_weight=pw))
        if path == "/api/runs":
            rows = self.server.conn.execute(
                "SELECT id,created_at,input_hash,params_json,result_json,"
                "version_snapshot_json FROM runs ORDER BY id DESC").fetchall()
            out = [{"id": r["id"], "created_at": r["created_at"],
                    "input_hash": r["input_hash"],
                    "params": json.loads(r["params_json"]),
                    "result": json.loads(r["result_json"]),
                    "snapshot": json.loads(r["version_snapshot_json"])}
                   for r in rows]
            return self._json(out)
        self.send_error(404)

    def do_POST(self):
        path = self.path.rstrip("/")
        try:
            if path == "/api/observations":
                return self._json(import_observations(
                    self.server.conn, self._body()))
            if path.startswith("/api/versions/") and path.endswith("/confirm"):
                vid = path.split("/")[3]
                body = self._body()
                params = confirm_version(
                    self.server.conn, vid, body["to_version"], body["mode"])
                return self._json({"ok": True, "params": params})
            if path == "/api/runs":
                body = self._body()
                with _LOCK:
                    state = engine.assemble(
                        self.server.conn,
                        process_weight=float(body.get("process_weight",
                                                     engine.PROCESS_WEIGHT)))
                    snap = _freeze_version_snapshot(self.server.conn)
                    params = state["adjustment"]["params"]
                    result = {"blocked": state["blocked"],
                              "status_text": state["status_text"],
                              "path_status": state["topology"]["path_status"],
                              "path": state["topology"]["path_message"],
                              "conflicts": state["topology"]["conflicts"],
                              "cut_edges": state["topology"]["cut_edges"],
                              "adjustment": {
                                  "rms": state["adjustment"]["rms"],
                                  "max_residual":
                                      state["adjustment"]["max_residual"],
                                  "max_obs_id":
                                      state["adjustment"]["max_obs_id"],
                                  "n_obs": len([
                                      o for o in state["adjustment"]["obs"]
                                      if o["status"] == "ok"])}}
                    params_json = json.dumps(params, ensure_ascii=False,
                                             sort_keys=True)
                    existing = self.server.conn.execute(
                        "SELECT id,created_at FROM runs WHERE input_hash=?"
                        " AND params_json=?",
                        (state["input_hash"], params_json)).fetchone()
                    if existing:
                        return self._json(
                            {"ok": True, "run_id": existing["id"],
                             "created_at": existing["created_at"],
                             "input_hash": state["input_hash"],
                             "deduplicated": True})
                    cur = self.server.conn.execute(
                        "INSERT INTO runs(input_hash,params_json,result_json,"
                        "version_snapshot_json) VALUES (?,?,?,?)",
                        (state["input_hash"], params_json,
                         json.dumps(result, ensure_ascii=False),
                         json.dumps(snap, ensure_ascii=False)))
                    self.server.conn.commit()
                    run_id = cur.lastrowid
                from datetime import datetime
                return self._json({"ok": True, "run_id": run_id,
                                   "created_at": datetime.now().isoformat(
                                       timespec="seconds"),
                                   "input_hash": state["input_hash"],
                                   "deduplicated": False})
            if path == "/api/reseed":
                with _LOCK:
                    self.server.conn.close()
                    if os.path.exists(os.environ.get(
                            "CLOSURE_DB", os.path.join(
                                os.path.dirname(__file__), "closure.db"))):
                        os.remove(os.environ.get(
                            "CLOSURE_DB", os.path.join(
                                os.path.dirname(__file__), "closure.db")))
                    self.server.conn = connect()
                    seed.seed(self.server.conn)
                return self._json({"ok": True})
        except (ValueError, KeyError) as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # surface unexpected errors to the UI
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        self.send_error(404)

    def do_PATCH(self):
        parts = self.path.rstrip("/").split("/")
        if len(parts) == 4 and parts[:3] == ["", "api", "observations"]:
            try:
                oid = int(parts[3])
            except ValueError:
                return self.send_error(404)
            body = self._body()
            fields, vals = [], []
            if "weight" in body:
                fields.append("weight=?")
                vals.append(float(body["weight"]))
            if "ignored" in body:
                fields.append("ignored=?")
                vals.append(1 if body["ignored"] else 0)
            if "measured_at" in body:
                fields.append("measured_at=?")
                vals.append(str(body["measured_at"]))
            if not fields:
                return self._json({"error": "无可更新字段"}, 400)
            with _LOCK:
                cur = self.server.conn.execute(
                    f"UPDATE observations SET {', '.join(fields)} WHERE id=?",
                    vals + [oid])
                self.server.conn.commit()
            if cur.rowcount == 0:
                return self._json({"error": f"观测 {oid} 不存在"}, 404)
            return self._json({"ok": True})
        self.send_error(404)


def main(port=8080):
    db_path = os.environ.get(
        "CLOSURE_DB", os.path.join(os.path.dirname(__file__), "closure.db"))
    fresh = not os.path.exists(db_path)
    conn = connect(db_path)
    init_db(conn)
    if fresh:
        seed.seed(conn)
        print("已初始化演示数据")
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.conn = conn
    print(f"盾构闭合工作台: http://localhost:{port}")
    server.serve_forever()


if __name__ == "__main__":
    import sys
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8080)
