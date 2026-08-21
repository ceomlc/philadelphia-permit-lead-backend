"""Philadelphia permit lead pipeline.

This module keeps the original script's fetch -> score -> dedup -> route ->
CSV shape while making each stage reusable by the API service.
"""

from __future__ import annotations

import csv
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import requests


API_URL = "https://phl.carto.com/api/v2/sql"
QUALIFY_THRESHOLD = 40
MAX_FETCH_LIMIT = 1000

SUBSTANTIAL_PROJECT_TYPES = {
    "New construction, addition, GFA change",
    "Addition and/or Alteration",
}
BUSINESS_OWNER_TAGS = ("LLC", "LP", "INC", "CORP")
DENIAL_STATUSES = {"REFUSED", "DENIED", "AMENDMENT DENIED", "EXPIRED DENIAL"}
DISTRESS_STATUSES = {"REVOKED", "STOP WORK", "ABANDONED"}

LEAD_FIELDS = (
    "permit",
    "opa_account_num",
    "address",
    "owner",
    "type_of_work",
    "status",
    "segment",
    "days_old",
    "score",
    "reasons",
    "contractor",
    "assigned_rep",
    "permit_count_for_property",
    "needs_manual_review",
)

PermitRow = dict[str, Any]
Lead = dict[str, Any]


def build_query(days_back: int = 1, limit: int = MAX_FETCH_LIMIT) -> str:
    """Build the confirmed Carto query used by the reference pipeline."""
    if days_back < 0:
        raise ValueError("days_back must be zero or greater")
    if not 1 <= limit <= MAX_FETCH_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_FETCH_LIMIT}")

    since = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    return f"""
    SELECT permitnumber, permittype, permitdescription, commercialorresidential,
           typeofwork, approvedscopeofwork, permitissuedate, status,
           applicanttype, contractorname, contractoraddress1,
           opa_account_num, address, zip, council_district, opa_owner,
           numberofunits, numberofstories, areaofdisturbance, denialdate
    FROM permits
    WHERE permitissuedate >= '{since}'
    ORDER BY permitissuedate DESC
    LIMIT {limit}
    """


