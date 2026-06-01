from __future__ import annotations

from src.reconciliation.annexure_builder import build_annexure_plans


def test_dynamic_annexure_creation_for_all_labels() -> None:
    labels = ["Invoice", "CreditNote", "DebitNote", "Payment"]
    plans = build_annexure_plans(labels)
    assert len(plans) == len(labels)
    assert [p.label for p in plans] == labels


def test_empty_annexure_plan_still_exists() -> None:
    plans = build_annexure_plans(["Journal"])
    assert plans[0].sheet_name.startswith("Annex_")

