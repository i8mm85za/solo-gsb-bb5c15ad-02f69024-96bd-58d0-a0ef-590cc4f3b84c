"""FastAPI application: multi-judge score calibration."""
from __future__ import annotations

import os
import math
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import database as db, rules
from .csv_io import CSVParseError, build_audit_csv, parse_scores
from .scoring import SCOPE_LABELS, SCOPES, SCOPE_JUDGE, analyze, stability_experiment

SAMPLE_PATH = os.environ.get(
    "CALIB_SAMPLE_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples", "sample_scores.csv"),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="多评委评分校准", version="1.0.0", lifespan=lifespan)


def _build_analysis(conn, session_id: str, scope: str) -> dict[str, Any]:
    records = db.load_scores(conn, session_id)
    decisions = db.load_decisions(conn, session_id)
    return analyze(records, decisions, scope=scope)


def _flat_scores(conn, session_id: str, scope: str) -> list[dict[str, Any]]:
    """Per-score rows with both raw and calibrated values (for rule matching)."""
    records = db.load_scores(conn, session_id)
    analysis = analyze(records, {}, scope=scope)
    enriched: dict[int, dict[str, Any]] = {}
    for block in analysis["contestants"]:
        for item_block in block["items"]:
            for score in item_block["scores"]:
                enriched[score["id"]] = score
    return [
        {
            "id": rec["id"],
            "contestant": rec["contestant"],
            "judge": rec["judge"],
            "item": rec["item"],
            "raw": rec["score"],
            "calibrated": enriched[rec["id"]]["calibrated"],
        }
        for rec in records
    ]


def _resolve_scope(conn, session_id: str, override: str | None) -> str:
    if override is not None:
        if override not in SCOPES:
            raise HTTPException(status_code=400, detail=f"不支持的校准范围: {override}")
        return override
    stored = db.get_scope(conn, session_id)
    return stored or SCOPE_JUDGE


class ScopeBody(BaseModel):
    scope: Literal["judge", "judge_item"]


class DecisionBody(BaseModel):
    use_raw: bool
    reason: str = Field(default="", max_length=2000)


class StabilityBody(BaseModel):
    exclude_type: Literal["judge", "item"]
    exclude_name: str = Field(min_length=1, max_length=200)


class RuleBody(BaseModel):
    name: str = Field(default="", max_length=100)
    priority: int = Field(default=100, ge=0, le=1_000_000)
    judge: str | None = Field(default=None, max_length=200)
    item: str | None = Field(default=None, max_length=200)
    raw_min: float | None = None
    raw_max: float | None = None
    cal_min: float | None = None
    cal_max: float | None = None
    use_raw: bool = False
    reason_template: str = Field(default="", max_length=2000)

    @field_validator("raw_min", "raw_max", "cal_min", "cal_max")
    @classmethod
    def _finite(cls, v: float | None) -> float | None:
        if v is not None and not math.isfinite(v):
            raise ValueError("分数区间必须是有限数字")
        return v

    @field_validator("reason_template")
    @classmethod
    def _reason_clean(cls, v: str) -> str:
        return (v or "").strip()


class RulesBody(BaseModel):
    rules: list[RuleBody] = Field(min_length=1, max_length=200)

    @field_validator("rules")
    @classmethod
    def _ordered_intervals(cls, items: list[RuleBody]) -> list[RuleBody]:
        for idx, rule in enumerate(items):
            label = f"第 {idx + 1} 条规则"
            name = rule.name.strip() or label
            if rule.raw_min is not None and rule.raw_max is not None:
                if rule.raw_min > rule.raw_max:
                    raise ValueError(f"{name}的原始分区间下限不能大于上限")
            if rule.cal_min is not None and rule.cal_max is not None:
                if rule.cal_min > rule.cal_max:
                    raise ValueError(f"{name}的校准分区间下限不能大于上限")
        return items


def _rule_payload(rule: RuleBody, index: int) -> dict[str, Any]:
    payload = rule.model_dump()
    payload["name"] = payload["name"].strip() or f"规则{index + 1}"
    payload["judge"] = (payload["judge"] or "").strip() or None
    payload["item"] = (payload["item"] or "").strip() or None
    return payload


