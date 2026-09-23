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


RULE_SID = "test-rules-session"

RULE_CSV = (
    "选手,评委,项目,分数\n"
    "C1,JA,P1,8.30\nC2,JA,P1,8.00\nC3,JA,P1,7.90\nC4,JA,P1,7.80\n"
    "C1,JB,P1,8.30\nC2,JB,P1,8.00\nC3,JB,P1,7.85\nC4,JB,P1,7.75\n"
    "C1,JC,P1,5.0\nC2,JC,P1,9.5\nC3,JC,P1,7.2\nC4,JC,P1,6.8\n"
)


def _rules_body():
    return {
        "rules": [
            {
                "name": "宽评委极端高分",
                "priority": 1,
                "judge": "JC",
                "raw_min": 9.0,
                "use_raw": True,
                "reason_template": "{评委}给出{原始分}异常偏高，规则「{规则}」保留原分",
            },
            {
                "name": "宽评委极端低分",
                "priority": 1,
                "judge": "JC",
                "raw_max": 5.0,
                "use_raw": True,
                "reason_template": "{评委}给出{原始分}异常偏低，保留原分",
            },
            {
                "name": "其余采用校准分",
                "priority": 100,
                "use_raw": False,
                "reason_template": "默认采用校准分",
            },
        ]
    }


class TestBatchRulesAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        client.delete(f"/api/sessions/{RULE_SID}")
        r = client.post(
            f"/api/sessions/{RULE_SID}/upload?scope=judge",
            files={"file": ("s.csv", RULE_CSV.encode("utf-8-sig"), "text/csv")},
        )
        assert r.status_code == 200, r.text

    def test_01_rules_empty_initially_and_404_without_session(self):
        r = client.get(f"/api/sessions/{RULE_SID}/rules")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["rules"], [])
        self.assertEqual(
            client.get("/api/sessions/no-such-session/rules").status_code, 404
        )

    def test_02_preview_stats_and_conflicts_without_writing(self):
        r = client.post(f"/api/sessions/{RULE_SID}/rules/preview", json=_rules_body())
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        by_name = {x["name"]: x for x in data["rules"]}

        # Raw ranges: JC >= 9 hits C2's 9.5 (1); JC <= 5 hits C1's 5.0 (1);
        # catch-all hits every score (12).
        self.assertEqual(by_name["宽评委极端高分"]["hits"], 1)
        self.assertEqual(by_name["宽评委极端高分"]["misses"], 11)
        self.assertEqual(by_name["宽评委极端低分"]["hits"], 1)
        self.assertEqual(by_name["其余采用校准分"]["hits"], 12)

        # The two JC scores match both a specific rule and the catch-all.
        self.assertEqual(data["summary"]["conflicts"], 2)
        self.assertEqual(by_name["其余采用校准分"]["conflicts"], 2)
        self.assertEqual(by_name["其余采用校准分"]["chosen"], 10)
        self.assertEqual(by_name["其余采用校准分"]["lost_to_priority"], 2)
        self.assertEqual(data["summary"]["matched"], 12)
        self.assertEqual(data["summary"]["unmatched"], 0)
        self.assertEqual(data["summary"]["will_change"], 12)

        # Conflicts carry unique winners resolved by priority.
        for conflict in data["conflicts"]:
            self.assertEqual(len(conflict["matched_indexes"]), 2)
            self.assertIn(conflict["winner_name"], ("宽评委极端高分", "宽评委极端低分"))
            self.assertEqual(conflict["winner_priority"], 1)

        # Reason template rendering (Chinese placeholders).
        self.assertIn("JC", by_name["宽评委极端高分"]["reason_sample"])
        self.assertIn("9.5", by_name["宽评委极端高分"]["reason_sample"])

        # Preview must not touch decisions or persist rules.
        analysis = client.get(f"/api/sessions/{RULE_SID}/analysis").json()
        self.assertEqual(analysis["counts"]["decisions"], 0)
        self.assertEqual(client.get(f"/api/sessions/{RULE_SID}/rules").json()["rules"], [])

    def test_03_apply_is_transactional_and_refreshes_ranking(self):
        before = client.get(f"/api/sessions/{RULE_SID}/analysis").json()
        r = client.post(f"/api/sessions/{RULE_SID}/rules/apply", json=_rules_body())
        self.assertEqual(r.status_code, 200, r.text)
        after = r.json()
        self.assertEqual(
            after["batch_result"],
            {"rules": 3, "written": 12, "changed": 12,
             "matched": 12, "unmatched": 0, "conflicts": 2},
        )
        # Exactly the two extreme JC scores keep raw; everyone else calibrated.
        raw_decisions = []
        for block in after["contestants"]:
            for ib in block["items"]:
                for s in ib["scores"]:
                    if s["use_raw"]:
                        raw_decisions.append(s)
        self.assertEqual(len(raw_decisions), 2)
        self.assertEqual({(s["judge"], s["raw"]) for s in raw_decisions}, {("JC", 9.5), ("JC", 5.0)})
        self.assertIn("异常偏高", next(s["reason"] for s in raw_decisions if s["raw"] == 9.5))
        # Ranking payload is the refreshed analysis.
        self.assertIn("contestants", after)
        self.assertEqual(after["counts"]["scores"], 12)
        self.assertEqual(after["counts"]["decisions"], 2)
        self.assertNotEqual(
            after["contestants"][0]["eff_total"], before["contestants"][0]["eff_total"]
        )
        # Rules are now persisted.
        saved = client.get(f"/api/sessions/{RULE_SID}/rules").json()["rules"]
        self.assertEqual([r["name"] for r in saved],
                         ["宽评委极端高分", "宽评委极端低分", "其余采用校准分"])
        self.assertTrue(all("id" in r and "position" in r for r in saved))

    def test_04_reapply_reports_zero_changes_and_provenance_rebinds(self):
        # Same payload again: matched/written still 12, changed == 0.
        r = client.post(f"/api/sessions/{RULE_SID}/rules/apply", json=_rules_body())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["batch_result"]["changed"], 0)
        self.assertEqual(r.json()["batch_result"]["written"], 12)

    def test_05_unmatched_scores_keep_existing_decisions(self):
        # A rule that matches only JA scores -> JC raw decisions must survive.
        body = {"rules": [{
            "name": "仅JA", "priority": 1, "judge": "JA",
            "use_raw": False, "reason_template": "JA校准",
        }]}
        r = client.post(f"/api/sessions/{RULE_SID}/rules/apply", json=body)
        self.assertEqual(r.status_code, 200)
        br = r.json()["batch_result"]
        self.assertEqual(br["matched"], 4)
        self.assertEqual(br["unmatched"], 8)
        self.assertEqual(r.json()["counts"]["decisions"], 2)  # JC raw kept

    def test_06_save_rules_without_decisions_and_delete(self):
        body = {"rules": [{"name": "仅保存", "use_raw": True, "reason_template": "x"}]}
        r = client.post(f"/api/sessions/{RULE_SID}/rules", json=body)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["rules"][0]["name"], "仅保存")
        # Decisions untouched by save.
        self.assertEqual(
            client.get(f"/api/sessions/{RULE_SID}/analysis").json()["counts"]["decisions"], 2
        )
        r2 = client.delete(f"/api/sessions/{RULE_SID}/rules")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["rules"], [])
        # Deleting rules must not delete the decisions they produced.
        self.assertEqual(
            client.get(f"/api/sessions/{RULE_SID}/analysis").json()["counts"]["decisions"], 2
        )

    def test_07_validation_errors(self):
        # Unknown judge / item -> 400.
        r = client.post(
            f"/api/sessions/{RULE_SID}/rules/preview",
            json={"rules": [{"name": "X", "judge": "不存在", "use_raw": True}]},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("评委", r.json()["detail"])
        r = client.post(
            f"/api/sessions/{RULE_SID}/rules/preview",
            json={"rules": [{"name": "X", "item": "不存在", "use_raw": True}]},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("项目", r.json()["detail"])
        # Inverted interval -> 422 from pydantic model validation.
        r = client.post(
            f"/api/sessions/{RULE_SID}/rules/preview",
            json={"rules": [{"name": "X", "raw_min": 9, "raw_max": 1, "use_raw": True}]},
        )
        self.assertEqual(r.status_code, 422)
        # Empty rule set -> 422.
        r = client.post(f"/api/sessions/{RULE_SID}/rules/preview", json={"rules": []})
        self.assertEqual(r.status_code, 422)

    def test_08_scope_switch_changes_calibrated_hits(self):
        client.post(f"/api/sessions/{RULE_SID}/scope", json={"scope": "judge_item"})
        body = {"rules": [{
            "name": "校准区间", "cal_min": 0, "cal_max": 100, "use_raw": False,
        }]}
        r = client.post(f"/api/sessions/{RULE_SID}/rules/preview", json=body)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["scope"], "judge_item")
        self.assertEqual(r.json()["summary"]["matched"], 12)
        client.post(f"/api/sessions/{RULE_SID}/scope", json={"scope": "judge"})

    def test_09_apply_without_data_400_and_cleared_session_404(self):
        client.delete("/api/sessions/empty-rules-session")
        client.post("/api/sessions/empty-rules-session/scope", json={"scope": "judge"})
        r = client.post(
            "/api/sessions/empty-rules-session/rules/apply",
            json={"rules": [{"use_raw": True}]},
        )
        self.assertEqual(r.status_code, 400)
        r = client.post(
            "/api/sessions/no-such-session/rules/apply",
            json={"rules": [{"use_raw": True}]},
        )
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
