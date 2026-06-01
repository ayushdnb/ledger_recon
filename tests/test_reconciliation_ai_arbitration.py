from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import AI_RECON_ARBITRATE, AI_RECON_OFF, settings
from src.reconciliation.ai_match_arbitration import (
    arbitrate_unresolved_matches,
    build_candidate_packets,
    parse_ai_reconciliation_response,
    validate_ai_decision,
)
from src.reconciliation.matching_engine import reconcile_rows
from src.reconciliation.recon_models import AIMatchDecision, ReconRow
from src.reconciliation.review_queue_builder import build_review_queue
from src.reconciliation.working_ledger_builder import apply_match_records, build_working_rows


def _config(tmp_path: Path, *, enabled: bool = True, cache: bool = False, **updates):
    return settings.model_copy(
        update={
            "ai_reconciliation_enabled": enabled,
            "ai_reconciliation_mode": AI_RECON_ARBITRATE if enabled else AI_RECON_OFF,
            "ai_recon_cache_enabled": cache,
            "ai_recon_cache_dir": tmp_path / "cache",
            "ai_recon_max_candidates_per_packet": 12,
            "ai_recon_max_input_tokens_per_request": 10000,
            "ai_recon_amount_tolerance": 0.01,
            "ai_recon_date_tolerance_days": 7,
            "ai_recon_min_confidence": 0.75,
            "ai_recon_max_batches": 10,
            "ai_recon_review_on_validation_failure": True,
            "ai_model_name": "unit-test-model",
            **updates,
        }
    )


def _row(
    row_id: str,
    side: str,
    amount: float,
    *,
    nref: str = "",
    date: str = "2025-01-01",
    label: str = "Invoice",
) -> ReconRow:
    source_amount = amount if side == "org" else -amount
    return ReconRow(
        data={
            "row_id": row_id,
            "record_kind": "Transaction",
            "ledger_role": f"{side}_ledger",
            "date": date,
            "normalized_date": date,
            "raw_date": date,
            "raw_type": label,
            "type_label": label,
            "reference_no": nref,
            "normalized_reference": nref,
            "particulars": f"{label} {row_id}",
            "account": "Sales",
            "debit_source": max(source_amount, 0.0),
            "credit_source": max(-source_amount, 0.0),
            "debit_org_perspective": max(amount, 0.0),
            "credit_org_perspective": max(-amount, 0.0),
            "page_number": 1,
            "source_row_number": 1,
            "source_file": f"{side}.pdf",
        },
        sheet_name=side,
    )


def _decision(
    *,
    decision_id: str = "AI-1",
    decision_type: str = "one_to_one",
    org_ids: list[str] | None = None,
    party_ids: list[str] | None = None,
    match_type: str = "amount_date",
    confidence: float = 0.95,
    amount_delta: float = 0.0,
    date_delta: int = 1,
) -> dict:
    return {
        "decision_id": decision_id,
        "decision_type": decision_type,
        "org_row_ids": org_ids if org_ids is not None else ["o1"],
        "party_row_ids": party_ids if party_ids is not None else ["p1"],
        "match_type": match_type,
        "confidence": confidence,
        "amount_delta": amount_delta,
        "date_delta_days": date_delta,
        "reason_code": "amount_date_agree",
        "explanation": "supplied amounts and dates agree",
        "review_reason": None,
    }


def _raw(*decisions: dict) -> str:
    return json.dumps({"decisions": list(decisions)})


class _FakeClient:
    def __init__(self, *responses: str, error: Exception | None = None) -> None:
        self.responses = list(responses)
        self.error = error
        self.calls = 0

    def complete_chat(self, messages, *, request_json_object=True):
        self.calls += 1
        if self.error:
            raise self.error
        return self.responses.pop(0)


def _unresolved_pair():
    return [_row("o1", "org", 100.0)], [_row("p1", "party", 100.0, date="2025-01-02")]


def test_ai_disabled_never_calls_provider(tmp_path: Path) -> None:
    org, party = _unresolved_pair()
    deterministic = reconcile_rows(org, party)
    client = _FakeClient(_raw(_decision()))
    result = arbitrate_unresolved_matches(
        org, party, deterministic, config=_config(tmp_path, enabled=False), client=client
    )
    assert client.calls == 0
    assert result.records == deterministic.records


