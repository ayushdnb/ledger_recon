"""Validation that runs every time a formalized workbook is generated.

Produces a list of :class:`ValidationItem` rows for the ``Validation_Report``
sheet. Validation is descriptive and auditable: it never mutates rows and never
discards anything. Parse-rate checks are reported as percentages with explicit
PASS / REVIEW / FAIL / INFO status.
"""

from __future__ import annotations

import json
from collections import Counter

from src.formalization.ledger_models import (
    FormalizedLedger,
    LedgerRow,
    RecordKind,
    ValidationItem,
)

# Required source-traceability fields on every row.
_TRACE_FIELDS = ("source_file", "page_number", "source_row_number", "raw_text", "extraction_method")


def _pct(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 100.0
    return round(100.0 * numerator / denominator, 1)


def _rate_status(pct: float, *, review_below: float = 95.0, fail_below: float = 0.0) -> str:
    if pct < fail_below:
        return "FAIL"
    if pct < review_below:
        return "REVIEW"
    return "PASS"


def _has_trace(row: LedgerRow) -> bool:
    for f in _TRACE_FIELDS:
        value = getattr(row, f, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            return False
    return True


def _validate_one(ledger: FormalizedLedger) -> list[ValidationItem]:
    scope = ledger.sheet_name or ledger.ledger_role
    rows = ledger.rows
    txns = ledger.transaction_rows()
    items: list[ValidationItem] = []

    # Identity detection.
    ident = ledger.identity
    items.append(
        ValidationItem(
            check="ledger_identity_detected",
            scope=scope,
            status="PASS" if ident.confidence >= 0.7 else "REVIEW",
            count=1 if ident.detected_title else 0,
            details=(
                f"title='{ident.detected_title}' method={ident.detection_method} "
                f"confidence={ident.confidence:.2f}"
            ),
        )
    )

    # Sheet-name sanitization (original vs final).
    items.append(
        ValidationItem(
            check="sheet_name_sanitized",
            scope=scope,
            status="PASS",
            count=len(ledger.sheet_name),
            details=f"original='{ident.detected_title}' final='{ledger.sheet_name}'",
        )
    )

    # Row counts.
    items.append(
        ValidationItem("extracted_row_count", scope, "INFO", len(rows), "All emitted rows.")
    )
    items.append(
        ValidationItem("transaction_row_count", scope, "INFO", len(txns), "record_kind=Transaction.")
    )
    if ledger.page_analysis:
        avg_page_conf = round(
            sum(p.confidence_score for p in ledger.page_analysis) / len(ledger.page_analysis), 1
        )
        items.append(
            ValidationItem(
                "page_confidence",
                scope,
                _rate_status(avg_page_conf, review_below=75.0),
                int(avg_page_conf),
                f"Average page confidence across {len(ledger.page_analysis)} pages (%).",
            )
        )
        avg_layout_conf = round(
            sum(r.layout_confidence for r in rows) / max(1, len(rows)), 1
        )
        items.append(
            ValidationItem(
                "layout_confidence",
                scope,
                _rate_status(avg_layout_conf, review_below=75.0),
                int(avg_layout_conf),
                "Average row-level layout confidence (%).",
            )
        )
        col_values: list[float] = []
        for row in rows:
            if not row.column_confidence_json:
                continue
            try:
                payload = json.loads(row.column_confidence_json)
            except json.JSONDecodeError:
                continue
            col_values.extend(float(v) for v in payload.values())
        avg_col_conf = round(sum(col_values) / len(col_values), 1) if col_values else 0.0
        items.append(
            ValidationItem(
                "column_confidence",
                scope,
                _rate_status(avg_col_conf, review_below=75.0),
                int(avg_col_conf),
                "Average detected-column confidence (%).",
            )
        )

    # Special-row capture.
    for kind, check in (
        (RecordKind.OPENING_BALANCE, "opening_balance_captured"),
        (RecordKind.CLOSING_BALANCE, "closing_balance_captured"),
        (RecordKind.TOTAL, "total_rows_captured"),
    ):
        n = len(ledger.rows_of_kind(kind))
        items.append(
            ValidationItem(
                check=check,
                scope=scope,
                status="PASS" if n > 0 else "REVIEW",
                count=n,
                details="Present." if n > 0 else "Not found; verify source.",
            )
        )

    # Parse success rates over transaction rows.
    dated = sum(1 for r in txns if r.date)
    items.append(
        ValidationItem(
            "date_parse_success_rate",
            scope,
            _rate_status(_pct(dated, len(txns))),
            int(_pct(dated, len(txns))),
            f"{dated}/{len(txns)} transaction rows have a resolved date (%).",
        )
    )

    with_amount = sum(
        1 for r in txns if r.debit_source is not None or r.credit_source is not None
    )
    items.append(
        ValidationItem(
            "amount_parse_success_rate",
            scope,
            _rate_status(_pct(with_amount, len(txns))),
            int(_pct(with_amount, len(txns))),
            f"{with_amount}/{len(txns)} transaction rows have a parsed debit/credit (%).",
        )
    )

    typed = sum(1 for r in txns if r.type_label and r.type_label != RecordKind.UNKNOWN)
    items.append(
        ValidationItem(
            "type_classification_success_rate",
            scope,
            _rate_status(_pct(typed, len(txns)), review_below=90.0),
            int(_pct(typed, len(txns))),
            f"{typed}/{len(txns)} transaction rows classified to a known type (%).",
        )
    )

    # Missing references.
    missing_ref = sum(1 for r in txns if not (r.reference_no or "").strip())
    items.append(
        ValidationItem(
            "missing_references_count",
            scope,
            "REVIEW" if missing_ref > 0 else "PASS",
            missing_ref,
            "Transaction rows without any reference/voucher number.",
        )
    )

    # Duplicate suspicion: identical (date, normalized_reference, debit, credit).
    keys = Counter(
        (r.date, r.normalized_reference, r.debit_source, r.credit_source)
        for r in txns
        if r.normalized_reference
    )
    dup = sum(c - 1 for c in keys.values() if c > 1)
    items.append(
        ValidationItem(
            "duplicate_row_suspicion_count",
            scope,
            "REVIEW" if dup > 0 else "PASS",
            dup,
            "Rows sharing (date, normalized_reference, debit, credit).",
        )
    )

    # Debit/credit numeric parse failures: transaction rows with neither amount.
    dc_fail = sum(
        1 for r in txns if r.debit_source is None and r.credit_source is None
    )
    items.append(
        ValidationItem(
            "debit_credit_parse_failures",
            scope,
            "REVIEW" if dc_fail > 0 else "PASS",
            dc_fail,
            "Transaction rows with no parseable debit or credit amount.",
        )
    )

    # Review queue count.
    review_n = sum(1 for r in rows if r.review_flag)
    items.append(
        ValidationItem(
            "review_queue_count",
            scope,
            "REVIEW" if review_n > 0 else "PASS",
            review_n,
            "Rows routed to Review_Queue.",
        )
    )
    items.append(
        ValidationItem(
            "rows_requiring_review",
            scope,
            "REVIEW" if review_n > 0 else "PASS",
            review_n,
            "Rows routed for low date/amount/layout or ambiguity.",
        )
    )

    ambiguous_balance = sum(
        1
        for r in rows
        if r.record_kind == RecordKind.TRANSACTION
        and bool(r.balance_raw_source)
        and not r.balance_side_source
    )
    items.append(
        ValidationItem(
            "rows_with_ambiguous_balance_handling",
            scope,
            "REVIEW" if ambiguous_balance > 0 else "PASS",
            ambiguous_balance,
            "Rows with balance value but no explicit Dr/Cr side marker.",
        )
    )

    date_ambiguity = sum(
        1
        for r in txns
        if (r.raw_date and not r.normalized_date) or r.date_parse_confidence < 70.0
    )
    items.append(
        ValidationItem(
            "rows_with_date_ambiguity",
            scope,
            "REVIEW" if date_ambiguity > 0 else "PASS",
            date_ambiguity,
            "Rows with low-confidence or unresolved dates.",
        )
    )

    amount_ambiguity = sum(
        1 for r in txns if r.amount_parse_confidence < 70.0
    )
    items.append(
        ValidationItem(
            "rows_with_amount_ambiguity",
            scope,
            "REVIEW" if amount_ambiguity > 0 else "PASS",
            amount_ambiguity,
            "Rows with low-confidence amount parsing.",
        )
    )

    # Source traceability completeness.
    traced = sum(1 for r in rows if _has_trace(r))
    items.append(
        ValidationItem(
            "source_traceability_completeness",
            scope,
            "PASS" if traced == len(rows) else "FAIL",
            int(_pct(traced, len(rows))),
            f"{traced}/{len(rows)} rows carry full source evidence (%).",
        )
    )

    # Rows dropped (must be zero).
    dropped = sum(a.rows_dropped for a in ledger.audit)
    items.append(
        ValidationItem(
            "rows_dropped",
            scope,
            "PASS" if dropped == 0 else "FAIL",
            dropped,
            "Financial rows dropped without audit (must be 0).",
        )
    )

    return items


def build_validation_report(ledgers: list[FormalizedLedger]) -> list[ValidationItem]:
    """Build the full validation report across both ledgers."""
    items: list[ValidationItem] = []

    items.append(
        ValidationItem(
            check="source_pdfs_discovered",
            scope="workbook",
            status="PASS" if len(ledgers) >= 2 else "FAIL",
            count=len(ledgers),
            details="; ".join(f"{l.ledger_role}:{l.identity.source_file}" for l in ledgers),
        )
    )

    for ledger in ledgers:
        items.extend(_validate_one(ledger))

    if ledgers:
        all_rows = [r for l in ledgers for r in l.rows]
        ledger_conf = round(
            sum(r.confidence_score for r in all_rows) / max(1, len(all_rows)), 1
        )
        items.append(
            ValidationItem(
                check="ledger_confidence",
                scope="workbook",
                status=_rate_status(ledger_conf, review_below=80.0),
                count=int(ledger_conf),
                details="Average confidence across all formalized rows (%).",
            )
        )
        ai_usage = sum(int(l.layout_stats.get("ai_usage_count", 0)) for l in ledgers)
        ai_hit = sum(int(l.layout_stats.get("ai_cache_hit_count", 0)) for l in ledgers)
        ai_miss = sum(int(l.layout_stats.get("ai_cache_miss_count", 0)) for l in ledgers)
        ai_repair_applied = sum(
            int(l.layout_stats.get("ai_repair_applied_count", 0)) for l in ledgers
        )
        ai_repair_rejected = sum(
            int(l.layout_stats.get("ai_repair_rejected_count", 0)) for l in ledgers
        )
        items.extend(
            [
                ValidationItem(
                    "ai_usage_count",
                    "workbook",
                    "INFO",
                    ai_usage,
                    "AI layout/repair calls.",
                ),
                ValidationItem("ai_cache_hit_count", "workbook", "INFO", ai_hit, "AI cache hits."),
                ValidationItem("ai_cache_miss_count", "workbook", "INFO", ai_miss, "AI cache misses."),
                ValidationItem(
                    "ai_repair_applied_count",
                    "workbook",
                    "INFO",
                    ai_repair_applied,
                    "Rows with accepted AI date repairs.",
                ),
                ValidationItem(
                    "ai_repair_rejected_count",
                    "workbook",
                    "INFO",
                    ai_repair_rejected,
                    "Failed-row repair proposals rejected.",
                ),
            ]
        )

    items.append(
        ValidationItem(
            check="no_reconciliation_performed",
            scope="workbook",
            status="PASS",
            count=0,
            details="Formalization only: no matching, annexures, or final balances.",
        )
    )
    return items
