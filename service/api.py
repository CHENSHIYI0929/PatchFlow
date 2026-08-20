from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from service.database import RunStore
from service.queue import JobQueue, RedisJobQueue
from service.settings import ServiceSettings


TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "timed_out"}


class RunCreate(BaseModel):
    repo_path: str
    task: str = Field(min_length=1)
    model: str | None = None
    provider: str | None = None
    max_steps: int | None = Field(default=None, ge=1, le=200)
    run_mode: str = Field(default="auto", pattern="^(safe|review|auto|benchmark)$")
    test_cmd: str | None = None
    sandbox: bool = False
    mechanisms: dict[str, bool] = Field(default_factory=dict)


def create_app(
    settings: ServiceSettings | None = None,
    *,
    store: RunStore | None = None,
    job_queue: JobQueue | None = None,
) -> FastAPI:
    cfg = settings or ServiceSettings.from_env()
    run_store = store or RunStore(cfg.database_url)
    queue = job_queue or RedisJobQueue(cfg.redis_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        run_store.init_schema()
        yield

    app = FastAPI(title="PatchFlow API", version="2.0.0", lifespan=lifespan)
    app.state.settings = cfg
    app.state.store = run_store
    app.state.job_queue = queue

    def authenticate(authorization: str | None = Header(default=None)) -> None:
        if not cfg.api_token:
            return
        if authorization != f"Bearer {cfg.api_token}":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API token")

    def resolve_repo(raw_path: str) -> Path:
        candidate = Path(raw_path)
        resolved = (cfg.repo_root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        try:
            resolved.relative_to(cfg.repo_root.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Repository must be inside PATCHFLOW_REPO_ROOT") from exc
        if not resolved.is_dir():
            raise HTTPException(status_code=400, detail="Repository directory does not exist")
        return resolved

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready() -> dict[str, str]:
        try:
            with run_store.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            queue.ping()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Dependency unavailable: {exc}") from exc
        return {"status": "ready"}

    @app.post("/v1/runs", status_code=202, dependencies=[Depends(authenticate)])
    def create_run(body: RunCreate) -> dict[str, Any]:
        repo = resolve_repo(body.repo_path)
        run_id = str(uuid.uuid4())
        request_payload = body.model_dump()
        request_payload["repo_path"] = str(repo)
        record = run_store.create_run(run_id, body.task, str(repo), request_payload)
        try:
            queue.enqueue(run_id)
        except Exception as exc:
            run_store.update_run(run_id, status="failed", failure_stage="queue", failure_message=str(exc))
            raise HTTPException(status_code=503, detail="Unable to enqueue run") from exc
        return record

    @app.get("/v1/runs/{run_id}", dependencies=[Depends(authenticate)])
    def get_run(run_id: str) -> dict[str, Any]:
        record = run_store.get_run(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return record

    @app.post("/v1/runs/{run_id}/cancel", dependencies=[Depends(authenticate)])
    def cancel_run(run_id: str) -> dict[str, Any]:
        record = run_store.get_run(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Run not found")
        if record["status"] in TERMINAL_STATUSES:
            return record
        queue.cancel(run_id)
        next_status = "cancelled" if record["status"] == "queued" else "cancellation_requested"
        run_store.append_event(run_id, "cancellation_requested", {})
        return run_store.update_run(run_id, status=next_status)

    @app.get("/v1/runs/{run_id}/events", dependencies=[Depends(authenticate)])
    async def stream_events(run_id: str, request: Request, after: int = 0) -> StreamingResponse:
        if run_store.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="Run not found")

        async def generate() -> AsyncIterator[str]:
            cursor = after
            while not await request.is_disconnected():
                events = run_store.list_events(run_id, cursor)
                for event in events:
                    cursor = event["sequence"]
                    yield f"id: {cursor}\nevent: {event['event_type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                record = run_store.get_run(run_id)
                if record and record["status"] in TERMINAL_STATUSES and not events:
                    break
                await asyncio.sleep(0.5)

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.get("/v1/runs/{run_id}/artifacts", dependencies=[Depends(authenticate)])
    def list_artifacts(run_id: str) -> list[dict[str, Any]]:
        if run_store.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return [
            {"name": item["name"], "size_bytes": item["size_bytes"]}
            for item in run_store.list_artifacts(run_id)
        ]

    @app.get("/v1/runs/{run_id}/artifacts/{name}", dependencies=[Depends(authenticate)])
    def download_artifact(run_id: str, name: str) -> FileResponse:
        artifact = next((item for item in run_store.list_artifacts(run_id) if item["name"] == name), None)
        if artifact is None:
            raise HTTPException(status_code=404, detail="Artifact not found")
        return FileResponse(artifact["path"], filename=name)

    return app


app = create_app()
