from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from permit_lead_pipeline import Lead


SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed')),
    days_back INTEGER NOT NULL,
    fetch_limit INTEGER NOT NULL,
    fetched_count INTEGER,
    qualified_count INTEGER,
    error TEXT
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
    permit TEXT,
    opa_account_num TEXT,
    address TEXT,
    owner TEXT,
    type_of_work TEXT,
    status TEXT,
    segment TEXT NOT NULL,
    days_old INTEGER,
    score INTEGER NOT NULL,
    reasons_json TEXT NOT NULL,
    contractor TEXT,
    assigned_rep TEXT,
    permit_count_for_property INTEGER NOT NULL,
    needs_manual_review INTEGER NOT NULL CHECK(needs_manual_review IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status_id
ON pipeline_runs(status, id DESC);

CREATE INDEX IF NOT EXISTS idx_leads_run_queue_score
ON leads(run_id, needs_manual_review, score DESC);
"""


class LeadRepository:
    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path).expanduser().resolve()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def start_run(self, *, days_back: int, fetch_limit: int) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO pipeline_runs(started_at, status, days_back, fetch_limit)
                VALUES (?, 'running', ?, ?)
                """,
                (now, days_back, fetch_limit),
            )
            return int(cursor.lastrowid)

    def complete_run(
        self, run_id: int, *, fetched_count: int, leads: Iterable[Lead]
    ) -> None:
        lead_list = list(leads)
        now = datetime.now(timezone.utc).isoformat()
        values = [
            (
                run_id,
                lead.get("permit"),
                lead.get("opa_account_num"),
                lead.get("address"),
                lead.get("owner"),
                lead.get("type_of_work"),
                lead.get("status"),
                lead["segment"],
                lead.get("days_old"),
                lead["score"],
                json.dumps(lead.get("reasons") or []),
                lead.get("contractor"),
                lead.get("assigned_rep"),
                lead["permit_count_for_property"],
                int(lead["needs_manual_review"]),
            )
            for lead in lead_list
        ]
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO leads(
                    run_id, permit, opa_account_num, address, owner, type_of_work,
                    status, segment, days_old, score, reasons_json, contractor,
                    assigned_rep, permit_count_for_property, needs_manual_review
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            connection.execute(
                """
                UPDATE pipeline_runs
                SET completed_at = ?, status = 'completed', fetched_count = ?,
                    qualified_count = ?
                WHERE id = ?
                """,
                (now, fetched_count, len(lead_list), run_id),
            )

    def fail_run(self, run_id: int, error: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE pipeline_runs
                SET completed_at = ?, status = 'failed', error = ?
                WHERE id = ?
                """,
                (now, error[:2000], run_id),
            )

    def latest_leads(self, queue: str = "routed") -> list[dict[str, Any]]:
        filters = {
            "routed": "AND needs_manual_review = 0",
            "manual_review": "AND needs_manual_review = 1",
            "all": "",
        }
        if queue not in filters:
            raise ValueError(f"unknown queue: {queue}")

        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT permit, opa_account_num, address, owner, type_of_work,
                       status, segment, days_old, score, reasons_json, contractor,
                       assigned_rep, permit_count_for_property, needs_manual_review
                FROM leads
                WHERE run_id = (
                    SELECT id FROM pipeline_runs
                    WHERE status = 'completed'
                    ORDER BY id DESC LIMIT 1
                )
                {filters[queue]}
                ORDER BY score DESC, id ASC
                """
            ).fetchall()
        return [self._deserialize_lead(row) for row in rows]

    def latest_summary(self) -> dict[str, Any]:
        """Return presentation-safe counts for the latest completed run."""
        with self._connect() as connection:
            latest_run = connection.execute(
                """
                SELECT id FROM pipeline_runs
                WHERE status = 'completed'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            if latest_run is None:
                return {
                    "run_id": None,
                    "leads_today": 0,
                    "routed_count": 0,
                    "manual_review_count": 0,
                    "avg_score": None,
                    "by_team": {
                        "Construction": 0,
                        "Commercial": 0,
                        "Residential": 0,
                    },
                }

            run_id = int(latest_run["id"])
            totals = connection.execute(
                """
                SELECT COUNT(*) AS leads_today,
                       SUM(CASE WHEN needs_manual_review = 0 THEN 1 ELSE 0 END)
                           AS routed_count,
                       SUM(CASE WHEN needs_manual_review = 1 THEN 1 ELSE 0 END)
                           AS manual_review_count,
                       AVG(score) AS avg_score
                FROM leads WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            team_rows = connection.execute(
                """
                SELECT segment, COUNT(*) AS lead_count
                FROM leads
                WHERE run_id = ? AND needs_manual_review = 0
                GROUP BY segment
                """,
                (run_id,),
            ).fetchall()

        by_team = {"Construction": 0, "Commercial": 0, "Residential": 0}
        for row in team_rows:
            if row["segment"] in by_team:
                by_team[row["segment"]] = int(row["lead_count"])
        return {
            "run_id": run_id,
            "leads_today": int(totals["leads_today"] or 0),
            "routed_count": int(totals["routed_count"] or 0),
            "manual_review_count": int(totals["manual_review_count"] or 0),
            "avg_score": round(float(totals["avg_score"]))
            if totals["avg_score"] is not None
            else None,
            "by_team": by_team,
        }

    @staticmethod
    def _deserialize_lead(row: sqlite3.Row) -> dict[str, Any]:
        lead = dict(row)
        lead["reasons"] = json.loads(lead.pop("reasons_json"))
        lead["needs_manual_review"] = bool(lead["needs_manual_review"])
        return lead
