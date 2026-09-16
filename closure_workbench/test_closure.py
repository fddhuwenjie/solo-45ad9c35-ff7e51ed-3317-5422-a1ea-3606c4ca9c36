"""Regression tests for the closure main chain.

Covers:
  * 3 stations sharing one CP: pairwise-evidence isolation -> unique path
  * each of the three blockers alone (time / version cross / cycle)
  * all three blockers together
  * clean state -> unique path AND usable adjustment (status ok / RMS)
  * blocked state -> line adjustment is stopped (no status ok / RMS / rows)
  * cut_edges match the actual offending observations
"""
import os
import tempfile
import unittest

from db import connect
import engine
import seed


class ClosureCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.path)
        self.conn = connect(self.path)
        seed.seed(self.conn)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    # ------------------------------------------------------------ helpers
    def ids(self, kind=None, station=None, cp=None, version=None):
        st = engine.State(self.conn)
        return [o["id"] for o in st.obs
                if (kind is None or o["kind"] == kind)
                and (station is None or o["station"] == station)
                and (cp is None or o["target_cp"] == cp)
                and (version is None or o["version_id"] == version)]

    def assemble(self, pw=engine.PROCESS_WEIGHT):
        return engine.assemble(self.conn, process_weight=pw)

    def clear_blockers(self, *, ignore_cycle=True, fix_time=True,
                       confirm_g2=True):
        """Resolve the three seeded blockers, one switch each."""
        if ignore_cycle:
            eid = self.ids(station="S0", cp="CP2", version="R0")[0]
            self.conn.execute("UPDATE observations SET ignored=1 WHERE id=?",
                              (eid,))
        if fix_time:
            # the seeded early S2->CP2 resurvey (time inversion)
            st = engine.State(self.conn)
            move = next(m["move_time"] for m in st.moves
                        if m["to_station"] == "S2")
            bad = [o["id"] for o in st.obs if o["kind"] == "resurvey"
                   and o["station"] == "S2" and o["measured_at"] < move]
            self.conn.execute("UPDATE observations SET measured_at=? WHERE id=?",
                              ("2026-08-22T07:30", bad[0]))
        self.conn.commit()
        if confirm_g2:
            import server
            server.confirm_version(self.conn, "G2", "BASE", "cp_abs")

    def assert_adjustment_blocked(self, r):
        a = r["adjustment"]
        self.assertEqual(a["status"], "blocked")
        self.assertIsNone(a["rms"])
        self.assertIsNone(a["max_residual"])
        self.assertIsNone(a["max_obs_id"])
        self.assertEqual(a["obs"], [])
        self.assertEqual(a["rings"], [])

    # ------------------------------------------------------------ tests
    def test_01_all_three_blockers_together(self):
        r = self.assemble()
        self.assertTrue(r["blocked"])
        types = {c["type"] for c in r["topology"]["conflicts"]}
        self.assertEqual(
            types,
            {"time_inversion", "version_cross", "contradiction_cycle"})
        # no usable adjustment while any blocker is present
        self.assert_adjustment_blocked(r)
        # cut edges point at exactly the real culprits
        cut = set(r["topology"]["cut_edges"])
        bad_time = self.ids(station="S2", cp="CP2", version="R3")[0]
        bad_cycle = self.ids(station="S0", cp="CP2", version="R0")[0]
        st = engine.State(self.conn)
        g2_at_60 = next(o["id"] for o in st.obs
                        if o["kind"] == "guide" and o["version_id"] == "G2"
                        and o["ring_no"] == 60)
        self.assertIn(bad_time, cut)
        self.assertIn(bad_cycle, cut)
        self.assertIn(g2_at_60, cut)
        # every reported conflict carries at least one highlighted edge
        for c in r["topology"]["conflicts"]:
            self.assertTrue(c["cut_edges"], c)
            self.assertTrue(set(c["cut_edges"]) <= cut)

    def test_02_three_stations_share_cp_pairwise_isolation(self):
        """CP2/CP3 are each observed by S0,S1,S2.  An early S2 observation
        must only be flagged on the pair that actually contains it; the
        S0->S1 pair on the same CP stays valid, and one timely edge on a
        station is enough to keep its pair evidence valid."""
        r = self.assemble()
        links = r["topology"]["links"]
        s01_cp2 = next(l for l in links
                       if (l["from"], l["to"], l["cp"]) ==
                       ("S0", "S1", "CP2"))
        s01_cp3 = next(l for l in links
                       if (l["from"], l["to"], l["cp"]) ==
                       ("S0", "S1", "CP3"))
        self.assertTrue(s01_cp2["time_ok"],
                        "S0->S1@CP2 must stay valid despite S2's bad timing")
        self.assertTrue(s01_cp3["time_ok"])
        # the early S2->CP2 resurvey is attached to the pairs containing S2
        s12_cp2 = next(l for l in links
                       if (l["from"], l["to"], l["cp"]) ==
                       ("S1", "S2", "CP2"))
        bad_time = self.ids(station="S2", cp="CP2", version="R3")[0]
        self.assertIn(bad_time, s12_cp2["early_evidence_ids"])
        self.assertNotIn(bad_time, s01_cp2["pair_obs_ids"])
        # S2 has a later valid CP2 edge, so the pair still has usable evidence
        self.assertTrue(s12_cp2["time_ok"])
        # each link evidence only contains observations of its own pair
        st = engine.State(self.conn)
        by_id = {o["id"]: o for o in st.obs}
        for l in links:
            pair = {l["from"], l["to"]}
            self.assertTrue(
                {by_id[i]["station"] for i in l["evidence_ids"]} <= pair)
            self.assertTrue(
                {by_id[i]["station"] for i in l["pair_obs_ids"]} <= pair)
        # topology still resolves to exactly one station succession
        self.assertEqual(r["topology"]["path_status"], "unique")
        self.assertEqual(r["topology"]["paths"][0]["stations"],
                         ["S0", "S1", "S2", "S3"])

    def test_03_clean_unique_path_and_adjustment(self):
        self.clear_blockers()
        r = self.assemble()
        self.assertFalse(r["blocked"])
        self.assertEqual(r["topology"]["conflicts"], [])
        self.assertEqual(r["topology"]["path_status"], "unique")
        self.assertEqual(
            r["topology"]["paths"][0]["stations"],
            ["S0", "S1", "S2", "S3"])
        self.assertEqual(r["topology"]["cut_edges"], [])
        a = r["adjustment"]
        self.assertEqual(a["status"], "ok")
        self.assertIsNotNone(a["rms"])
        self.assertGreaterEqual(a["rms"], 0.0)
        self.assertTrue(any(o["status"] == "ok" for o in a["obs"]))
        # all line points converted (SG2 now chains via confirmed G2)
        self.assertFalse(any(o["status"] == "unconverted" for o in a["obs"]))

    def test_04_only_time_inversion(self):
        # remove the other two blockers: ignore blundered cycle edge and
        # confirm G2, but leave the early S2 resurvey untouched
        self.conn.execute(
            "UPDATE observations SET ignored=1 WHERE id=?",
            (self.ids(station="S0", cp="CP2", version="R0")[0],))
        self.conn.commit()
        import server
        server.confirm_version(self.conn, "G2", "BASE", "cp_abs")
        r = self.assemble()
        types = {c["type"] for c in r["topology"]["conflicts"]}
        self.assertEqual(types, {"time_inversion"})
        # topology is still unique, but closure is blocked by the time flag
        self.assertEqual(r["topology"]["path_status"], "unique")
        self.assertTrue(r["blocked"])
        self.assert_adjustment_blocked(r)
        cut = set(r["topology"]["cut_edges"])
        bad_time = self.ids(station="S2", cp="CP2", version="R3")[0]
        self.assertEqual(cut, {bad_time})

    def test_05_only_version_cross(self):
        # fix time + ignore blundered edge, keep G2 unconfirmed
        self.clear_blockers(ignore_cycle=True, fix_time=True, confirm_g2=False)
        r = self.assemble()
        types = {c["type"] for c in r["topology"]["conflicts"]}
        self.assertEqual(types, {"version_cross"})
        self.assert_adjustment_blocked(r)
        cross = next(c for c in r["topology"]["conflicts"]
                     if c["type"] == "version_cross")
        self.assertEqual(cross["ring"], 60)
        cut = set(r["topology"]["cut_edges"])
        st = engine.State(self.conn)
        g2_at_60 = next(o["id"] for o in st.obs
                        if o["kind"] == "guide" and o["version_id"] == "G2"
                        and o["ring_no"] == 60)
        self.assertIn(g2_at_60, cut)
        # cut edges all belong to the unresolved family F2
        by_id = {o["id"]: o for o in st.obs}
        self.assertTrue(all(by_id[i]["version_id"] in ("G2", "SG2")
                            for i in cut))

    def test_06_only_contradiction_cycle(self):
        # fix time + confirm G2, but leave the 18 mm blunder edge live
        self.clear_blockers(ignore_cycle=False, fix_time=True, confirm_g2=True)
        r = self.assemble()
        types = {c["type"] for c in r["topology"]["conflicts"]}
        self.assertTrue(types <= {"contradiction_cycle"})
        self.assertTrue(types)
        self.assert_adjustment_blocked(r)
        bad = self.ids(station="S0", cp="CP2", version="R0")[0]
        self.assertEqual(r["topology"]["cut_edges"], [bad])
        for c in r["topology"]["conflicts"]:
            self.assertIn(bad, c["cut_edges"])

    def test_07_cycle_disappears_when_blunder_ignored(self):
        self.clear_blockers()
        r = self.assemble()
        self.assertFalse(any(c["type"] == "contradiction_cycle"
                             for c in r["topology"]["conflicts"]))

    def test_08_weight_change_propagates_only_when_not_blocked(self):        # blocked: adjustment must remain stopped regardless of weight
        r1 = self.assemble(pw=1.0)
        r2 = self.assemble(pw=100.0)
        self.assertEqual(r1["adjustment"]["status"], "blocked")
        self.assertEqual(r2["adjustment"]["status"], "blocked")
        # clean: process-edge weight changes the result
        self.clear_blockers()
        lo = self.assemble(pw=1.0)["adjustment"]
        hi = self.assemble(pw=100.0)["adjustment"]
        self.assertEqual(lo["status"], "ok")
        self.assertEqual(hi["status"], "ok")
        self.assertNotEqual((lo["rms"], lo["max_residual"]),
                            (hi["rms"], hi["max_residual"]))


    def test_09_broken_handover_yields_no_path_and_blocks_adjustment(self):
        """Ignoring all S1 resurvey evidence must make the continuation
        enumeration return 0 paths (S0 can no longer hand over), not a wrong
        unique, and stop the adjustment."""
        self.clear_blockers()
        st = engine.State(self.conn)
        s1 = [o["id"] for o in st.obs if o["kind"] == "resurvey"
              and o["station"] == "S1"]
        qmarks = ",".join("?" * len(s1))
        self.conn.execute(
            f"UPDATE observations SET ignored=1 WHERE id IN ({qmarks})", s1)
        self.conn.commit()
        r = self.assemble()
        self.assertEqual(r["topology"]["path_status"], "none")
        self.assertTrue(r["blocked"])
        self.assert_adjustment_blocked(r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