def test_supported_deterministic_match_never_calls_provider(tmp_path: Path) -> None:
    org = [_row("o1", "org", 100.0)]
    party = [_row("p1", "party", 100.0)]
    deterministic = reconcile_rows(org, party)
    assert [record.match_status for record in deterministic.records] == ["matched_supported"]
    client = _FakeClient(_raw(_decision()))
    result = arbitrate_unresolved_matches(
        org, party, deterministic, config=_config(tmp_path), client=client
    )
    assert client.calls == 0
    assert [record.match_status for record in result.records] == ["matched_supported"]


def test_ai_enabled_calls_only_for_unresolved_cluster(tmp_path: Path) -> None:
    org = [_row("o0", "org", 50.0, nref="EXACT"), _row("o1", "org", 100.0)]
    party = [
        _row("p0", "party", 50.0, nref="EXACT"),
        _row("p1", "party", 100.0, date="2025-01-02"),
    ]
    deterministic = reconcile_rows(org, party)
    client = _FakeClient(_raw(_decision()))
    result = arbitrate_unresolved_matches(org, party, deterministic, config=_config(tmp_path), client=client)
    assert client.calls == 1
    assert sum(record.match_status == "matched_strong" for record in result.records) == 1
    assert sum(record.match_status == "matched_ai" for record in result.records) == 1


def test_strict_json_schema_rejects_extra_invented_field() -> None:
    decision = _decision()
    decision["invented_amount"] = 999
    with pytest.raises(ValueError, match="schema"):
        parse_ai_reconciliation_response(_raw(decision))


def test_strict_json_schema_rejects_duplicate_keys() -> None:
    raw = '{"decisions":[],"decisions":[]}'
    with pytest.raises(ValueError, match="duplicate JSON key"):
        parse_ai_reconciliation_response(raw)


def test_invalid_row_id_is_rejected(tmp_path: Path) -> None:
    org, party = _unresolved_pair()
    packet = build_candidate_packets(org, party, reconcile_rows(org, party).records, config=_config(tmp_path))[0]
    decision = AIMatchDecision(**{**_decision(), "org_row_ids": ["missing"], "party_row_ids": ["p1"], "date_delta_days": 0})
    result = validate_ai_decision(
        decision, packet, {"o1": org[0]}, {"p1": party[0]}, config=_config(tmp_path)
    )
    assert result.accepted is False
    assert result.rejection_reason == "row_id_not_supplied"


def test_invented_amount_delta_is_rejected(tmp_path: Path) -> None:
    org, party = _unresolved_pair()
    packet = build_candidate_packets(org, party, reconcile_rows(org, party).records, config=_config(tmp_path))[0]
    decision = AIMatchDecision(**{**_decision(), "amount_delta": 10.0, "date_delta_days": 0})
    result = validate_ai_decision(
        decision, packet, {"o1": org[0]}, {"p1": party[0]}, config=_config(tmp_path)
    )
    assert result.rejection_reason == "invented_or_incorrect_amount_delta"


def test_duplicate_row_ownership_is_rejected(tmp_path: Path) -> None:
    org, party = _unresolved_pair()
    client = _FakeClient(_raw(_decision(decision_id="AI-1"), _decision(decision_id="AI-2")))
    result = arbitrate_unresolved_matches(
        org, party, reconcile_rows(org, party), config=_config(tmp_path), client=client
    )
    assert any(v.rejection_reason == "duplicate_row_ownership" for v in result.validation_results)
    assert any(record.review_required for record in result.records)


def test_amount_mismatch_is_rejected(tmp_path: Path) -> None:
    org = [_row("o1", "org", 100.0)]
    party = [_row("p1", "party", 90.0)]
    packet = build_candidate_packets(org, party, reconcile_rows(org, party).records, config=_config(tmp_path))[0]
    decision = AIMatchDecision(**{**_decision(), "amount_delta": 10.0, "date_delta_days": 0})
    result = validate_ai_decision(
        decision, packet, {"o1": org[0]}, {"p1": party[0]}, config=_config(tmp_path)
    )
    assert result.rejection_reason == "amount_mismatch"


