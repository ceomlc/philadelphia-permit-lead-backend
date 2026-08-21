from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from permit_lead_pipeline import (
    Lead,
    PermitRow,
    export_csv,
    fetch_permits,
    process_permits,
)

from .repository import LeadRepository


class PipelineAlreadyRunning(RuntimeError):
    pass


@dataclass(frozen=True)
class PipelineRunResult:
    run_id: int
    fetched_count: int
    qualified_count: int
    routed_count: int
    manual_review_count: int
    csv_path: str


class PipelineRunner:
    """Orchestrates persistence and export around the reusable core pipeline.

    Future integrations belong after `process_permits`: contact enrichment can
    resolve manual-review leads before routing, a per-agent router can replace
    the team router, and a Follow Up Boss adapter can consume persisted leads.
    """

    def __init__(
        self,
        repository: LeadRepository,
        csv_path: str | Path,
        *,
        fetcher: Callable[[int, int], list[PermitRow]] = fetch_permits,
    ):
        self.repository = repository
        self.csv_path = Path(csv_path).expanduser().resolve()
        self.fetcher = fetcher
        self._run_lock = threading.Lock()

    def run(self, *, days_back: int = 1, limit: int = 1000) -> PipelineRunResult:
        if not self._run_lock.acquire(blocking=False):
            raise PipelineAlreadyRunning("a pipeline run is already in progress")

        run_id: int | None = None
        try:
            run_id = self.repository.start_run(days_back=days_back, fetch_limit=limit)
            rows = self.fetcher(days_back, limit)
            leads: list[Lead] = process_permits(rows)
            export_csv(leads, self.csv_path)
            self.repository.complete_run(run_id, fetched_count=len(rows), leads=leads)
            manual_count = sum(lead["needs_manual_review"] for lead in leads)
            return PipelineRunResult(
                run_id=run_id,
                fetched_count=len(rows),
                qualified_count=len(leads),
                routed_count=len(leads) - manual_count,
                manual_review_count=manual_count,
                csv_path=str(self.csv_path),
            )
        except Exception as exc:
            if run_id is not None:
                self.repository.fail_run(run_id, str(exc))
            raise
        finally:
            self._run_lock.release()
