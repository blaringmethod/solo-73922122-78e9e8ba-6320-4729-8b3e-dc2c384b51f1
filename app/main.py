"""FastAPI application: browser UI plus JSON pagination API."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from . import db
from .demo_loader import maybe_load_demo
from .parsing import (
    CSVValidationError,
    MAX_REVISION_ROWS,
    MAX_SAMPLE_ROWS,
    parse_revisions,
    parse_samples,
)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    maybe_load_demo()
    yield


app = FastAPI(title="故障预测温度取数平台", version="1.0.0", lifespan=lifespan)
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, msg: str | None = None, error: str | None = None):
    with db.session_scope() as session:
        overview = db.get_current_overview(session)
        jobs = list(
            session.scalars(select(db.FetchJob).order_by(db.FetchJob.id.desc()).limit(20))
        )
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "overview": overview,
                "jobs": jobs,
                "msg": msg,
                "error": error,
                "max_sample_rows": MAX_SAMPLE_ROWS,
                "max_revision_rows": MAX_REVISION_ROWS,
            },
        )


def _read_upload(upload: UploadFile) -> bytes:
    data = upload.file.read()
    if not data:
        raise CSVValidationError("上传内容为空，请选择有效的 CSV 文件")
    return data


@app.post("/uploads/samples")
def upload_samples(file: UploadFile | None = File(default=None)):
    if file is None:
        return RedirectResponse("/?error=" + quote("未选择文件"), status_code=303)
    try:
        rows = parse_samples(_read_upload(file))
        # Parsing fully succeeded: only now do we touch stored input.
        with db.session_scope() as session:
            db.replace_sample_set(session, rows, file.filename or "samples.csv")
    except CSVValidationError as exc:
        return RedirectResponse("/?error=" + quote(str(exc)), status_code=303)
    return RedirectResponse(
        f"/?msg={quote('样本 CSV 已整份替换，共 ' + str(len(rows)) + ' 行')}",
        status_code=303,
    )


@app.post("/uploads/revisions")
def upload_revisions(file: UploadFile | None = File(default=None)):
    if file is None:
        return RedirectResponse("/?error=" + quote("未选择文件"), status_code=303)
    try:
        rows = parse_revisions(_read_upload(file))
        with db.session_scope() as session:
            db.replace_revision_set(session, rows, file.filename or "revisions.csv")
    except CSVValidationError as exc:
        return RedirectResponse("/?error=" + quote(str(exc)), status_code=303)
    return RedirectResponse(
        f"/?msg={quote('读数修订 CSV 已整份替换，共 ' + str(len(rows)) + ' 行')}",
        status_code=303,
    )


@app.post("/jobs")
def create_job():
    try:
        with db.session_scope() as session:
            job = db.create_fetch_job(session)
            job_id = job.id
    except ValueError as exc:
        return RedirectResponse("/?error=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(
    request: Request,
    job_id: int,
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
):
    limit = _clamp_limit(limit)
    with db.session_scope() as session:
        job = session.get(db.FetchJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="取数单不存在")
        page, next_cursor, has_more = db.fetch_job_page(session, job_id, cursor, limit)
        return templates.TemplateResponse(
            request,
            "job.html",
            {
                "job": job,
                "rows": page,
                "cursor": cursor,
                "next_cursor": next_cursor,
                "has_more": has_more,
                "limit": limit,
                "quote": quote,
            },
        )


@app.get("/api/jobs/{job_id}/results")
def job_results_api(
    job_id: int,
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
):
    limit = _clamp_limit(limit)
    with db.session_scope() as session:
        job = session.get(db.FetchJob, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="取数单不存在")
        page, next_cursor, has_more = db.fetch_job_page(session, job_id, cursor, limit)
        return {
            "job_id": job_id,
            "sample_set_id": job.sample_set_id,
            "revision_set_id": job.revision_set_id,
            "total_samples": job.total_samples,
            "limit": limit,
            "cursor": cursor,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "items": [
                {
                    "sample_id": row.sample_id,
                    "device_id": row.device_id,
                    "prediction_time": row.prediction_time.isoformat(),
                    "temperature": row.temperature,
                    "source_revision_id": row.source_revision_id,
                }
                for row in page
            ],
        }


def _clamp_limit(limit: int) -> int:
    if limit < 1:
        raise HTTPException(status_code=422, detail="limit 必须为正整数")
    return min(limit, MAX_PAGE_SIZE)
