"""End-to-end API tests with a temporary data directory."""
import os
import sys
import tempfile
import unittest

_tmp = tempfile.mkdtemp(prefix="calib_test_")
os.environ["CALIB_DATA_DIR"] = _tmp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import database as db  # noqa: E402
from app.main import app  # noqa: E402

db.init_db()
client = TestClient(app)
SID = "test-session"

CSV_BODY = (
    "选手,评委,项目,分数\n"
    "C1,JA,P1,8.30\nC2,JA,P1,8.00\nC3,JA,P1,7.90\nC4,JA,P1,7.80\n"
    "C1,JB,P1,8.30\nC2,JB,P1,8.00\nC3,JB,P1,7.85\nC4,JB,P1,7.75\n"
    "C1,JC,P1,5.0\nC2,JC,P1,9.5\nC3,JC,P1,7.2\nC4,JC,P1,6.8\n"
)


class TestAPI(unittest.TestCase):
    def test_01_health_and_empty(self):
        self.assertEqual(client.get("/api/health").json()["status"], "ok")
        r = client.get(f"/api/sessions/{SID}/analysis")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["counts"]["scores"], 0)

    def test_02_upload_errors(self):
        r = client.post(
            f"/api/sessions/{SID}/upload?scope=judge",
            files={"file": ("bad.csv", "选手,评委,分数\nA,J,9".encode(), "text/csv")},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("项目", r.json()["detail"])

    def test_03_upload_ok(self):
        r = client.post(
            f"/api/sessions/{SID}/upload?scope=judge",
            files={"file": ("s.csv", CSV_BODY.encode("utf-8-sig"), "text/csv")},
        )
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["counts"]["scores"], 12)
        self.assertGreaterEqual(len(data["reversals"]), 1)

    def test_04_persists_across_requests(self):
        r = client.get(f"/api/sessions/{SID}/analysis")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["counts"]["scores"], 12)

    def test_05_decision_and_rerank(self):
        # Override C1's first raw score (id 1) and verify effective totals change.
        before = client.get(f"/api/sessions/{SID}/analysis").json()
        r = client.patch(
            f"/api/sessions/{SID}/scores/1/decision",
            json={"use_raw": True, "reason": "复核确认"},
        )
        self.assertEqual(r.status_code, 200)
        after = r.json()
        self.assertEqual(after["counts"]["decisions"], 1)
        self.assertNotEqual(
            after["contestants"][0]["eff_total"], before["contestants"][0]["eff_total"]
        )

    def test_06_scope_switch(self):
        r = client.post(f"/api/sessions/{SID}/scope", json={"scope": "judge_item"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["scope"], "judge_item")
        self.assertEqual(len(r.json()["groups"]), 3)
        # switch back
        client.post(f"/api/sessions/{SID}/scope", json={"scope": "judge"})

    def test_07_reset(self):
        r = client.post(f"/api/sessions/{SID}/decisions/reset")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["counts"]["decisions"], 0)

    def test_08_export(self):
        r = client.get(f"/api/sessions/{SID}/export")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r.headers["content-type"])
        self.assertIn("attachment", r.headers["content-disposition"])
        self.assertIn("审计".encode(), r.content)

    def test_09_sample(self):
        r = client.post(f"/api/sessions/{SID}/sample?scope=judge")
        self.assertEqual(r.status_code, 200)
        self.assertGreater(r.json()["counts"]["scores"], 0)

    def test_10_stability_experiment(self):
        # Sample is loaded (6 contestants x 4 judges x 3 items).
        analysis = client.get(f"/api/sessions/{SID}/analysis").json()
        judge = analysis["judges"][0]
        n_scores = analysis["counts"]["scores"]

        r = client.post(
            f"/api/sessions/{SID}/stability",
            json={"exclude_type": "judge", "exclude_name": judge},
        )
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["exclusion"]["type"], "judge")
        self.assertEqual(data["exclusion"]["name"], judge)
        self.assertEqual(data["baseline_counts"]["scores"], n_scores)
        # Every contestant x item has one score per judge -> 6 * 3 removed.
        self.assertEqual(data["removed_scores"], 18)
        self.assertEqual(data["scenario_counts"]["scores"], n_scores - 18)
        self.assertEqual(len(data["contestants"]), analysis["counts"]["contestants"])

        # Baseline totals are echoed and every row carries deltas.
        base_blocks = {b["contestant"]: b for b in analysis["contestants"]}
        for row in data["contestants"]:
            self.assertTrue(row["present"])
            self.assertEqual(row["baseline"]["eff_rank"], base_blocks[row["contestant"]]["eff_rank"])
            for kind in ("raw", "cal", "eff"):
                self.assertIn(f"{kind}_rank_delta", row)
                self.assertIn(f"{kind}_total_delta", row)
            self.assertGreaterEqual(row["affected_scores"], 1)

        # Item exclusion too.
        item = analysis["items"][0]
        r2 = client.post(
            f"/api/sessions/{SID}/stability",
            json={"exclude_type": "item", "exclude_name": item},
        )
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["exclusion"]["type"], "item")
        self.assertEqual(r2.json()["removed_scores"], 24)  # 6 contestants x 4 judges

        # The experiment must not mutate scores/decisions: baseline is unchanged.
        again = client.get(f"/api/sessions/{SID}/analysis").json()
        self.assertEqual(again["counts"]["scores"], n_scores)
        self.assertEqual(len(again["judges"]), 4)

    def test_11_stability_errors(self):
        # Unknown judge name -> 400.
        r = client.post(
            f"/api/sessions/{SID}/stability",
            json={"exclude_type": "judge", "exclude_name": "不存在的评委"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("评委", r.json()["detail"])

        # Bad payload -> 422 from pydantic.
        r2 = client.post(
            f"/api/sessions/{SID}/stability",
            json={"exclude_type": "round", "exclude_name": "X"},
        )
        self.assertEqual(r2.status_code, 422)

        # Unknown session -> 404.
        r3 = client.post(
            "/api/sessions/no-such-session/stability",
            json={"exclude_type": "judge", "exclude_name": "J"},
        )
        self.assertEqual(r3.status_code, 404)

    def test_12_clear(self):
        r = client.delete(f"/api/sessions/{SID}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["cleared"])
        r2 = client.get(f"/api/sessions/{SID}/analysis")
        self.assertEqual(r2.json()["counts"]["scores"], 0)
        # Export after clearing must fail
        self.assertEqual(client.get(f"/api/sessions/{SID}/export").status_code, 404)
        # Stability experiment on an empty session must also fail
        r3 = client.post(
            f"/api/sessions/{SID}/stability",
            json={"exclude_type": "judge", "exclude_name": "J"},
        )
        self.assertEqual(r3.status_code, 404)


if __name__ == "__main__":
    unittest.main()