def fetch_permits(days_back: int = 1, limit: int = MAX_FETCH_LIMIT) -> list[PermitRow]:
    """Fetch permit rows from Philadelphia's public Carto endpoint."""
    response = requests.get(
        API_URL,
        params={"q": build_query(days_back=days_back, limit=limit), "format": "json"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ValueError("Philadelphia permit API response did not contain a rows array")
    return rows


def days_since(iso_date_str: str | None, *, now: datetime | None = None) -> int | None:
    if not iso_date_str:
        return None
    issued_at = datetime.fromisoformat(iso_date_str.replace("Z", "+00:00"))
    if issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return (current - issued_at.astimezone(timezone.utc)).days


def classify_segment(row: PermitRow) -> str:
    """Apply the spec's three-way team boundary exactly."""
    if row.get("commercialorresidential") == "Commercial":
        if "new construction" in (row.get("typeofwork") or "").lower():
            return "Construction"
        return "Commercial"
    return "Residential"


def score_permit(
    row: PermitRow, *, now: datetime | None = None
) -> tuple[int, list[str], int | None]:
    """Score one permit using only the confirmed rules in BACKEND_SPEC.md."""
    score = 0
    reasons: list[str] = []
    segment = classify_segment(row)

    days_old = days_since(row.get("permitissuedate"), now=now)
    if days_old is not None and days_old <= 30:
        score += 20
        reasons.append("issued within last 30 days")

    type_of_work = row.get("typeofwork") or ""
    if type_of_work in SUBSTANTIAL_PROJECT_TYPES:
        score += 25
        reasons.append("substantial project type")
    if (row.get("numberofstories") or 0) >= 3:
        score += 10
        reasons.append("multi-story project")
    if (row.get("numberofunits") or 0) >= 2:
        score += 10
        reasons.append("multi-unit project")

    owner = (row.get("opa_owner") or "").strip().upper()
    is_business_owned = any(tag in owner for tag in BUSINESS_OWNER_TAGS)
    is_addition_or_expansion = any(
        signal in type_of_work.lower() for signal in ("addition", "expansion")
    )
    if segment == "Residential" and owner and not is_business_owned and is_addition_or_expansion:
        score += 25
        reasons.append("individually-owned residential addition/expansion")
    elif segment in {"Commercial", "Construction"} and is_business_owned:
        score += 15
        reasons.append("business/investor-owned property")

    status_raw = (row.get("status") or "").strip().upper()
    if status_raw in DENIAL_STATUSES:
        score += 15
        reasons.append(f"permit denied/refused (status: {row.get('status')})")
    elif status_raw in DISTRESS_STATUSES:
        score += 10
        reasons.append(f"possible distress signal (status: {row.get('status')})")

    if row.get("contractorname"):
        score += 5
        reasons.append("contractor contact already available")

    return score, reasons, days_old


def route_lead(row: PermitRow, segment: str, *, needs_manual_review: bool) -> str | None:
    """Team routing extension point; replace internals when agent capacity exists."""
    del row  # Kept in the signature for future territory/capacity routing.
    if needs_manual_review:
        return None
    return f"{segment} Team"


def qualify_permits(
    rows: Iterable[PermitRow], *, now: datetime | None = None
) -> list[Lead]:
    """Score, qualify, and shape raw permit rows before property deduplication."""
    qualified: list[Lead] = []
    for row in rows:
        score, reasons, days_old = score_permit(row, now=now)
        if score < QUALIFY_THRESHOLD:
            continue

        segment = classify_segment(row)
        needs_manual_review = not bool(row.get("contractorname"))
        qualified.append(
            {
                "permit": row.get("permitnumber"),
                "opa_account_num": row.get("opa_account_num"),
                "address": row.get("address"),
                "owner": row.get("opa_owner"),
                "type_of_work": row.get("typeofwork"),
                "status": row.get("status"),
                "segment": segment,
                "days_old": days_old,
                "score": score,
                "reasons": reasons,
                "contractor": row.get("contractorname"),
                "assigned_rep": route_lead(
                    row, segment, needs_manual_review=needs_manual_review
                ),
                "permit_count_for_property": 1,
                "needs_manual_review": needs_manual_review,
            }
        )
    return qualified


def deduplicate_leads(qualified: Iterable[Lead]) -> list[Lead]:
    """Keep the highest-scoring permit per property and count related permits."""
    sorted_leads = sorted(qualified, key=lambda lead: lead["score"], reverse=True)
    seen: dict[Any, Lead] = {}
    for original in sorted_leads:
        lead = {**original, "reasons": list(original["reasons"])}
        key = lead["opa_account_num"] or lead["address"]
        if key not in seen:
            seen[key] = lead
            continue

        kept = seen[key]
        kept["permit_count_for_property"] += 1
        kept["reasons"].append(
            f"also has permit {lead['permit']} ({lead['type_of_work']})"
        )

    return sorted(seen.values(), key=lambda lead: lead["score"], reverse=True)


def process_permits(rows: Iterable[PermitRow], *, now: datetime | None = None) -> list[Lead]:
    return deduplicate_leads(qualify_permits(rows, now=now))


def export_csv(leads: Iterable[Lead], output_path: str | Path = "qualified_leads.csv") -> Path:
    """Write the front-end-compatible CSV atomically."""
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    csv_rows = []
    for lead in leads:
        row = {field: lead.get(field) for field in LEAD_FIELDS}
        row["reasons"] = "; ".join(lead.get("reasons") or [])
        csv_rows.append(row)

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            newline="",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            writer = csv.DictWriter(handle, fieldnames=LEAD_FIELDS)
            writer.writeheader()
            writer.writerows(csv_rows)
        os.replace(temporary_name, destination)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return destination


def run_pipeline(
    days_back: int = 1,
    limit: int = MAX_FETCH_LIMIT,
    *,
    fetcher: Callable[[int, int], list[PermitRow]] = fetch_permits,
    csv_path: str | Path = "qualified_leads.csv",
) -> tuple[list[PermitRow], list[Lead]]:
    rows = fetcher(days_back, limit)
    leads = process_permits(rows)
    export_csv(leads, csv_path)
    return rows, leads


def main() -> None:
    days_back = 1
    rows, qualified = run_pipeline(days_back=days_back)
    print(
        f"Pulled {len(rows)} permits issued in the last {days_back} day(s), "
        f"{len(qualified)} qualified.\n"
    )
    for lead in qualified:
        destination = lead["assigned_rep"] or "Manual Review Queue"
        print(
            f"[{lead['score']}] {lead['address']} — {lead['owner']} "
            f"({lead['type_of_work']}) -> {destination}"
        )
        print(f"    reasons: {'; '.join(lead['reasons'])}")
    print("\nSaved qualified_leads.csv")


if __name__ == "__main__":
    main()
