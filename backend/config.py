from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_path: Path
    csv_path: Path
    admin_api_key: str | None
    scheduler_enabled: bool
    scheduler_hour_utc: int
    scheduler_minute_utc: int
    scheduler_days_back: int
    scheduler_limit: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_path=Path(os.getenv("DATABASE_PATH", "data/permit_leads.sqlite3")),
            csv_path=Path(os.getenv("CSV_PATH", "qualified_leads.csv")),
            admin_api_key=os.getenv("ADMIN_API_KEY"),
            scheduler_enabled=_as_bool(os.getenv("SCHEDULER_ENABLED")),
            scheduler_hour_utc=int(os.getenv("SCHEDULER_HOUR_UTC", "10")),
            scheduler_minute_utc=int(os.getenv("SCHEDULER_MINUTE_UTC", "0")),
            scheduler_days_back=int(os.getenv("SCHEDULER_DAYS_BACK", "1")),
            scheduler_limit=int(os.getenv("SCHEDULER_LIMIT", "1000")),
        )