def test_valid_one_to_one_ai_decision_is_accepted(tmp_path: Path) -> None:
    org, party = _unresolved_pair()
    result = arbitrate_unresolved_matches(
        org,
        party,
        reconcile_rows(org, party),
        config=_config(tmp_path),
        client=_FakeClient(_raw(_decision())),
    )
    accepted = [record for record in result.records if record.match_status == "matched_ai"]
    assert len(accepted) == 1
    assert accepted[0].decision_source == "AI"
    assert accepted[0].validation_status == "ACCEPTED"


def test_valid_one_to_many_grouped_decision_is_accepted(tmp_path: Path) -> None:
    org = [_row("o1", "org", 100.0)]
    # Multiple subset-sum solutions (25+25+50 and 50+50) -> deterministic allocation skips.
    party = [
        _row("p1", "party", 25.0),
        _row("p2", "party", 25.0),
        _row("p3", "party", 50.0),
        _row("p4", "party", 50.0),
    ]
    decision = _decision(
        decision_type="one_to_many",
        party_ids=["p1", "p2", "p3"],
        match_type="grouped_amount",
        date_delta=0,
    )
    result = arbitrate_unresolved_matches(
        org,
        party,
        reconcile_rows(org, party),
        config=_config(tmp_path),
        client=_FakeClient(_raw(decision)),
    )
    accepted = [record for record in result.records if record.match_status == "matched_ai"]
    assert len(accepted) == 1
    assert accepted[0].party_row_ids == ["p1", "p2", "p3"]
    assert accepted[0].party_amount == 100.0


def test_low_confidence_decision_goes_to_review(tmp_path: Path) -> None:
    org, party = _unresolved_pair()
    result = arbitrate_unresolved_matches(
        org,
        party,
        reconcile_rows(org, party),
        config=_config(tmp_path),
        client=_FakeClient(_raw(_decision(confidence=0.5))),
    )
    assert any(record.review_required for record in result.records)
    assert not any(record.match_status == "matched_ai" for record in result.records)


def test_provider_failure_goes_to_review_unless_strict(tmp_path: Path) -> None:
    org, party = _unresolved_pair()
    client = _FakeClient(error=RuntimeError("provider down"))
    result = arbitrate_unresolved_matches(
        org, party, reconcile_rows(org, party), config=_config(tmp_path), client=client
    )
    assert any(record.review_reason == "ai_provider_failure" for record in result.records)
    with pytest.raises(RuntimeError, match="provider down"):
        arbitrate_unresolved_matches(
            org,
            party,
            reconcile_rows(org, party),
            config=_config(tmp_path),
            client=_FakeClient(error=RuntimeError("provider down")),
            strict=True,
        )


def test_max_batches_routes_excess_packets_to_review_without_provider_call(tmp_path: Path) -> None:
    org, party = _unresolved_pair()
    client = _FakeClient(_raw(_decision()))
    result = arbitrate_unresolved_matches(
        org,
        party,
        reconcile_rows(org, party),
        config=_config(tmp_path, ai_recon_max_batches=0),
        client=client,
    )
    assert client.calls == 0
    assert any(record.review_reason == "ai_recon_max_batches_exceeded" for record in result.records)


def test_cache_prevents_repeated_provider_call(tmp_path: Path) -> None:
    org, party = _unresolved_pair()
    config = _config(tmp_path, cache=True)
    deterministic = reconcile_rows(org, party)
    first = _FakeClient(_raw(_decision()))
    second = _FakeClient(_raw(_decision()))
    arbitrate_unresolved_matches(org, party, deterministic, config=config, client=first)
    result = arbitrate_unresolved_matches(org, party, deterministic, config=config, client=second)
    assert first.calls == 1
    assert second.calls == 0
    assert result.cache_hit_count == 1


def test_valid_ai_resolution_shrinks_review_queue_and_marks_working_ledgers(tmp_path: Path) -> None:
    org, party = _unresolved_pair()
    deterministic = reconcile_rows(org, party)
    before = build_review_queue(org, party, deterministic.records)
    result = arbitrate_unresolved_matches(
        org, party, deterministic, config=_config(tmp_path), client=_FakeClient(_raw(_decision()))
    )
    after = build_review_queue(org, party, result.records)
    org_working = build_working_rows(org)
    party_working = build_working_rows(party)
    apply_match_records(org_working, party_working, result.records)
    assert len(after) < len(before)
    assert org_working[0].values["match_status"] == "matched_ai"
    assert party_working[0].values["decision_source"] == "AI"
