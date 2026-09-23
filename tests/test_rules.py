"""Tests for the batch adjudication rule engine."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.rules import (
    evaluate_rules,
    normalize_rule,
    render_reason,
    rule_matches,
    winning_outcomes,
)


def scores(data):
    # data: list of (id, contestant, judge, item, raw, calibrated)
    return [
        {"id": i, "contestant": c, "judge": j, "item": it, "raw": rw, "calibrated": cl}
        for i, (c, j, it, rw, cl) in enumerate(data, start=1)
    ]


SCORES = scores([
    ("C1", "JA", "P1", 9.5, 8.5),
    ("C2", "JA", "P1", 5.0, 7.0),
    ("C1", "JB", "P1", 9.0, 8.0),
    ("C2", "JB", "P1", 6.0, 7.5),
    ("C1", "JA", "P2", 8.0, 8.2),
])


class TestRuleMatching(unittest.TestCase):
    def test_empty_rule_matches_all(self):
        rule = normalize_rule({})
        self.assertTrue(all(rule_matches(s, rule) for s in SCORES))

    def test_judge_and_item_equality(self):
        rule = normalize_rule({"judge": "JA"})
        self.assertEqual({s["id"] for s in SCORES if rule_matches(s, rule)}, {1, 2, 5})
        rule = normalize_rule({"judge": "JA", "item": "P1"})
        self.assertEqual({s["id"] for s in SCORES if rule_matches(s, rule)}, {1, 2})

    def test_raw_and_calibrated_intervals_inclusive(self):
        rule = normalize_rule({"raw_min": 6.0, "raw_max": 9.0})
        self.assertEqual({s["id"] for s in SCORES if rule_matches(s, rule)}, {3, 4, 5})
        rule = normalize_rule({"cal_min": 8.0})
        self.assertEqual({s["id"] for s in SCORES if rule_matches(s, rule)}, {1, 3, 5})
        rule = normalize_rule({"cal_max": 7.5})
        self.assertEqual({s["id"] for s in SCORES if rule_matches(s, rule)}, {2, 4})
        # Endpoints are inclusive.
        rule = normalize_rule({"raw_min": 9.5})
        self.assertEqual({s["id"] for s in SCORES if rule_matches(s, rule)}, {1})

    def test_combined_conditions(self):
        rule = normalize_rule({"judge": "JA", "raw_min": 9.0, "cal_max": 8.5})
        hit = [s["id"] for s in SCORES if rule_matches(s, rule)]
        self.assertEqual(hit, [1])


class TestEvaluate(unittest.TestCase):
    def test_priority_resolves_conflict_to_unique_winner(self):
        rules = [
            {"name": "low-prio-raw", "priority": 10, "judge": "JA", "use_raw": True},
            {"name": "high-prio-cal", "priority": 1, "use_raw": False},
        ]
        res = evaluate_rules(SCORES, rules)
        # Every score matches the catch-all; JA's scores match both -> 3 conflicts.
        self.assertEqual(res["summary"]["matched"], 5)
        self.assertEqual(res["summary"]["unmatched"], 0)
        self.assertEqual(res["summary"]["conflicts"], 3)
        # Unique winner per score; the priority-1 rule takes the conflicts.
        by_id = {row["score_id"]: row for row in res["scores"]}
        self.assertEqual(by_id[1]["winner_index"], 1)
        self.assertEqual(by_id[1]["use_raw"], False)
        self.assertEqual(by_id[3]["winner_index"], 1)
        # Rule 0 only keeps the two scores where it is the unique/top match...
        stats = {r["name"]: r for r in res["rules"]}
        self.assertEqual(stats["low-prio-raw"]["hits"], 3)
        self.assertEqual(stats["low-prio-raw"]["chosen"], 0)
        self.assertEqual(stats["low-prio-raw"]["lost_to_priority"], 3)
        self.assertEqual(stats["high-prio-cal"]["chosen"], 5)
        outcomes = winning_outcomes(res)
        self.assertEqual(len(outcomes), 5)
        self.assertTrue(all(o["use_raw"] is False for o in outcomes))

    def test_equal_priority_broken_by_order(self):
        rules = [
            {"name": "first", "priority": 5, "judge": "JA", "use_raw": True},
            {"name": "second", "priority": 5, "judge": "JA", "use_raw": False},
        ]
        res = evaluate_rules(SCORES, rules)
        by_id = {row["score_id"]: row for row in res["scores"]}
        self.assertTrue(all(by_id[sid]["winner_index"] == 0 for sid in (1, 2, 5)))
        self.assertTrue(all(by_id[sid]["use_raw"] is True for sid in (1, 2, 5)))

    def test_hit_miss_and_unmatched_counts(self):
        rules = [{"name": "JB-high", "judge": "JB", "raw_min": 9.0, "use_raw": True}]
        res = evaluate_rules(SCORES, rules)
        stat = res["rules"][0]
        self.assertEqual(stat["hits"], 1)
        self.assertEqual(stat["misses"], 4)
        self.assertEqual(stat["conflicts"], 0)
        self.assertEqual(res["summary"]["matched"], 1)
        self.assertEqual(res["summary"]["unmatched"], 4)
        self.assertEqual(res["summary"]["conflicts"], 0)

    def test_will_change_against_current_decisions(self):
        rules = [{"name": "catchall-raw", "use_raw": True, "reason_template": "批量保留"}]
        # Score 1 already raw with same reason -> unchanged; score 3 already
        # raw but with a different reason -> changed.
        current = {1: (True, "批量保留"), 3: (True, "旧理由")}
        res = evaluate_rules(SCORES, rules, current_decisions=current)
        self.assertEqual(res["summary"]["will_change"], 4)
        self.assertEqual(res["summary"]["unchanged"], 1)
        self.assertEqual(res["summary"]["manual_override"], 2)
        by_id = {row["score_id"]: row for row in res["scores"]}
        self.assertFalse(by_id[1]["will_change"])
        self.assertTrue(by_id[3]["will_change"])

    def test_no_rules(self):
        res = evaluate_rules(SCORES, [])
        self.assertEqual(res["rules"], [])
        self.assertEqual(res["scores"], [])
        self.assertEqual(res["conflicts"], [])
        self.assertEqual(res["summary"]["matched"], 0)
        self.assertEqual(res["summary"]["unmatched"], 5)

    def test_inputs_not_mutated(self):
        rules = [{"name": "r", "judge": "JA", "use_raw": True}]
        before = [dict(s) for s in SCORES]
        evaluate_rules(SCORES, rules)
        self.assertEqual(SCORES, before)


class TestReasonTemplate(unittest.TestCase):
    def test_english_and_chinese_placeholders(self):
        score = {"contestant": "C1", "judge": "JA", "item": "P1", "raw": 9.5, "calibrated": 8.5}
        rule = {"name": "高分区"}
        text = render_reason(
            "{judge}评委给{选手}的{原始分}分（校准{calibrated}）命中{规则}", score, rule
        )
        self.assertEqual(text, "JA评委给C1的9.5分（校准8.5）命中高分区")

    def test_action_placeholder(self):
        score = {"contestant": "C1", "judge": "JA", "item": "P1", "raw": 9.5, "calibrated": 8.5}
        raw_text = render_reason("本规则动作：{动作}", score, {"use_raw": True})
        cal_text = render_reason("本规则动作：{动作}", score, {"use_raw": False})
        self.assertEqual(raw_text, "本规则动作：保留原分")
        self.assertEqual(cal_text, "本规则动作：采用校准分")

    def test_empty_template(self):
        self.assertEqual(render_reason("", SCORES[0], {"name": "r"}), "")

    def test_broken_template_is_kept_literal(self):
        text = render_reason("理由 {0", SCORES[0], {"name": "r"})
        self.assertEqual(text, "理由 {0")

    def test_sample_reason_in_stats(self):
        rules = [{"name": "JA", "judge": "JA", "reason_template": "{judge}:{原始分}"}]
        res = evaluate_rules(SCORES, rules)
        self.assertIn("JA:", res["rules"][0]["reason_sample"])
        # Rule that never hits has no sample but remains present.
        res2 = evaluate_rules(SCORES, [{"name": "none", "judge": "ZZ", "reason_template": "x"}])
        self.assertEqual(res2["rules"][0]["reason_sample"], "")


if __name__ == "__main__":
    unittest.main()
