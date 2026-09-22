"""FastAPI application: multi-judge score calibration."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import database as db
from .csv_io import CSVParseError, build_audit_csv, parse_scores
from .scoring import SCOPES, SCOPE_JUDGE, analyze, stability_experiment

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
