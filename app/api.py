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

from app.llm import DeepSeekIntentInterpreter, DeepSeekPolicy, DeepSeekSettings
from app.observer import answer_observer_question
from app.runtime import CONSENT_NOTICE, NOTICE_VERSION, Participant
from app.service import (
    ArchivedWorldError,
    AuthorizationError,
    CapacityError,
    WorldService,
)
from world.event_store import ConcurrencyConflict, EventStoreError, WorldNotFound
from world.kernel import ProposalRejected


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = PROJECT_ROOT / "app" / "web"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "great_world.sqlite3"
DATABASE_PATH = Path(os.environ.get("GREAT_WORLD_DB", str(DEFAULT_DB_PATH)))
ACCESS_CODE = os.environ.get("GREAT_WORLD_ACCESS_CODE", "").strip() or None
if ACCESS_CODE is not None and len(ACCESS_CODE) < 12:
    raise ValueError("GREAT_WORLD_ACCESS_CODE 至少需要 12 个字符。")
OBSERVER_TOKEN = os.environ.get("GREAT_WORLD_OBSERVER_TOKEN", "").strip() or None
if OBSERVER_TOKEN is not None and len(OBSERVER_TOKEN) < 24:
    raise ValueError("GREAT_WORLD_OBSERVER_TOKEN 至少需要 24 个字符。")
DEEPSEEK_SETTINGS = DeepSeekSettings.from_env()
DEEPSEEK_POLICY = (
    DeepSeekPolicy(DEEPSEEK_SETTINGS) if DEEPSEEK_SETTINGS.enabled else None
)
DEEPSEEK_INTENT_INTERPRETER = (
    DeepSeekIntentInterpreter(DEEPSEEK_SETTINGS)
    if DEEPSEEK_SETTINGS.enabled
    else None
)

service = WorldService(
    DATABASE_PATH,
    llm_policy=DEEPSEEK_POLICY,
    llm_agent_ids=(DEEPSEEK_SETTINGS.agent_ids if DEEPSEEK_POLICY else ()),
    intent_interpreter=DEEPSEEK_INTENT_INTERPRETER,
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


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=1000)
    observed_seq: int = Field(ge=1)
    request_id: str = Field(min_length=8, max_length=128)

    @field_validator("text", "request_id")
    @classmethod
    def nonblank_turn_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("内容不能为空。")
        return value.strip()


class ForkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at_seq: int = Field(ge=1)


class ObserverQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=1000)
    world_id: str | None = Field(default=None, min_length=8, max_length=128)

    @field_validator("question")
    @classmethod
    def nonblank_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("观察问题不能为空。")
        return value.strip()


class ResetWorldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    world_id: str = Field(min_length=8, max_length=128)
    confirmation: str = Field(min_length=8, max_length=256)


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


def require_observer_token(
    x_observer_token: str | None = Header(default=None),
) -> None:
    if OBSERVER_TOKEN is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="观察台尚未配置访问令牌。",
        )
    provided = x_observer_token or ""
    if not secrets.compare_digest(provided, OBSERVER_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="观察台令牌不正确。",
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


@app.exception_handler(ArchivedWorldError)
async def archived_world_handler(_: Request, exc: ArchivedWorldError) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "detail": str(exc),
            "code": "world_archived",
            "current_world_id": exc.current_world_id,
        },
    )


@app.exception_handler(EventStoreError)
async def event_store_handler(_: Request, exc: EventStoreError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc), "code": "event_store_error"})


@app.exception_handler(ValueError)
async def value_error_handler(_: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc), "code": "invalid_request"})


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html")


@app.get("/observer", include_in_schema=False)
def observer_index() -> FileResponse:
    return FileResponse(WEB_ROOT / "observer.html")


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


@app.post("/api/worlds/{world_id}/actions", status_code=201, deprecated=True)
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


@app.post("/api/worlds/{world_id}/advance", deprecated=True)
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


@app.post("/api/worlds/{world_id}/turns", status_code=201)
def perform_turn(
    world_id: str,
    request: TurnRequest,
    participant: Participant = Depends(participant_from_token),
) -> dict[str, Any]:
    result = service.perform_turn(
        participant,
        world_id,
        request.text,
        observed_seq=request.observed_seq,
        request_id=request.request_id,
    )
    return {
        "status": "committed",
        "player_event_ids": [event.event_id for event in result.player_events],
        "response_event_ids": [event.event_id for event in result.response_events],
        "seq": result.view["world"]["seq"],
        "message": result.message,
        "feedback": list(result.feedback),
        "view": result.view,
    }


@app.get("/api/observer/worlds/current")
def current_observer_snapshot(
    _: None = Depends(require_observer_token),
) -> dict[str, Any]:
    return service.observer_snapshot()


@app.get("/api/observer/worlds/{world_id}")
def observer_snapshot(
    world_id: str,
    _: None = Depends(require_observer_token),
) -> dict[str, Any]:
    return service.observer_snapshot(world_id)


@app.post("/api/observer/query")
def observer_query(
    request: ObserverQueryRequest,
    _: None = Depends(require_observer_token),
) -> dict[str, Any]:
    snapshot = service.observer_snapshot(request.world_id)
    result = answer_observer_question(snapshot, request.question)
    return {
        **result,
        "world_id": snapshot["world"]["id"],
        "world_seq": snapshot["world"]["seq"],
    }


@app.post("/api/observer/reset", status_code=201)
def observer_reset(
    request: ResetWorldRequest,
    _: None = Depends(require_observer_token),
) -> dict[str, Any]:
    required = f"RESET {request.world_id}"
    if not secrets.compare_digest(request.confirmation.strip(), required):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"请输入 {required} 以确认整世界重置。",
        )
    result = service.reset_default_world(request.world_id)
    return {
        "status": "reset",
        "archived_world_id": result.archived_world_id,
        "world_id": result.world_id,
        "epoch_index": result.epoch_index,
        "seed_changed": result.seed_changed,
    }


@app.post("/api/worlds/{world_id}/forks", status_code=201)
def fork_world(
    world_id: str,
    request: ForkRequest,
    participant: Participant = Depends(participant_from_token),
) -> dict[str, Any]:
    child_world_id = service.fork(participant, world_id, request.at_seq)
    return {"world_id": child_world_id, "parent_world_id": world_id, "fork_seq": request.at_seq}
