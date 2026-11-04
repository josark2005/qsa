from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import (
    api_error,
    get_container,
    get_db,
    require_admin,
    verify_csrf,
)
from app.core.container import Container
from app.models.llm_memory import LLMMemory
from app.services.llm_memory import (
    delete_memory,
    get_memory,
    list_memories,
    update_memory_summary,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/memories",
    tags=["admin-memories"],
    dependencies=[Depends(require_admin)],
)

PREVIEW_CHARS = 120
MAX_SUMMARY_CHARS = 100000


class MemoryUpdateIn(BaseModel):
    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)


def _preview(summary: str) -> str:
    text = summary or ""
    if len(text) <= PREVIEW_CHARS:
        return text
    return text[: PREVIEW_CHARS - 1] + "…"


def _payload(memory: LLMMemory, *, full: bool = False) -> dict[str, Any]:
    summary = memory.summary or ""
    payload: dict[str, Any] = {
        "id": memory.id,
        "session_key": memory.session_key,
        "summary_preview": _preview(summary),
        "summary_chars": len(summary),
        "message_count": memory.message_count,
        "last_summarized_at": (
            memory.last_summarized_at.isoformat()
            if memory.last_summarized_at is not None
            else None
        ),
        "created_at": (
            memory.created_at.isoformat()
            if memory.created_at is not None
            else None
        ),
        "updated_at": (
            memory.updated_at.isoformat()
            if memory.updated_at is not None
            else None
        ),
    }
    if full:
        payload["summary"] = summary
    return payload


@router.get("")
def list_memory_items(
    q: str | None = Query(default=None, max_length=128),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    items, total = list_memories(db, q=q, page=page, page_size=page_size)
    return {
        "items": [_payload(item) for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/{memory_id}")
def get_memory_item(
    memory_id: int,
    db: Session = Depends(get_db),
) -> dict:
    memory = get_memory(db, memory_id)
    if memory is None:
        raise api_error(404, "memory_not_found", "会话记忆不存在")
    return _payload(memory, full=True)


@router.patch("/{memory_id}")
def update_memory_item(
    memory_id: int,
    payload: MemoryUpdateIn,
    _: None = Depends(verify_csrf),
    db: Session = Depends(get_db),
) -> dict:
    memory = update_memory_summary(db, memory_id, payload.summary)
    if memory is None:
        raise api_error(404, "memory_not_found", "会话记忆不存在")
    db.commit()
    db.refresh(memory)
    return _payload(memory, full=True)


@router.delete("/{memory_id}")
def delete_memory_item(
    memory_id: int,
    _: None = Depends(verify_csrf),
    container: Container = Depends(get_container),
) -> dict:
    deleted = delete_memory(
        session_factory=container.session_factory,
        redis_client=container.redis,
        memory_id=memory_id,
    )
    if not deleted:
        raise api_error(404, "memory_not_found", "会话记忆不存在")
    return {"ok": True}

