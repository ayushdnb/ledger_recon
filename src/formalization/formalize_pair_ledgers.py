"""Formalize a ledger pair into one audited two-ledger workbook.

This is the deterministic, token-efficient replacement for pushing raw ledger
context into an LLM. The pipeline is:

    PDFs -> word-layout extraction -> deterministic row formalization
         -> (optional, capped) AI layout profiling -> audited workbook

No reconciliation, matching, annexure, or final-balance logic is performed.

Run from the project root::

    .\\.venv\\Scripts\\python.exe -m src.formalization.formalize_pair_ledgers \\
        --pair-id pair_001_baby_and_mom__good_luck
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
from pathlib import Path

from src.config import FORMALIZATION_AI_LAYOUT, settings
from src.formalization.ai_failed_row_repair import repair_failed_rows_batch
from src.formalization.ai_label_grouping import group_unknown_labels_batch
from src.formalization.ai_layout_profiler import profile_layout_result
from src.formalization.ledger_cleaning import clean_text
from src.formalization.ledger_models import (
    ROLE_ORG,
    ROLE_PARTY,
    FormalizedLedger,
    LedgerRow,
    RecordKind,
    make_row_id,
)
from src.formalization.pdf_ledger_extractor import (
    ExtractedRow,
    LedgerExtraction,
    discover_pair_pdfs,
    extract_ledger,
)
from src.formalization.reference_normalization import normalize_reference
from src.formalization.type_classification import classify_type
from src.formalization.validation import build_validation_report
from src.formalization.workbook_writer import (
    assign_sheet_names,
    write_formalized_workbook,
)

logger = logging.getLogger("formalize_pair_ledgers")

WORKBOOK_STEM = "formalized_ledgers__{pair_id}.xlsx"

# record kinds whose type_label is fixed by the kind itself
_FIXED_TYPE_LABELS = {
    RecordKind.OPENING_BALANCE: "OpeningBalance",
    RecordKind.CLOSING_BALANCE: "ClosingBalance",
}


def _org_perspective(
    ledger_role: str, debit_source: float | None, credit_source: float | None
) -> tuple[float | None, float | None, float | None]:
    """Map source debit/credit to organisation perspective (reversibly).

    org_ledger: direct. party_ledger: mirrored (debit<->credit) so the
    counterparty book reads from the organisation's point of view. The source
    columns are preserved separately, so this is fully reversible.
    """
    if ledger_role == ROLE_ORG:
        debit_org, credit_org = debit_source, credit_source
    else:
        debit_org, credit_org = credit_source, debit_source

    amount_org: float | None = None
    if debit_org is not None or credit_org is not None:
        amount_org = (debit_org or 0.0) - (credit_org or 0.0)
    return debit_org, credit_org, amount_org


def _assemble_row(
    er: ExtractedRow, ledger_role: str, detected_name: str, source_file: str
) -> LedgerRow:
    row_id = make_row_id(
        ledger_role, source_file, er.page_number, er.source_row_number, er.raw_text
    )

    reasons: list[str] = []
    confidence = min(100.0, er.row_confidence or 100.0)
    type_label = ""

    if er.record_kind in _FIXED_TYPE_LABELS:
        type_label = _FIXED_TYPE_LABELS[er.record_kind]
    elif er.record_kind in {RecordKind.TRANSACTION, RecordKind.UNKNOWN}:
        tc = classify_type(er.raw_type)
        type_label = tc.type_label
        confidence = min(confidence, tc.confidence)
        if tc.review_required and tc.review_reason:
            reasons.append(tc.review_reason)

    # Structural quality checks for transactions.
    if er.record_kind == RecordKind.TRANSACTION:
        if er.date_raw and not er.date_iso:
            confidence -= 20.0
            reasons.append(f"Unparseable date '{er.date_raw}'.")
        elif not er.date_iso and not er.date_carried:
            confidence -= 20.0
            reasons.append("Missing date and no prior date to carry forward.")
        if er.debit_source is None and er.credit_source is None:
            confidence -= 40.0
            reasons.append("No parseable debit or credit amount.")
        if er.date_confidence < 70.0:
            reasons.append("Low date confidence.")
        if er.amount_confidence < 70.0:
            reasons.append("Low amount confidence.")
        if er.layout_confidence < 65.0:
            reasons.append("Low layout confidence.")
        if tc.review_required and "low confidence" in (tc.review_reason or "").lower():
            reasons.append("Multiple type classifications tie or remain ambiguous.")

    if er.record_kind == RecordKind.UNKNOWN:
        reasons.append("Row could not be classified to a known record kind.")

    if er.record_kind_reason:
        reasons.append(er.record_kind_reason)

    debit_org, credit_org, amount_org = _org_perspective(
        ledger_role, er.debit_source, er.credit_source
    )

    opening_balance = None
    closing_balance = None
    balance_like = next(
        (v for v in (er.balance_source, er.debit_source, er.credit_source) if v is not None),
        None,
    )
    if er.record_kind == RecordKind.OPENING_BALANCE:
        opening_balance = balance_like
    elif er.record_kind == RecordKind.CLOSING_BALANCE:
        closing_balance = balance_like

    confidence = max(0.0, min(100.0, confidence))
    review_flag = bool(reasons)

    reference_variants = {
        (er.reference_no or "").strip(),
        (er.voucher_no or "").strip(),
    }
    reference_variants = {v for v in reference_variants if v}
    if len(reference_variants) > 1:
        review_flag = True
        reasons.append("Multiple reference interpretations exist.")

    return LedgerRow(
        row_id=row_id,
        record_kind=er.record_kind,
        ledger_role=ledger_role,
        detected_ledger_name=detected_name,
        source_file=source_file,
        page_number=er.page_number,
        source_row_number=er.source_row_number,
        date=er.date_iso,
        raw_date=er.date_raw,
        normalized_date=er.date_iso,
        date_parse_confidence=er.date_confidence,
        raw_type=er.raw_type,
        type_label=type_label,
        particulars=er.particulars,
        account=er.account,
        voucher_no=er.voucher_no,
        reference_no=er.reference_no,
        normalized_reference=normalize_reference(er.reference_no),
        debit_source=er.debit_source,
        credit_source=er.credit_source,
        balance_source=er.balance_source,
        balance_side_source=er.balance_side_source,
        debit_raw_source=er.debit_raw,
        credit_raw_source=er.credit_raw,
        balance_raw_source=er.balance_raw,
        amount_parse_confidence=er.amount_confidence,
        debit_org_perspective=debit_org,
        credit_org_perspective=credit_org,
        amount_org_perspective=amount_org,
        opening_balance=opening_balance,
        closing_balance=closing_balance,
        raw_text=er.raw_text,
        extraction_method=er.extraction_strategy_used,
        confidence_score=round(confidence, 1),
        layout_confidence=er.layout_confidence,
        extraction_strategy_used=er.extraction_strategy_used,
        column_confidence_json=json.dumps(er.column_confidence, ensure_ascii=False),
        review_flag=review_flag,
        review_reason=clean_text("; ".join(reasons)),
    )


def _build_targeted_layout_evidence(rows: list[LedgerRow], page_analysis: list[object]) -> dict | None:
    """Build minimal AI evidence from one lowest-confidence review-heavy page."""
    if not rows or not page_analysis:
        return None

    # Skip AI when deterministic extraction has no rows routed to review.
    if not any(r.review_flag for r in rows):
        return None

    target_page = min(page_analysis, key=lambda p: p.confidence_score)
    page_lines = [r.raw_text for r in rows if r.page_number == target_page.page_number and r.raw_text]
    if not page_lines:
        return None

    return {
        "ledger_role": rows[0].ledger_role,
        "page_count": 1,
        "detected_columns": list(target_page.detected_columns),
        "header_texts": list(target_page.detected_headers)[:4],
        # Hard cap is still enforced by ai_layout_profiler using settings.
        "sample_lines": page_lines[:30],
    }


def _build_ledger(
    ledger_role: str, extractions: list[LedgerExtraction]
) -> FormalizedLedger:
    identity = extractions[0].identity
    detected_name = identity.detected_title
    rows: list[LedgerRow] = []
    audit = []
    page_analysis = []
    layout_stats = {
        "ledger_role": ledger_role,
        "source_files": [e.source_file for e in extractions],
        "page_count": sum(e.page_count for e in extractions),
        "detected_columns": [],
        "header_texts": [],
        "sample_lines": [],
        "row_count": 0,
        "page_analysis": [],
        "strategy_counts": {},
        "ai_usage_count": 0,
        "ai_cache_hit_count": 0,
        "ai_cache_miss_count": 0,
        "ai_repair_applied_count": 0,
        "ai_repair_rejected_count": 0,
        "ai_label_applied_count": 0,
        "ai_label_rejected_count": 0,
    }
    for extraction in extractions:
        audit.extend(extraction.audit)
        page_analysis.extend(extraction.page_analysis)
        stats = extraction.layout_stats
        layout_stats["detected_columns"] = list(
            dict.fromkeys(layout_stats["detected_columns"] + stats.get("detected_columns", []))
        )
        layout_stats["header_texts"].extend(stats.get("header_texts", [])[:6])
        layout_stats["sample_lines"].extend(stats.get("sample_lines", [])[:30])
        layout_stats["page_analysis"].extend(stats.get("page_analysis", []))
        layout_stats["row_count"] += int(stats.get("row_count", 0))
        for k, v in stats.get("strategy_counts", {}).items():
            layout_stats["strategy_counts"][k] = layout_stats["strategy_counts"].get(k, 0) + int(v)
        layout_stats["ai_usage_count"] += int(stats.get("ai_usage_count", 0))
        layout_stats["ai_cache_hit_count"] += int(stats.get("ai_cache_hit_count", 0))
        layout_stats["ai_cache_miss_count"] += int(stats.get("ai_cache_miss_count", 0))
        for er in extraction.rows:
            rows.append(
                _assemble_row(er, ledger_role, detected_name, extraction.source_file)
            )

    # Optional, capped AI layout profiling from one targeted page only (layout_only mode).
    layout_source = "deterministic"
    if settings.ai_formalization_mode == FORMALIZATION_AI_LAYOUT:
        ai_evidence = _build_targeted_layout_evidence(rows, page_analysis)
        if ai_evidence is not None:
            ai_result = profile_layout_result(ai_evidence)
            if ai_result.used_ai:
                layout_stats["ai_usage_count"] += 1
            if ai_result.cache_hit:
                layout_stats["ai_cache_hit_count"] += 1
            if ai_result.cache_miss:
                layout_stats["ai_cache_miss_count"] += 1
            if ai_result.profile is not None:
                layout_source = ai_result.profile.source

    # Optional, capped AI unknown-label grouping (group_unknown_labels mode).
    # Runs for both ledgers since either side may carry unclassified types.
    grouping_result = group_unknown_labels_batch(rows)
    if grouping_result.used_ai:
        layout_stats["ai_usage_count"] += 1
    if grouping_result.cache_hit:
        layout_stats["ai_cache_hit_count"] += 1
    if grouping_result.cache_miss:
        layout_stats["ai_cache_miss_count"] += 1
    layout_stats["ai_label_applied_count"] = int(
        layout_stats.get("ai_label_applied_count", 0)
    ) + grouping_result.applied_count
    layout_stats["ai_label_rejected_count"] = int(
        layout_stats.get("ai_label_rejected_count", 0)
    ) + grouping_result.rejected_count

    # Optional, capped AI failed-row repair (repair_failed_rows mode, party ledger only).
    if ledger_role == ROLE_PARTY:
        repair_result = repair_failed_rows_batch(
            rows,
            ledger_role=ledger_role,
            header_texts=layout_stats.get("header_texts", []),
        )
        if repair_result.used_ai:
            layout_stats["ai_usage_count"] += 1
        if repair_result.cache_hit:
            layout_stats["ai_cache_hit_count"] += 1
        if repair_result.cache_miss:
            layout_stats["ai_cache_miss_count"] += 1
        layout_stats["ai_repair_applied_count"] = int(
            layout_stats.get("ai_repair_applied_count", 0)
        ) + repair_result.applied_count
        layout_stats["ai_repair_rejected_count"] = int(
            layout_stats.get("ai_repair_rejected_count", 0)
        ) + repair_result.rejected_count

    return FormalizedLedger(
        ledger_role=ledger_role,
        identity=identity,
        sheet_name="",
        rows=rows,
        audit=audit,
        page_analysis=page_analysis,
        layout_stats=layout_stats,
        layout_source=layout_source,
    )


def run_formalization(pair_id: str, pair_root: Path) -> dict[str, object]:
    """Formalize the pair and write the workbook(s). Returns output paths."""
    pair_dir = pair_root / pair_id
    discovered = discover_pair_pdfs(pair_dir)

    ledgers: list[FormalizedLedger] = []
    for role in (ROLE_ORG, ROLE_PARTY):
        extractions = [extract_ledger(pdf, role) for pdf in discovered[role]]
        for pdf, extraction in zip(discovered[role], extractions):
            logger.info(
                "Extracted %s: %s -> %s rows over %s page(s)",
                role,
                pdf.name,
                len(extraction.rows),
                extraction.page_count,
            )
        ledgers.append(_build_ledger(role, extractions))

    assign_sheet_names(ledgers)
    for ledger in ledgers:
        logger.info(
            "%s identity '%s' -> sheet '%s' (layout=%s)",
            ledger.ledger_role,
            ledger.identity.detected_title,
            ledger.sheet_name,
            ledger.layout_source,
        )

    validation_items = build_validation_report(ledgers)

    workbook_name = WORKBOOK_STEM.format(pair_id=pair_id)
    pair_output_path = pair_dir / "output" / workbook_name
    sheet_map = write_formalized_workbook(
        pair_output_path, pair_id, ledgers, validation_items
    )

    central_dir = settings.resolved(settings.formalized_output_dir)
    central_dir.mkdir(parents=True, exist_ok=True)
    central_output_path = central_dir / workbook_name
    shutil.copyfile(pair_output_path, central_output_path)

    return {
        "pair_output": pair_output_path,
        "central_output": central_output_path,
        "sheet_map": sheet_map,
        "ledgers": ledgers,
        "validation": validation_items,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic formalization of a ledger pair into one audited "
            "two-ledger workbook (no reconciliation)."
        ),
    )
    parser.add_argument(
        "--pair-id",
        default=settings.default_pair_id,
        help="Pair id, e.g. pair_001_baby_and_mom__good_luck",
    )
    parser.add_argument(
        "--pair-root",
        default=None,
        help="Override the work-pairs root (default from settings.input_pair_root).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.pair_root is not None:
        pair_root = Path(args.pair_root)
        if not pair_root.is_absolute():
            pair_root = settings.project_root / pair_root
    else:
        pair_root = settings.resolved(settings.input_pair_root)

    logger.info("Formalizing pair: %s", args.pair_id)
    logger.info("Formalization AI mode: %s", settings.ai_formalization_mode)

    outputs = run_formalization(args.pair_id, pair_root)

    logger.info("Workbook written: %s", outputs["pair_output"])
    logger.info("Central copy written: %s", outputs["central_output"])
    print(f"Formalized workbook: {outputs['pair_output']}")
    print(f"Central copy:        {outputs['central_output']}")
    print(f"Sheet names:         {outputs['sheet_map']}")


if __name__ == "__main__":
    main()
