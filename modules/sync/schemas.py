from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SyncEventIn(BaseModel):
    event_id: str
    site_id: str
    action: str
    table_name: str
    record_id: str
    payload_json: str
    source_updated_at: datetime


class SyncPushIn(BaseModel):
    events: list[SyncEventIn] = Field(..., min_length=1)


class SyncPushResult(BaseModel):
    event_id: str
    applied: bool = True
    message: str = ""


class SyncPushOut(BaseModel):
    results: list[SyncPushResult]


class SyncPullIn(BaseModel):
    last_event_id: str | None = None
    limit: int = Field(default=100, ge=1, le=500)
    site_id: str | None = None


class SyncEventOut(BaseModel):
    event_id: str
    site_id: str
    action: str
    table_name: str
    record_id: str
    payload_json: str
    source_updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SyncPullOut(BaseModel):
    events: list[SyncEventOut]
    has_more: bool = False


class SyncStatus(BaseModel):
    enabled: bool
    site_id: str | None
    online: bool
    pending_count: int
    last_push_at: datetime | None
    last_pull_at: datetime | None
    last_error: str | None
