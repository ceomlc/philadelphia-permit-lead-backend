from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Callable

from fastapi import FastAPI, Header, HTTPException, Query, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from permit_lead_pipeline import MAX_FETCH_LIMIT, PermitRow, fetch_permits

from .config import Settings
from .repository import LeadRepository
from .service import PipelineAlreadyRunning, PipelineRunResult, PipelineRunner


class QueueName(str, Enum):
    routed = "routed"
    manual_review = "manual_review"
    all = "all"


class LeadResponse(BaseModel):
    permit: str | None
    opa_account_num: str | None
    address: str | None
    owner: str | None
    type_of_work: str | None
    status: str | None
    segment: str
    days_old: int | None
    score: int
    reasons: list[str]
    contractor: str | None
    assigned_rep: str | None
    permit_count_for_property: int
    needs_manual_review: bool


class PipelineRunResponse(BaseModel):
    run_id: int
    fetched_count: int
    qualified_count: int
    routed_count: int
    manual_review_count: int
    csv_path: str


class TeamCountsResponse(BaseModel):
    Construction: int
    Commercial: int
    Residential: int


class LeadSummaryResponse(BaseModel):
    run_id: int | None
    leads_today: int
    routed_count: int
    manual_review_count: int
    avg_score: int | None
    by_team: TeamCountsResponse


def _seconds_until(hour: int, minute: int) -> float:
    now = datetime.now(timezone.utc)
    next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    return (next_run - now).total_seconds()


async def _scheduler_loop(runner: PipelineRunner, settings: Settings) -> None:
    while True:
        await asyncio.sleep(
            _seconds_until(settings.scheduler_hour_utc, settings.scheduler_minute_utc)
        )
        try:
            await asyncio.to_thread(
                runner.run,
                days_back=settings.scheduler_days_back,
                limit=settings.scheduler_limit,
            )
        except Exception:
            # The failed run is recorded in SQLite. Keep the daily scheduler alive.
            continue


def create_app(
    settings: Settings | None = None,
    *,
    fetcher: Callable[[int, int], list[PermitRow]] = fetch_permits,
) -> FastAPI:
    active_settings = settings or Settings.from_env()
    repository = LeadRepository(active_settings.database_path)
    runner = PipelineRunner(repository, active_settings.csv_path, fetcher=fetcher)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        repository.initialize()
        app.state.repository = repository
        app.state.runner = runner
        scheduler_task = None
        if active_settings.scheduler_enabled:
            scheduler_task = asyncio.create_task(_scheduler_loop(runner, active_settings))
        try:
            yield
        finally:
            if scheduler_task:
                scheduler_task.cancel()
                try:
                    await scheduler_task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(
        title="Philadelphia Permit Lead API",
        version="1.0.0",
        lifespan=lifespan,
    )
    frontend_directory = Path(__file__).resolve().parent.parent / "frontend"

    def require_admin(x_admin_key: Annotated[str | None, Header()] = None) -> None:
        expected = active_settings.admin_api_key
        if not expected:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="ADMIN_API_KEY is not configured",
            )
        if not x_admin_key or not secrets.compare_digest(x_admin_key, expected):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="administrator access required",
            )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/leads/summary", response_model=LeadSummaryResponse)
    def get_lead_summary() -> dict:
        return repository.latest_summary()

    @app.post("/pipeline/run", response_model=PipelineRunResponse)
    async def run_pipeline(
        days_back: Annotated[int, Query(ge=0)] = 1,
        limit: Annotated[int, Query(ge=1, le=MAX_FETCH_LIMIT)] = MAX_FETCH_LIMIT,
    ) -> PipelineRunResult:
        try:
            return await asyncio.to_thread(runner.run, days_back=days_back, limit=limit)
        except PipelineAlreadyRunning as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"pipeline run failed: {exc}",
            ) from exc

    @app.get("/leads", response_model=list[LeadResponse])
    def get_leads(
        queue: QueueName = QueueName.routed,
        x_admin_key: Annotated[str | None, Header()] = None,
    ) -> list[dict]:
        if queue in {QueueName.manual_review, QueueName.all}:
            require_admin(x_admin_key)
        return repository.latest_leads(queue.value)

    @app.get("/leads/routed", response_model=list[LeadResponse])
    def get_routed_leads() -> list[dict]:
        return repository.latest_leads("routed")

    @app.get("/leads/manual-review", response_model=list[LeadResponse])
    def get_manual_review_leads(
        x_admin_key: Annotated[str | None, Header()] = None,
    ) -> list[dict]:
        require_admin(x_admin_key)
        return repository.latest_leads("manual_review")

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(frontend_directory / "index.html")

    @app.get("/admin", include_in_schema=False)
    def admin_dashboard() -> FileResponse:
        return FileResponse(frontend_directory / "admin.html")

    app.mount(
        "/frontend",
        StaticFiles(directory=frontend_directory),
        name="frontend",
    )

    return app


app = create_app()
