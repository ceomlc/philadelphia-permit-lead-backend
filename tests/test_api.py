import csv
from pathlib import Path

from fastapi.testclient import TestClient

from backend.config import Settings
from backend.main import create_app


def sample_rows():
    base = {
        "commercialorresidential": "Residential",
        "typeofwork": "Addition and/or Alteration",
        "permitissuedate": "2026-08-20T00:00:00Z",
        "status": "Issued",
        "opa_owner": "JANE DOE",
        "numberofunits": 1,
        "numberofstories": 1,
    }
    return [
        {
            **base,
            "permitnumber": "ROUTED-1",
            "contractorname": "Known Builder",
            "opa_account_num": "OPA-1",
            "address": "1 Market St",
        },
        {
            **base,
            "permitnumber": "MANUAL-1",
            "contractorname": None,
            "opa_account_num": "OPA-2",
            "address": "2 Market St",
        },
    ]


def make_client(tmp_path: Path):
    settings = Settings(
        database_path=tmp_path / "leads.sqlite3",
        csv_path=tmp_path / "qualified_leads.csv",
        admin_api_key="test-admin-key",
        scheduler_enabled=False,
        scheduler_hour_utc=10,
        scheduler_minute_utc=0,
        scheduler_days_back=1,
        scheduler_limit=1000,
    )

    def fetcher(days_back, limit):
        assert days_back == 1
        assert limit == 1000
        return sample_rows()

    return TestClient(create_app(settings, fetcher=fetcher)), settings


def test_run_persists_queues_and_exports_csv(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        run = client.post("/pipeline/run")
        assert run.status_code == 200
        assert run.json()["qualified_count"] == 2
        assert run.json()["routed_count"] == 1
        assert run.json()["manual_review_count"] == 1

        routed = client.get("/leads/routed")
        assert routed.status_code == 200
        assert [lead["permit"] for lead in routed.json()] == ["ROUTED-1"]
        assert routed.json()[0]["needs_manual_review"] is False

        default_queue = client.get("/leads")
        assert default_queue.json() == routed.json()

        forbidden = client.get("/leads/manual-review")
        assert forbidden.status_code == 403
        wrong_key = client.get(
            "/leads/manual-review", headers={"X-Admin-Key": "wrong"}
        )
        assert wrong_key.status_code == 403
        manual = client.get(
            "/leads/manual-review", headers={"X-Admin-Key": "test-admin-key"}
        )
        assert manual.status_code == 200
        assert [lead["permit"] for lead in manual.json()] == ["MANUAL-1"]
        assert manual.json()[0]["assigned_rep"] is None

        all_leads = client.get(
            "/leads?queue=all", headers={"X-Admin-Key": "test-admin-key"}
        )
        assert all_leads.status_code == 200
        assert len(all_leads.json()) == 2

    with settings.csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert {row["permit"] for row in rows} == {"ROUTED-1", "MANUAL-1"}
    assert "needs_manual_review" in rows[0]