def _validate_rule_references(
    judges: set[str], items: set[str], rule_payloads: list[dict[str, Any]]
) -> None:
    for index, rule in enumerate(rule_payloads):
        label = rule["name"]
        if rule["judge"] is not None and rule["judge"] not in judges:
            raise HTTPException(
                status_code=400,
                detail=f"{label} 引用的评委不存在：{rule['judge']}（请使用当前数据中已有的评委，或留空表示不限）",
            )
        if rule["item"] is not None and rule["item"] not in items:
            raise HTTPException(
                status_code=400,
                detail=f"{label} 引用的项目不存在：{rule['item']}（请使用当前数据中已有的项目，或留空表示不限）",
            )


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/sessions/{session_id}/analysis")
def get_analysis(session_id: str, scope: str | None = None) -> dict[str, Any]:
    with db.get_conn() as conn:
        effective_scope = _resolve_scope(conn, session_id, scope)
        return _build_analysis(conn, session_id, effective_scope)


@app.post("/api/sessions/{session_id}/scope")
def update_scope(session_id: str, body: ScopeBody) -> dict[str, Any]:
    with db.get_conn() as conn:
        db.ensure_session(conn, session_id, body.scope)
        db.set_scope(conn, session_id, body.scope)
        return _build_analysis(conn, session_id, body.scope)


@app.post("/api/sessions/{session_id}/upload")
async def upload_scores(
    session_id: str,
    file: UploadFile = File(...),
    scope: str = SCOPE_JUDGE,
) -> dict[str, Any]:
    if scope not in SCOPES:
        raise HTTPException(status_code=400, detail=f"不支持的校准范围: {scope}")
    raw = await file.read()
    try:
        records = parse_scores(raw)
    except CSVParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    with db.get_conn() as conn:
        db.ensure_session(conn, session_id, scope)
        db.replace_scores(conn, session_id, records)
        return _build_analysis(conn, session_id, scope)


@app.post("/api/sessions/{session_id}/sample")
def load_sample(session_id: str, scope: str = SCOPE_JUDGE) -> dict[str, Any]:
    if scope not in SCOPES:
        raise HTTPException(status_code=400, detail=f"不支持的校准范围: {scope}")
    try:
        with open(SAMPLE_PATH, "rb") as fh:
            records = parse_scores(fh.read())
    except OSError:
        raise HTTPException(status_code=404, detail="示例数据文件不可用。")
    except CSVParseError as exc:
        raise HTTPException(status_code=500, detail=f"示例数据格式错误: {exc}")
    with db.get_conn() as conn:
        db.ensure_session(conn, session_id, scope)
        db.replace_scores(conn, session_id, records)
        return _build_analysis(conn, session_id, scope)


@app.patch("/api/sessions/{session_id}/scores/{score_id}/decision")
def patch_decision(session_id: str, score_id: int, body: DecisionBody) -> dict[str, Any]:
    with db.get_conn() as conn:
        try:
            db.upsert_decision(conn, session_id, score_id, body.use_raw, body.reason)
        except db.NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        scope = _resolve_scope(conn, session_id, None)
        return _build_analysis(conn, session_id, scope)


