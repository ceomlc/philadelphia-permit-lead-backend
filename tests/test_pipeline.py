from datetime import datetime, timezone

from permit_lead_pipeline import classify_segment, process_permits, score_permit


NOW = datetime(2026, 8, 21, tzinfo=timezone.utc)


def permit(**overrides):
    row = {
        "permitnumber": "P-1",
        "commercialorresidential": "Residential",
        "typeofwork": "Addition and/or Alteration",
        "permitissuedate": "2026-08-20T00:00:00Z",
        "status": "Issued",
        "contractorname": "Builder LLC",
        "opa_account_num": "OPA-1",
        "address": "1 Market St",
        "opa_owner": "JANE DOE",
        "numberofunits": 1,
        "numberofstories": 1,
    }
    row.update(overrides)
    return row


def test_segment_boundaries():
    assert classify_segment(permit()) == "Residential"
    assert classify_segment(permit(commercialorresidential="Commercial")) == "Commercial"
    assert (
        classify_segment(
            permit(
                commercialorresidential="Commercial",
                typeofwork="New construction, addition, GFA change",
            )
        )
        == "Construction"
    )


def test_residential_individual_ownership_signal_is_segment_specific():
    residential_score, reasons, _ = score_permit(permit(), now=NOW)
    commercial_score, commercial_reasons, _ = score_permit(
        permit(commercialorresidential="Commercial"), now=NOW
    )

    assert residential_score == 75
    assert "individually-owned residential addition/expansion" in reasons
    assert commercial_score == 50
    assert "individually-owned residential addition/expansion" not in commercial_reasons


def test_business_owner_signal_only_applies_to_commercial_segments():
    residential_score, reasons, _ = score_permit(permit(opa_owner="DEMO LLC"), now=NOW)
    commercial_score, commercial_reasons, _ = score_permit(
        permit(opa_owner="DEMO LLC", commercialorresidential="Commercial"), now=NOW
    )

    assert residential_score == 50
    assert "business/investor-owned property" not in reasons
    assert commercial_score == 65
    assert "business/investor-owned property" in commercial_reasons


def test_all_confirmed_commercial_scoring_signals_are_additive():
    row = permit(
        commercialorresidential="Commercial",
        typeofwork="New construction, addition, GFA change",
        opa_owner="PROJECT OWNER LLC",
        numberofstories=3,
        numberofunits=2,
        status="amendment denied",
    )

    score, reasons, days_old = score_permit(row, now=NOW)

    assert classify_segment(row) == "Construction"
    assert score == 100
    assert days_old == 1
    assert "multi-story project" in reasons
    assert "multi-unit project" in reasons
    assert "business/investor-owned property" in reasons
    assert "permit denied/refused (status: amendment denied)" in reasons


def test_distress_status_is_case_insensitive_and_worth_ten_points():
    score, reasons, _ = score_permit(
        permit(
            permitissuedate="2026-06-01T00:00:00Z",
            typeofwork="Other",
            opa_owner="DEMO LLC",
            commercialorresidential="Commercial",
            contractorname=None,
            status=" Stop Work ",
        ),
        now=NOW,
    )

    assert score == 25
    assert reasons == ["business/investor-owned property", "possible distress signal (status:  Stop Work )"]


def test_dedup_keeps_highest_score_counts_permits_and_separates_manual_review():
    rows = [
        permit(permitnumber="P-low", contractorname=None, status="Issued"),
        permit(permitnumber="P-high", contractorname=None, status="Denied"),
        permit(
            permitnumber="P-routed",
            opa_account_num="OPA-2",
            address="2 Market St",
            contractorname="Known Builder",
        ),
    ]

    leads = process_permits(rows, now=NOW)

    assert len(leads) == 2
    kept = next(lead for lead in leads if lead["opa_account_num"] == "OPA-1")
    routed = next(lead for lead in leads if lead["opa_account_num"] == "OPA-2")
    assert kept["permit"] == "P-high"
    assert kept["permit_count_for_property"] == 2
    assert any("also has permit P-low" in reason for reason in kept["reasons"])
    assert kept["needs_manual_review"] is True
    assert kept["assigned_rep"] is None
    assert routed["needs_manual_review"] is False
    assert routed["assigned_rep"] == "Residential Team"


def test_dedup_falls_back_to_address_when_opa_number_is_missing():
    rows = [
        permit(permitnumber="P-1", opa_account_num=None, address="Same Address"),
        permit(
            permitnumber="P-2",
            opa_account_num=None,
            address="Same Address",
            status="Denied",
        ),
    ]

    leads = process_permits(rows, now=NOW)

    assert len(leads) == 1
    assert leads[0]["permit"] == "P-2"
    assert leads[0]["permit_count_for_property"] == 2
