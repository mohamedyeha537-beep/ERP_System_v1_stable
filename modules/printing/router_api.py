"""API لوكيل الطباعة المحلي."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.deps import get_db_session
from modules.printing.service import (
    authenticate_agent,
    claim_job,
    job_to_api_dict,
    list_pending_jobs_for_agent,
    mark_job_failed,
    mark_job_printed,
)
from sqlalchemy.orm import Session

router = APIRouter(prefix="/api/print-agent", tags=["print-agent"])


def _token_from_header(
    x_agent_token: str | None = Header(default=None, alias="X-Agent-Token"),
    authorization: str | None = Header(default=None),
) -> str | None:
    if x_agent_token and x_agent_token.strip():
        return x_agent_token.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def get_print_agent(
    db: Session = Depends(get_db_session),
    token: str | None = Depends(_token_from_header),
):
    agent = authenticate_agent(db, token)
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="رمز الوكيل غير صالح.",
        )
    db.commit()
    return agent


class HeartbeatIn(BaseModel):
    hostname: str | None = None
    version: str | None = None


@router.post("/heartbeat")
def heartbeat(
    body: HeartbeatIn | None = None,
    agent=Depends(get_print_agent),
):
    return {"ok": True, "agent_id": agent.id, "name": agent.name}


@router.get("/jobs")
def list_jobs(
    db: Session = Depends(get_db_session),
    agent=Depends(get_print_agent),
    limit: int = 20,
):
    jobs = list_pending_jobs_for_agent(db, agent, limit=min(max(limit, 1), 50))
    return {"jobs": [job_to_api_dict(j, db) for j in jobs]}


@router.post("/jobs/{job_id}/claim")
def api_claim_job(
    job_id: int,
    db: Session = Depends(get_db_session),
    agent=Depends(get_print_agent),
):
    job = claim_job(db, agent, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="المهمة غير متاحة.")
    db.commit()
    return {"ok": True, "job": job_to_api_dict(job, db)}


class JobResultIn(BaseModel):
    error_message: str | None = Field(default=None, max_length=500)


@router.post("/jobs/{job_id}/printed")
def api_job_printed(
    job_id: int,
    db: Session = Depends(get_db_session),
    agent=Depends(get_print_agent),
):
    job = mark_job_printed(db, agent, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="تعذّر تأكيد الطباعة.")
    db.commit()
    return {"ok": True}


@router.post("/jobs/{job_id}/failed")
def api_job_failed(
    job_id: int,
    body: JobResultIn,
    db: Session = Depends(get_db_session),
    agent=Depends(get_print_agent),
):
    job = mark_job_failed(db, agent, job_id, body.error_message or "failed")
    if job is None:
        raise HTTPException(status_code=404, detail="تعذّر تسجيل الفشل.")
    db.commit()
    return {"ok": True}
