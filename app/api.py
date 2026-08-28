"""FastAPI entry point for the Great World v0."""

from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.llm import DeepSeekPolicy, DeepSeekSettings
from app.runtime import CONSENT_NOTICE, NOTICE_VERSION, Participant
from app.service import AuthorizationError, CapacityError, WorldService
from world.event_store import ConcurrencyConflict, EventStoreError, WorldNotFound
from world.kernel import ProposalRejected


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = PROJECT_ROOT / "app" / "web"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "great_world.sqlite3"
DATABASE_PATH = Path(os.environ.get("GREAT_WORLD_DB", str(DEFAULT_DB_PATH)))
ACCESS_CODE = os.environ.get("GREAT_WORLD_ACCESS_CODE", "").strip() or None
if ACCESS_CODE is not None and len(ACCESS_CODE) < 12:
    raise ValueError("GREAT_WORLD_ACCESS_CODE 至少需要 12 个字符。")
DEEPSEEK_SETTINGS = DeepSeekSettings.from_env()
DEEPSEEK_POLICY = (
    DeepSeekPolicy(DEEPSEEK_SETTINGS) if DEEPSEEK_SETTINGS.enabled else None
)

service = WorldService(
    DATABASE_PATH,
    llm_policy=DEEPSEEK_POLICY,
    llm_agent_ids=(DEEPSEEK_SETTINGS.agent_ids if DEEPSEEK_POLICY else ()),
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    service.initialize()
    yield


app = FastAPI(
    title="大世界 v0",
    version="0.1.0",
    description="A replayable, forkable world whose causal state is controlled by a kernel.",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")


@app.middleware("http")
async def limit_request_body(request: Request, call_next):
    """Keep the local prototype from accepting unbounded JSON bodies."""

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            too_large = int(content_length) > 64 * 1024
        except ValueError:
            too_large = True
        if too_large:
            return JSONResponse(
                status_code=413,
                content={"detail": "请求内容不能超过 64 KiB。", "code": "request_too_large"},
            )
    return await call_next(request)


class ConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=80)
    accepted: bool
    notice_version: str

    @field_validator("display_name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("显示名不能为空。")
        return value.strip()


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["move", "speak", "wish", "explore"]
    payload: dict[str, Any] = Field(default_factory=dict)


class ForkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at_seq: int = Field(ge=1)


def participant_from_token(
    x_consent_token: str | None = Header(default=None),
) -> Participant:
    participant = service.runtime.participant_for_token(x_consent_token)
    if participant is None or participant.notice_version != NOTICE_VERSION:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="需要有效且为当前版本的现实层知情同意。",
        )
    return participant


def require_access_code(
    x_world_access_code: str | None = Header(default=None),
) -> None:
    if ACCESS_CODE is None:
        return
    provided = x_world_access_code or ""
    if not secrets.compare_digest(provided, ACCESS_CODE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="邀请码不正确。",
        )


@app.exception_handler(ProposalRejected)
async def proposal_rejected_handler(_: Request, exc: ProposalRejected) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc), "code": "proposal_rejected"})


@app.exception_handler(ConcurrencyConflict)
async def conflict_handler(_: Request, exc: ConcurrencyConflict) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "detail": "世界已经向前发展，请刷新后重新行动。",
            "code": "world_advanced",
            "actual_seq": exc.actual_seq,
        },
    )


@app.exception_handler(WorldNotFound)
async def world_not_found_handler(_: Request, exc: WorldNotFound) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc), "code": "world_not_found"})


@app.exception_handler(AuthorizationError)
async def authorization_handler(_: Request, exc: AuthorizationError) -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": str(exc), "code": "forbidden"})


@app.exception_handler(CapacityError)
async def capacity_handler(_: Request, exc: CapacityError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc), "code": "world_full"})


@app.exception_handler(EventStoreError)
async def event_store_handler(_: Request, exc: EventStoreError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc), "code": "event_store_error"})


@app.exception_handler(ValueError)
async def value_error_handler(_: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc), "code": "invalid_request"})


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": app.version}


@app.get("/api/reality/consent-notice")
def consent_notice() -> dict[str, Any]:
    return {**CONSENT_NOTICE, "access_code_required": ACCESS_CODE is not None}


@app.post("/api/reality/consents", status_code=201)
def create_consent(
    request: ConsentRequest,
    _: None = Depends(require_access_code),
) -> dict[str, str]:
    participant, token = service.runtime.record_consent(
        request.display_name, request.accepted, request.notice_version
    )
    return {"participant_id": participant.participant_id, "consent_token": token}


@app.post("/api/worlds/default/join")
def join_default_world(
    participant: Participant = Depends(participant_from_token),
) -> dict[str, str]:
    world_id, entity_id = service.join_default_world(participant)
    return {"world_id": world_id, "entity_id": entity_id}


@app.get("/api/worlds/{world_id}/view")
def world_view(
    world_id: str,
    participant: Participant = Depends(participant_from_token),
) -> dict[str, Any]:
    return service.observer_view(participant, world_id)


@app.post("/api/worlds/{world_id}/actions", status_code=201)
def submit_action(
    world_id: str,
    request: ActionRequest,
    participant: Participant = Depends(participant_from_token),
) -> dict[str, Any]:
    events = service.submit_action(participant, world_id, request.type, request.payload)
    return {
        "accepted": True,
        "event_ids": [event.event_id for event in events],
        "seq": events[-1].seq if events else service.kernel.replay(world_id).seq,
    }


@app.post("/api/worlds/{world_id}/advance")
def advance_world(
    world_id: str,
    participant: Participant = Depends(participant_from_token),
) -> dict[str, Any]:
    result = service.advance(participant, world_id)
    return {
        "accepted": True,
        "event_ids": [event.event_id for event in result.events],
        "message": result.message,
    }


@app.post("/api/worlds/{world_id}/forks", status_code=201)
def fork_world(
    world_id: str,
    request: ForkRequest,
    participant: Participant = Depends(participant_from_token),
) -> dict[str, Any]:
    child_world_id = service.fork(participant, world_id, request.at_seq)
    return {"world_id": child_world_id, "parent_world_id": world_id, "fork_seq": request.at_seq}