@app.post("/api/sessions/{session_id}/decisions/reset")
def reset_decisions(session_id: str) -> dict[str, Any]:
    with db.get_conn() as conn:
        if db.get_scope(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在。")
        removed = db.reset_decisions(conn, session_id)
        scope = _resolve_scope(conn, session_id, None)
        analysis = _build_analysis(conn, session_id, scope)
        analysis["reset_count"] = removed
        return analysis


@app.post("/api/sessions/{session_id}/stability")
def stability_check(session_id: str, body: StabilityBody) -> dict[str, Any]:
    """Temporary what-if run: exclude one judge/item and diff ranks/totals.

    Pure read-only computation — no scores, decisions or session state are
    modified. The client discards the result when returning to baseline.
    """
    with db.get_conn() as conn:
        if db.get_scope(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在，请先上传数据。")
        scope = _resolve_scope(conn, session_id, None)
        records = db.load_scores(conn, session_id)
        decisions = db.load_decisions(conn, session_id)
    if not records:
        raise HTTPException(status_code=404, detail="当前会话没有评分数据，无法进行实验。")
    try:
        return stability_experiment(
            records, decisions, scope, body.exclude_type, body.exclude_name.strip()
        )
    except LookupError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/sessions/{session_id}/rules")
def list_rules(session_id: str) -> dict[str, Any]:
    with db.get_conn() as conn:
        if db.get_scope(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在。")
        return {"rules": db.load_rules(conn, session_id)}


@app.post("/api/sessions/{session_id}/rules")
def save_rules(session_id: str, body: RulesBody) -> dict[str, Any]:
    """Persist the rule set for a session without touching any decisions."""
    payloads = [_rule_payload(r, i) for i, r in enumerate(body.rules)]
    with db.get_conn() as conn:
        scope = db.get_scope(conn, session_id)
        if scope is None:
            raise HTTPException(status_code=404, detail="会话不存在，请先上传数据。")
        records = db.load_scores(conn, session_id)
        judges = {r["judge"] for r in records}
        items = {r["item"] for r in records}
        _validate_rule_references(judges, items, payloads)
        saved = db.replace_rules(conn, session_id, payloads)
    return {"rules": saved}


@app.delete("/api/sessions/{session_id}/rules")
def clear_rules(session_id: str) -> dict[str, Any]:
    with db.get_conn() as conn:
        if db.get_scope(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在。")
        saved = db.replace_rules(conn, session_id, [])
    return {"rules": saved}


def _build_rule_preview(conn, session_id: str, payloads: list[dict[str, Any]]) -> dict[str, Any]:
    scope = _resolve_scope(conn, session_id, None)
    flat = _flat_scores(conn, session_id, scope)
    decisions = db.load_decisions(conn, session_id)
    result = rules.evaluate_rules(flat, payloads, current_decisions=decisions)
    result["scope"] = scope
    result["scope_label"] = SCOPE_LABELS[scope]
    # The raw winners map is an internal lookup — keep the payload JSON-ready.
    result.pop("winners", None)
    return result


@app.post("/api/sessions/{session_id}/rules/preview")
def preview_rules(session_id: str, body: RulesBody) -> dict[str, Any]:
    """Dry-run: hit/miss/conflict stats per rule. Never modifies the session."""
    payloads = [_rule_payload(r, i) for i, r in enumerate(body.rules)]
    with db.get_conn() as conn:
        if db.get_scope(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在，请先上传数据。")
        records = db.load_scores(conn, session_id)
        judges = {r["judge"] for r in records}
        items = {r["item"] for r in records}
        _validate_rule_references(judges, items, payloads)
        return _build_rule_preview(conn, session_id, payloads)


@app.post("/api/sessions/{session_id}/rules/apply")
def apply_rules(session_id: str, body: RulesBody) -> dict[str, Any]:
    """Persist the rule set and batch-write the resulting decisions atomically.

    One transaction: replace the session's rules, evaluate them against the
    current calibration scope, upsert one decision per matched score (with
    provenance back to the winning rule), then recompute and return rankings.
    Unmatched scores keep their existing decisions.
    """
    payloads = [_rule_payload(r, i) for i, r in enumerate(body.rules)]
    with db.get_conn() as conn:
        scope = db.get_scope(conn, session_id)
        if scope is None:
            raise HTTPException(status_code=404, detail="会话不存在，请先上传数据。")
        records = db.load_scores(conn, session_id)
        if not records:
            raise HTTPException(status_code=400, detail="当前会话没有评分数据，无法执行批量裁决。")
        judges = {r["judge"] for r in records}
        items = {r["item"] for r in records}
        _validate_rule_references(judges, items, payloads)

        flat = _flat_scores(conn, session_id, scope or SCOPE_JUDGE)
        decisions_before = db.load_decisions(conn, session_id)
        result = rules.evaluate_rules(flat, payloads, current_decisions=decisions_before)
        outcomes = rules.winning_outcomes(result)

        saved = db.replace_rules(conn, session_id, payloads)
        id_by_position = {rule["position"]: rule["id"] for rule in saved}
        provenance: dict[int, int | None] = {
            row["score_id"]: id_by_position.get(row["winner_index"])
            for row in result["scores"]
        }
        written, changed = db.bulk_upsert_decisions(conn, session_id, outcomes, provenance)
        analysis = _build_analysis(conn, session_id, scope or SCOPE_JUDGE)

    analysis["batch_result"] = {
        "rules": len(saved),
        "written": written,
        "changed": changed,
        "matched": result["summary"]["matched"],
        "unmatched": result["summary"]["unmatched"],
        "conflicts": result["summary"]["conflicts"],
    }
    return analysis


@app.delete("/api/sessions/{session_id}")
def clear_session(session_id: str) -> dict[str, Any]:
    with db.get_conn() as conn:
        existed = db.delete_session(conn, session_id)
    return {"cleared": existed}


@app.get("/api/sessions/{session_id}/export")
def export_audit(session_id: str) -> Response:
    with db.get_conn() as conn:
        if db.get_scope(conn, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在，请先上传数据。")
        scope = _resolve_scope(conn, session_id, None)
        analysis = _build_analysis(conn, session_id, scope)
    if analysis["counts"]["scores"] == 0:
        raise HTTPException(status_code=400, detail="当前会话没有可导出的数据。")
    payload = build_audit_csv(analysis)
    headers = {"Content-Disposition": 'attachment; filename="calibration_audit.csv"'}
    return Response(content=payload, media_type="text/csv; charset=utf-8", headers=headers)


# Frontend (served last so /api routes take precedence).
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
