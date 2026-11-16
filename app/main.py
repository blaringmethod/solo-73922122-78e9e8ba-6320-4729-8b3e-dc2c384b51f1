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
from .job_runner import pin_and_run
from .parsing import (
    CSVValidationError,
    MAX_REVISION_ROWS,
    MAX_SAMPLE_ROWS,
    parse_revisions,
    parse_samples,
)
from .retirement import MAX_RETIRE_JOBS, RetirementError, parse_job_ids, retire_jobs

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
                "max_retire_jobs": MAX_RETIRE_JOBS,
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
        # Pin current inputs (committed first, with a lease), then materialise
        # all results under heartbeat protection.  A concurrent upload cannot
        # change this job; a concurrent retirement skips its pinned old sets.
        job_id = pin_and_run()
    except ValueError as exc:
        return RedirectResponse("/?error=" + quote(str(exc)), status_code=303)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/retirements")
async def create_retirement(request: Request):
    form = await request.form()
    try:
        job_ids = parse_job_ids(form.getlist("job_ids"))
        with db.session_scope() as session:
            report = retire_jobs(session, job_ids)
            report_dict = report.as_dict()
    except RetirementError as exc:
        return RedirectResponse("/?error=" + quote(str(exc)), status_code=303)
    with db.session_scope() as session:
        jobs = list(
            session.scalars(select(db.FetchJob).order_by(db.FetchJob.id.desc()).limit(20))
        )
        return templates.TemplateResponse(
            request,
            "retirement.html",
            {"report": report_dict, "remaining_jobs": jobs},
        )


@app.get("/api/retirements/preview")
def retirement_preview():
    """List every job with the references retirement would see."""
    with db.session_scope() as session:
        live = db.live_lease_job_ids(session)
        jobs = session.scalars(select(db.FetchJob).order_by(db.FetchJob.id)).all()
        current = session.get(db.CurrentInput, 1)
        return {
            "max_retire_jobs": MAX_RETIRE_JOBS,
            "live_lease_job_ids": sorted(live),
            "current_sample_set_id": current.sample_set_id if current else None,
            "current_revision_set_id": current.revision_set_id if current else None,
            "jobs": [
                {
                    "id": job.id,
                    "status": job.status,
                    "sample_set_id": job.sample_set_id,
                    "revision_set_id": job.revision_set_id,
                    "lease_live": job.id in live,
                }
                for job in jobs
            ],
        }


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
            raise HTTPException(status_code=404, detail="取数单不存在（可能已被清退）")
        if not job.is_ready:
            return templates.TemplateResponse(
                request,
                "job_pending.html",
                {"job": job, "lease_ttl_seconds": db.lease_ttl_seconds()},
                status_code=409,
            )
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
            raise HTTPException(status_code=404, detail="取数单不存在（可能已被清退）")
        if not job.is_ready:
            return {
                "job_id": job_id,
                "status": job.status,
                "detail": job.status_detail,
                "items": [],
                "next_cursor": None,
                "has_more": False,
            }
        page, next_cursor, has_more = db.fetch_job_page(session, job_id, cursor, limit)
        return {
            "job_id": job_id,
            "status": job.status,
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
