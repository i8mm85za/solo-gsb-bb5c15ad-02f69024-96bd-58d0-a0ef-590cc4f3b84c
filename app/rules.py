"""Batch adjudication rule engine.

A rule matches a score when *all* of its conditions hold:

- judge / item equality (``None`` means "any")
- raw score interval  ``raw_min <= score.raw <= raw_max`` (open ends allowed)
- calibrated score interval ``cal_min <= score.calibrated <= cal_max``

A matched rule applies an action (keep the raw score or use the calibrated
score) together with a reason template. Templates support ``str.format``
placeholders, e.g. ``{judge}``、``{item}``、``{contestant}``、``{raw}``、
``{calibrated}``、``{rule}`` (Chinese aliases 评委/项目/选手/原始分/校准分/规则
also work); unknown placeholders render as empty strings.

When several rules match the same score the conflict is resolved by priority
(smaller number wins; ties are broken by list order, so the outcome is always
unique). Everything here is pure computation — persistence lives in
``database.py`` and callers decide whether the result is written back.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

EPS = 1e-9

ACTION_KEEP_RAW = "保留原分"
ACTION_USE_CALIBRATED = "采用校准分"

# Placeholder aliases accepted in reason templates.
_CHINESE_ALIASES = {
    "评委": "judge",
    "项目": "item",
    "选手": "contestant",
    "原始分": "raw",
    "校准分": "calibrated",
    "规则": "rule",
    "动作": "action",
}


class _ReasonContext(dict):
    """format_map context: Chinese aliases supported, missing keys -> ''."""

    def __missing__(self, key: str) -> str:
        aliased = _CHINESE_ALIASES.get(key)
        if aliased is not None:
            return str(super().get(aliased, ""))
        return ""


def _num(value: float | None) -> str:
    if value is None:
        return ""
    text = f"{float(value):.4f}".rstrip("0").rstrip(".")
    return text or "0"


def normalize_rule(rule: Mapping[str, Any], index: int = 0) -> dict[str, Any]:
    """Coerce a rule mapping into the canonical engine shape."""
    name = str(rule.get("name") or "").strip()
    return {
        "name": name or f"规则{index + 1}",
        "priority": int(rule.get("priority", 100)),
        "judge": (str(rule["judge"]).strip() or None) if rule.get("judge") is not None else None,
        "item": (str(rule["item"]).strip() or None) if rule.get("item") is not None else None,
        "raw_min": rule.get("raw_min"),
        "raw_max": rule.get("raw_max"),
        "cal_min": rule.get("cal_min"),
        "cal_max": rule.get("cal_max"),
        "use_raw": bool(rule.get("use_raw", False)),
        "reason_template": str(rule.get("reason_template") or "").strip(),
    }


def rule_matches(score: Mapping[str, Any], rule: Mapping[str, Any]) -> bool:
    """Return True iff every non-None condition of the rule covers the score."""
    if rule.get("judge") is not None and score["judge"] != rule["judge"]:
        return False
    if rule.get("item") is not None and score["item"] != rule["item"]:
        return False
    intervals = (
        ("raw", "raw_min", "raw_max"),
        ("calibrated", "cal_min", "cal_max"),
    )
    for field, lo_key, hi_key in intervals:
        value = float(score[field])
        lo, hi = rule.get(lo_key), rule.get(hi_key)
        if lo is not None and value < float(lo) - EPS:
            return False
        if hi is not None and value > float(hi) + EPS:
            return False
    return True


def render_reason(template: str, score: Mapping[str, Any], rule: Mapping[str, Any]) -> str:
    """Render a reason template for one matched score (never raises)."""
    if not template:
        return ""
    ctx = _ReasonContext(
        contestant=score.get("contestant", ""),
        judge=score.get("judge", ""),
        item=score.get("item", ""),
        raw=_num(score.get("raw")),
        calibrated=_num(score.get("calibrated")),
        rule=rule.get("name", ""),
        action=ACTION_KEEP_RAW if rule.get("use_raw") else ACTION_USE_CALIBRATED,
        raw_min=_num(rule.get("raw_min")),
        raw_max=_num(rule.get("raw_max")),
        cal_min=_num(rule.get("cal_min")),
        cal_max=_num(rule.get("cal_max")),
    )
    try:
        return template.format_map(ctx)
    except (ValueError, IndexError, KeyError):
        # Malformed template (stray braces, bad format spec, ...): keep text.
        return template


def _ordered_winners(matched: Mapping[int, Sequence[int]], rules: Sequence[Mapping[str, Any]]):
    """For each score id with >=1 matching rule, pick the unique winner.

    Lower ``priority`` wins; equal priority is broken by list order.
    """
    winners: dict[int, int] = {}
    for score_id, indexes in matched.items():
        if indexes:
            winners[score_id] = min(indexes, key=lambda i: (rules[i]["priority"], i))
    return winners


def evaluate_rules(
    flat_scores: Sequence[Mapping[str, Any]],
    rules: Sequence[Mapping[str, Any]],
    current_decisions: Mapping[int, tuple[bool, str]] | None = None,
) -> dict[str, Any]:
    """Dry-run (or pre-write evaluation) of a batch of rules.

    ``flat_scores`` entries need id/contestant/judge/item/raw/calibrated.
    ``current_decisions`` maps score_id -> (use_raw, reason); a missing entry
    is the default "use calibrated, no reason" state and powers will_change.

    Returns per-rule hit/miss/conflict/change statistics, the full conflict
    list (a score matched by 2+ rules) and the per-score winning outcome.
    Nothing here mutates its inputs.
    """
    current_decisions = current_decisions or {}
    rules = [normalize_rule(r, i) for i, r in enumerate(rules)]
    total = len(flat_scores)

    matched: dict[int, list[int]] = {s["id"]: [] for s in flat_scores}
    for rule_index, rule in enumerate(rules):
        for score in flat_scores:
            if rule_matches(score, rule):
                matched[score["id"]].append(rule_index)

    winners = _ordered_winners(matched, rules)

    rule_stats = []
    for index, rule in enumerate(rules):
        hit_ids = [sid for sid, idxs in matched.items() if index in idxs]
        chosen_ids = [sid for sid, win in winners.items() if win == index]
        conflicts = sum(1 for sid in hit_ids if len(matched[sid]) > 1)
        will_change = 0
        for sid in chosen_ids:
            cur = current_decisions.get(sid, (False, ""))
            score = _score_by_id(flat_scores, sid)
            reason = render_reason(rule["reason_template"], score, rule)
            if (bool(cur[0]), cur[1] or "") != (rule["use_raw"], reason):
                will_change += 1
        sample_score = _score_by_id(flat_scores, hit_ids[0]) if hit_ids else None
        rule_stats.append(
            {
                "index": index,
                "name": rule["name"],
                "priority": rule["priority"],
                "judge": rule["judge"],
                "item": rule["item"],
                "raw_min": rule["raw_min"],
                "raw_max": rule["raw_max"],
                "cal_min": rule["cal_min"],
                "cal_max": rule["cal_max"],
                "use_raw": rule["use_raw"],
                "action_label": ACTION_KEEP_RAW if rule["use_raw"] else ACTION_USE_CALIBRATED,
                "reason_template": rule["reason_template"],
                "reason_sample": (
                    render_reason(rule["reason_template"], sample_score, rule)
                    if sample_score is not None
                    else ""
                ),
                "hits": len(hit_ids),
                "misses": total - len(hit_ids),
                "conflicts": conflicts,
                "chosen": len(chosen_ids),
                "lost_to_priority": len(hit_ids) - len(chosen_ids),
                "will_change": will_change,
            }
        )

    score_rows: list[dict[str, Any]] = []
    conflict_rows: list[dict[str, Any]] = []
    matched_total = 0
    will_change_total = 0
    manual_override = 0
    for score in flat_scores:
        sid = score["id"]
        indexes = matched[sid]
        if not indexes:
            continue
        matched_total += 1
        win_index = winners[sid]
        rule = rules[win_index]
        reason = render_reason(rule["reason_template"], score, rule)
        cur = current_decisions.get(sid, (False, ""))
        changed = (bool(cur[0]), cur[1] or "") != (rule["use_raw"], reason)
        if changed:
            will_change_total += 1
        if sid in current_decisions:
            manual_override += 1
        score_rows.append(
            {
                "score_id": sid,
                "contestant": score["contestant"],
                "judge": score["judge"],
                "item": score["item"],
                "raw": score["raw"],
                "calibrated": score["calibrated"],
                "current_use_raw": bool(cur[0]),
                "matched_indexes": list(indexes),
                "winner_index": win_index,
                "use_raw": rule["use_raw"],
                "reason": reason,
                "will_change": changed,
            }
        )
        if len(indexes) > 1:
            conflict_rows.append(
                {
                    "score_id": sid,
                    "contestant": score["contestant"],
                    "judge": score["judge"],
                    "item": score["item"],
                    "raw": score["raw"],
                    "calibrated": score["calibrated"],
                    "matched_indexes": list(indexes),
                    "matched_names": [rules[i]["name"] for i in indexes],
                    "matched_priorities": [rules[i]["priority"] for i in indexes],
                    "winner_index": win_index,
                    "winner_name": rule["name"],
                    "winner_priority": rule["priority"],
                }
            )

    return {
        "total_scores": total,
        "rules": rule_stats,
        "scores": score_rows,
        "conflicts": conflict_rows,
        "summary": {
            "matched": matched_total,
            "unmatched": total - matched_total,
            "conflicts": len(conflict_rows),
            "affected": matched_total,
            "will_change": will_change_total,
            "unchanged": matched_total - will_change_total,
            "manual_override": manual_override,
        },
        "winners": winners,
    }


def _score_by_id(flat_scores: Sequence[Mapping[str, Any]], score_id: int):
    for score in flat_scores:
        if score["id"] == score_id:
            return score
    return None


def winning_outcomes(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract persistable (score_id, use_raw, reason) rows from a result."""
    by_id = {row["score_id"]: row for row in result["scores"]}
    return [
        {
            "score_id": sid,
            "use_raw": by_id[sid]["use_raw"],
            "reason": by_id[sid]["reason"],
        }
        for sid in result["winners"]
    ]
