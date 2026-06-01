"""AI-assisted standardization runner for a ledger pair.

Reads the raw extraction workbook for a pair, asks the configured AI provider to
standardize both ledgers into a strict schema, validates the result, and writes a
standardized workbook. NO reconciliation, matching, or final-balance decision is
performed here.

Large raw contexts are split into deterministic chunks that respect
``AI_MAX_INPUT_CHARS_PER_REQUEST``. Each chunk is validated independently; only
validated chunks contribute to the final workbook.

The AI call is gated by configuration: hosted providers require
``AI_DATA_APPROVAL=hosted_approved`` and ``AI_ENABLED=true``. If the gate is not
satisfied, the run fails clearly before any network call.

Run from the project root::

    python -m src.standardization.standardize_pair_with_ai --pair-id pair_001_baby_and_mom__good_luck

Windows (no venv activation required)::

    .\\scripts\\run_ai_standardization.ps1
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.config import settings
from src.excel.workbook_io import write_workbook
from src.providers.ai_client import AIClientError, build_ai_client
from src.standardization.ledger_schema import (
    AIStandardizationResult,
    OpeningClosingBalance,
    SchemaError,
    StandardizedLedgerRow,
    parse_ai_result,
)
from src.standardization.prompt_builder import (
    ContextChunk,
    build_messages_for_chunk,
    load_raw_context,
    messages_char_count,
    plan_context_chunks,
)
from src.standardization.type_labeler import label_type, load_type_labels

logger = logging.getLogger("standardize_pair_with_ai")

ROW_COLUMNS = list(StandardizedLedgerRow.model_fields.keys())
BALANCE_COLUMNS = list(OpeningClosingBalance.model_fields.keys())
LEDGER_ROLES = ("org_ledger", "party_ledger")

CHUNK_AUDIT_COLUMNS = [
    "chunk_id",
    "source_ledger",
    "page_range",
    "input_char_count",
    "ai_status",
    "validation_status",
    "error_message",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _raw_workbook_path(pair_id: str) -> Path:
    pair_root = settings.resolved(settings.input_pair_root)
    return pair_root / pair_id / "output" / f"raw_pdf_context__{pair_id}.xlsx"


def _load_latest_reference_profile() -> dict | None:
    """Load the most recent reference profile JSON, if any exist."""
    profile_dir = settings.resolved(settings.reference_profile_output_dir)
    if not profile_dir.exists():
        return None
    candidates = sorted(profile_dir.glob("reference_workbook_profile__*.json"))
    if not candidates:
        return None
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        return json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not read reference profile: %s", latest)
        return None


def _parsable_date(value: object) -> bool:
    if value is None or str(value).strip() == "":
        return True  # null is allowed; handled by presence rules elsewhere
    try:
        parsed = pd.to_datetime(str(value), errors="coerce", dayfirst=True)
    except (ValueError, TypeError):
        return False
    return not pd.isna(parsed)


def _validate_rows(rows: list[dict]) -> dict[str, int]:
    """Augment review flags on transaction rows; return per-check failure counts."""
    counts = {
        "missing_source_page": 0,
        "missing_raw_text": 0,
        "unparseable_date": 0,
    }
    for row in rows:
        reasons: list[str] = []
        if row.get("source_page") is None:
            counts["missing_source_page"] += 1
            reasons.append("missing source_page")
        if not (row.get("raw_text") or "").strip():
            counts["missing_raw_text"] += 1
            reasons.append("empty raw_text")
        if not _parsable_date(row.get("transaction_date")):
            counts["unparseable_date"] += 1
            reasons.append("unparseable transaction_date")
        if reasons:
            row["review_required"] = True
            existing = (row.get("review_reason") or "").strip()
            combined = "; ".join(reasons)
            row["review_reason"] = f"{existing}; {combined}".strip("; ")
    return counts


def _balance_presence(balances: list[dict]) -> dict[str, dict[str, bool]]:
    """Whether each role has an opening and a closing balance."""
    presence: dict[str, dict[str, bool]] = {
        role: {"OpeningBalance": False, "ClosingBalance": False}
        for role in LEDGER_ROLES
    }
    for bal in balances:
        role = bal.get("ledger_role")
        kind = bal.get("balance_kind")
        if role in presence and kind in presence[role]:
            presence[role][kind] = True
    return presence


def _merge_chunk_results(
    pair_id: str, chunk_results: list[AIStandardizationResult]
) -> AIStandardizationResult:
    """Merge validated chunk results deterministically into one payload."""
    if not chunk_results:
        raise SchemaError("No validated chunk results to merge.")

    org_rows: list[StandardizedLedgerRow] = []
    party_rows: list[StandardizedLedgerRow] = []
    balances: list[OpeningClosingBalance] = []
    balance_keys: set[tuple] = set()
    uncertain: list[str] = []
    warnings: list[str] = []

    detected_org: str | None = None
    detected_party: str | None = None
    detected_account: str | None = None
    detected_start: str | None = None
    detected_end: str | None = None

    for result in chunk_results:
        org_rows.extend(result.org_ledger_rows)
        party_rows.extend(result.party_ledger_rows)
        for bal in result.opening_closing_balances:
            key = (
                bal.ledger_role,
                bal.balance_kind,
                bal.source_page,
                bal.source_file_name,
                bal.amount,
            )
            if key not in balance_keys:
                balance_keys.add(key)
                balances.append(bal)
        uncertain.extend(result.uncertain_fields)
        warnings.extend(result.extraction_warnings)
        if detected_org is None and result.detected_org_name:
            detected_org = result.detected_org_name
        if detected_party is None and result.detected_party_name:
            detected_party = result.detected_party_name
        if detected_account is None and result.detected_account_name:
            detected_account = result.detected_account_name
        if detected_start is None and result.detected_period_start:
            detected_start = result.detected_period_start
        if detected_end is None and result.detected_period_end:
            detected_end = result.detected_period_end

    return AIStandardizationResult(
        pair_id=pair_id,
        detected_org_name=detected_org,
        detected_party_name=detected_party,
        detected_account_name=detected_account,
        detected_period_start=detected_start,
        detected_period_end=detected_end,
        opening_closing_balances=balances,
        org_ledger_rows=org_rows,
        party_ledger_rows=party_rows,
        uncertain_fields=sorted(set(uncertain)),
        extraction_warnings=sorted(set(warnings)),
    )


def _build_type_mapping(all_rows: list[dict], type_labels: dict) -> pd.DataFrame:
    """Map each distinct raw_type to a canonical label via the local labeler."""
    seen: dict[str, dict] = {}
    for row in all_rows:
        raw = row.get("raw_type")
        key = "" if raw is None else str(raw)
        if key in seen:
            continue
        match = label_type(key)
        seen[key] = {
            "raw_type": match.raw_type,
            "ai_canonical_type": row.get("canonical_type"),
            "local_canonical_type": match.canonical_type,
            "local_match_method": match.match_method,
            "local_match_score": match.match_score,
            "matched_alias": match.matched_alias,
            "agreement": row.get("canonical_type") == match.canonical_type,
            "review_required": match.review_required,
            "review_reason": match.review_reason,
        }
    columns = [
        "raw_type",
        "ai_canonical_type",
        "local_canonical_type",
        "local_match_method",
        "local_match_score",
        "matched_alias",
        "agreement",
        "review_required",
        "review_reason",
    ]
    return pd.DataFrame(list(seen.values()), columns=columns)


def _build_review_queue(
    org_rows: list[dict], party_rows: list[dict], balances: list[dict]
) -> pd.DataFrame:
    queue = []
    for source, rows in (("org_ledger", org_rows), ("party_ledger", party_rows)):
        for row in rows:
            if row.get("review_required"):
                queue.append(
                    {
                        "item_kind": "transaction",
                        "ledger_role": row.get("ledger_role", source),
                        "source_page": row.get("source_page"),
                        "raw_type": row.get("raw_type"),
                        "canonical_type": row.get("canonical_type"),
                        "review_reason": row.get("review_reason"),
                        "raw_text": row.get("raw_text"),
                    }
                )
    for bal in balances:
        if bal.get("review_required"):
            queue.append(
                {
                    "item_kind": "balance",
                    "ledger_role": bal.get("ledger_role"),
                    "source_page": bal.get("source_page"),
                    "raw_type": bal.get("balance_kind"),
                    "canonical_type": bal.get("balance_kind"),
                    "review_reason": bal.get("review_reason"),
                    "raw_text": bal.get("source_text"),
                }
            )
    columns = [
        "item_kind",
        "ledger_role",
        "source_page",
        "raw_type",
        "canonical_type",
        "review_reason",
        "raw_text",
    ]
    return pd.DataFrame(queue, columns=columns)


def _build_validation_report(
    org_rows: list[dict],
    party_rows: list[dict],
    balances: list[dict],
    org_counts: dict[str, int],
    party_counts: dict[str, int],
) -> pd.DataFrame:
    rows = []

    def add(check: str, scope: str, status: str, details: str, count: int) -> None:
        rows.append(
            {
                "check": check,
                "scope": scope,
                "status": status,
                "details": details,
                "count": count,
            }
        )

    add(
        "required_columns_present",
        "Org_Ledger_Standardized",
        "PASS",
        "All schema columns present (written from validated schema).",
        len(ROW_COLUMNS),
    )
    add(
        "required_columns_present",
        "Party_Ledger_Standardized",
        "PASS",
        "All schema columns present (written from validated schema).",
        len(ROW_COLUMNS),
    )
    add(
        "canonical_type_approved",
        "all rows",
        "PASS",
        "canonical_type validated against approved labels by schema.",
        len(org_rows) + len(party_rows),
    )

    for scope, counts in (("org_ledger", org_counts), ("party_ledger", party_counts)):
        add(
            "source_page_present",
            scope,
            "PASS" if counts["missing_source_page"] == 0 else "REVIEW",
            "Rows missing source_page were flagged review_required.",
            counts["missing_source_page"],
        )
        add(
            "raw_text_present",
            scope,
            "PASS" if counts["missing_raw_text"] == 0 else "REVIEW",
            "Transaction rows with empty raw_text were flagged review_required.",
            counts["missing_raw_text"],
        )
        add(
            "transaction_date_parses",
            scope,
            "PASS" if counts["unparseable_date"] == 0 else "REVIEW",
            "Rows with unparseable dates were flagged review_required.",
            counts["unparseable_date"],
        )

    presence = _balance_presence(balances)
    for role in LEDGER_ROLES:
        for kind in ("OpeningBalance", "ClosingBalance"):
            present = presence[role][kind]
            add(
                f"{kind}_present",
                role,
                "PASS" if present else "REVIEW",
                "Present." if present else "Missing; should be reviewed.",
                1 if present else 0,
            )

    add(
        "no_reconciliation_performed",
        "workbook",
        "PASS",
        "This stage performs standardization only; no matching or final balance.",
        0,
    )

    return pd.DataFrame(
        rows, columns=["check", "scope", "status", "details", "count"]
    )


def _reference_summary_df(reference_profile: dict | None) -> pd.DataFrame:
    columns = [
        "sheet_name",
        "headers",
        "is_ledger_like",
        "is_summary_like",
        "non_empty_row_count",
    ]
    if not reference_profile:
        return pd.DataFrame(
            [
                {
                    "sheet_name": "(no reference profile found)",
                    "headers": "",
                    "is_ledger_like": "",
                    "is_summary_like": "",
                    "non_empty_row_count": "",
                }
            ],
            columns=columns,
        )
    rows = [
        {
            "sheet_name": s.get("sheet_name"),
            "headers": " | ".join(h for h in s.get("headers", []) if h),
            "is_ledger_like": s.get("is_ledger_like"),
            "is_summary_like": s.get("is_summary_like"),
            "non_empty_row_count": s.get("non_empty_row_count"),
        }
        for s in reference_profile.get("sheets", [])
    ]
    return pd.DataFrame(rows, columns=columns)


def _source_manifest_df(raw_workbook_path: Path) -> pd.DataFrame:
    try:
        return pd.read_excel(raw_workbook_path, sheet_name="Source_Manifest")
    except (ValueError, KeyError, OSError):
        return pd.DataFrame(
            [{"note": f"Source_Manifest not found in {raw_workbook_path.name}"}]
        )


def _build_standardized_workbook(
    output_path: Path,
    pair_id: str,
    result: AIStandardizationResult,
    raw_workbook_path: Path,
    reference_profile: dict | None,
    type_labels: dict,
    audit_log_rows: list[dict],
    chunk_audit_rows: list[dict],
) -> None:
    org_rows = [r.model_dump() for r in result.org_ledger_rows]
    party_rows = [r.model_dump() for r in result.party_ledger_rows]
    balances = [b.model_dump() for b in result.opening_closing_balances]

    org_counts = _validate_rows(org_rows)
    party_counts = _validate_rows(party_rows)

    org_df = pd.DataFrame(org_rows, columns=ROW_COLUMNS)
    party_df = pd.DataFrame(party_rows, columns=ROW_COLUMNS)
    balances_df = pd.DataFrame(balances, columns=BALANCE_COLUMNS)
    type_mapping_df = _build_type_mapping(org_rows + party_rows, type_labels)
    review_df = _build_review_queue(org_rows, party_rows, balances)
    validation_df = _build_validation_report(
        org_rows, party_rows, balances, org_counts, party_counts
    )
    audit_df = pd.DataFrame(
        audit_log_rows,
        columns=["timestamp", "stage", "detail"],
    )
    chunk_audit_df = pd.DataFrame(chunk_audit_rows, columns=CHUNK_AUDIT_COLUMNS)
    reference_df = _reference_summary_df(reference_profile)
    manifest_df = _source_manifest_df(raw_workbook_path)

    readme_text = (
        "AI-ASSISTED STANDARDIZATION ONLY.\n\n"
        "This workbook contains ledger rows standardized from the raw extraction "
        "context by an AI model, then validated against a strict schema by Python "
        "before being written here. NO reconciliation, matching, or final balance "
        "decision has been performed.\n\n"
        "Type labels (Invoice, Payment, etc.) are standardization labels only and "
        "are NOT reconciliation decisions.\n\n"
        f"pair_id: {pair_id}\n"
        f"generated (UTC): {_now_iso()}\n"
        f"detected_org_name: {result.detected_org_name}\n"
        f"detected_party_name: {result.detected_party_name}\n\n"
        "Sheets:\n"
        "  Source_Manifest                  - source files carried from raw extraction.\n"
        "  Org_Ledger_Standardized          - standardized organisation ledger rows.\n"
        "  Party_Ledger_Standardized        - standardized party ledger rows.\n"
        "  Opening_Closing_Balances         - opening/closing balances as metadata.\n"
        "  Type_Mapping                     - raw_type -> canonical label cross-check.\n"
        "  Review_Queue                     - rows/balances needing human review.\n"
        "  Validation_Report                - per-check validation results.\n"
        "  Chunk_Audit                      - per-chunk AI request audit trail.\n"
        "  AI_Audit_Log                     - run metadata (no secrets).\n"
        "  Reference_Workbook_Profile_Summary - reference structure context.\n"
    )

    data_sheets = [
        ("Source_Manifest", manifest_df),
        ("Org_Ledger_Standardized", org_df),
        ("Party_Ledger_Standardized", party_df),
        ("Opening_Closing_Balances", balances_df),
        ("Type_Mapping", type_mapping_df),
        ("Review_Queue", review_df),
        ("Validation_Report", validation_df),
        ("Chunk_Audit", chunk_audit_df),
        ("AI_Audit_Log", audit_df),
        ("Reference_Workbook_Profile_Summary", reference_df),
    ]
    write_workbook(output_path, readme_text, data_sheets)


def plan_standardization_chunks(
    pair_id: str,
    raw_workbook_path: Path | None = None,
    reference_profile: dict | None = None,
    type_labels: dict | None = None,
) -> list[ContextChunk]:
    """Plan context chunks for a pair (no AI call)."""
    raw_workbook_path = raw_workbook_path or _raw_workbook_path(pair_id)
    context = load_raw_context(raw_workbook_path)
    return plan_context_chunks(
        context=context,
        pair_id=pair_id,
        reference_profile=reference_profile,
        type_labels=type_labels or load_type_labels(),
        max_input_chars=settings.ai_max_input_chars_per_request,
    )


def run_standardization(pair_id: str, *, dry_run: bool = False) -> dict[str, Path | list]:
    """Run the full AI standardization for a pair and write the workbook(s)."""
    raw_workbook_path = _raw_workbook_path(pair_id)
    if not raw_workbook_path.exists():
        raise FileNotFoundError(
            f"Raw extraction workbook not found: {raw_workbook_path}. "
            "Run extraction first."
        )

    type_labels = load_type_labels()
    reference_profile = _load_latest_reference_profile()
    context = load_raw_context(raw_workbook_path)
    chunks = plan_context_chunks(
        context=context,
        pair_id=pair_id,
        reference_profile=reference_profile,
        type_labels=type_labels,
        max_input_chars=settings.ai_max_input_chars_per_request,
    )

    chunk_audit_rows: list[dict] = []
    for idx, chunk in enumerate(chunks, start=1):
        messages = build_messages_for_chunk(
            pair_id=pair_id,
            chunk=chunk,
            chunk_index=idx,
            chunk_total=len(chunks),
            reference_profile=reference_profile,
            type_labels=type_labels,
        )
        chunk_audit_rows.append(
            {
                "chunk_id": chunk.chunk_id,
                "source_ledger": chunk.source_ledger,
                "page_range": chunk.page_range,
                "input_char_count": messages_char_count(messages),
                "ai_status": "planned" if dry_run else "pending",
                "validation_status": "skipped" if dry_run else "pending",
                "error_message": "",
            }
        )

    audit_log_rows: list[dict] = [
        {"timestamp": _now_iso(), "stage": "start", "detail": f"pair_id={pair_id}"},
        {
            "timestamp": _now_iso(),
            "stage": "config",
            "detail": (
                f"provider={settings.ai_provider}, model={settings.ai_model_name}, "
                f"temperature={settings.ai_temperature}, "
                f"data_approval={settings.ai_data_approval}, "
                f"max_input_chars={settings.ai_max_input_chars_per_request}"
            ),
        },
        {
            "timestamp": _now_iso(),
            "stage": "chunks",
            "detail": f"chunk_count={len(chunks)}",
        },
    ]

    if dry_run:
        logger.info(
            "Dry run: planned %s chunk(s) for pair %s", len(chunks), pair_id
        )
        for row in chunk_audit_rows:
            logger.info(
                "  %s ledger=%s pages=%s chars=%s",
                row["chunk_id"],
                row["source_ledger"],
                row["page_range"],
                row["input_char_count"],
            )
        return {"chunks": chunks, "chunk_audit": chunk_audit_rows}

    client = build_ai_client(settings)
    validated_results: list[AIStandardizationResult] = []

    for idx, chunk in enumerate(chunks, start=1):
        audit_row = chunk_audit_rows[idx - 1]
        messages = build_messages_for_chunk(
            pair_id=pair_id,
            chunk=chunk,
            chunk_index=idx,
            chunk_total=len(chunks),
            reference_profile=reference_profile,
            type_labels=type_labels,
        )
        audit_log_rows.append(
            {
                "timestamp": _now_iso(),
                "stage": "prompt",
                "detail": (
                    f"{chunk.chunk_id}: system_chars="
                    f"{len(messages[0]['content'])}, "
                    f"user_chars={len(messages[1]['content'])}, "
                    f"total_chars={messages_char_count(messages)}"
                ),
            }
        )
        try:
            raw_text = client.complete_chat(messages, request_json_object=True)
            audit_row["ai_status"] = "success"
        except AIClientError as exc:
            audit_row["ai_status"] = "failed"
            audit_row["validation_status"] = "skipped"
            audit_row["error_message"] = str(exc)
            raise AIClientError(
                f"{chunk.chunk_id} AI call failed: {exc}"
            ) from exc

        audit_log_rows.append(
            {
                "timestamp": _now_iso(),
                "stage": "response",
                "detail": f"{chunk.chunk_id}: response_chars={len(raw_text)}",
            }
        )

        try:
            chunk_result = parse_ai_result(raw_text)
            audit_row["validation_status"] = "pass"
        except SchemaError as exc:
            audit_row["validation_status"] = "fail"
            audit_row["error_message"] = str(exc)
            raise SchemaError(
                f"{chunk.chunk_id} returned invalid JSON/schema: {exc}"
            ) from exc

        validated_results.append(chunk_result)
        audit_log_rows.append(
            {
                "timestamp": _now_iso(),
                "stage": "parsed",
                "detail": (
                    f"{chunk.chunk_id}: org_rows={len(chunk_result.org_ledger_rows)}, "
                    f"party_rows={len(chunk_result.party_ledger_rows)}, "
                    f"balances={len(chunk_result.opening_closing_balances)}"
                ),
            }
        )

    result = _merge_chunk_results(pair_id, validated_results)
    audit_log_rows.append(
        {
            "timestamp": _now_iso(),
            "stage": "merged",
            "detail": (
                f"org_rows={len(result.org_ledger_rows)}, "
                f"party_rows={len(result.party_ledger_rows)}, "
                f"balances={len(result.opening_closing_balances)}"
            ),
        }
    )

    pair_output_path = (
        raw_workbook_path.parent / f"standardized_ledgers__{pair_id}.xlsx"
    )
    _build_standardized_workbook(
        output_path=pair_output_path,
        pair_id=pair_id,
        result=result,
        raw_workbook_path=raw_workbook_path,
        reference_profile=reference_profile,
        type_labels=type_labels,
        audit_log_rows=audit_log_rows,
        chunk_audit_rows=chunk_audit_rows,
    )

    central_dir = settings.resolved(settings.standardized_output_dir)
    central_dir.mkdir(parents=True, exist_ok=True)
    central_output_path = central_dir / f"standardized_ledgers__{pair_id}.xlsx"
    shutil.copyfile(pair_output_path, central_output_path)

    return {"pair_output": pair_output_path, "central_output": central_output_path}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI-assisted standardization for a ledger pair (no reconciliation).",
    )
    parser.add_argument(
        "--pair-id",
        default=settings.default_pair_id,
        help="Pair id, e.g. pair_001_baby_and_mom__good_luck",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan context chunks and print sizes only; no AI call.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    logger.info("Standardizing pair: %s", args.pair_id)
    outputs = run_standardization(args.pair_id, dry_run=args.dry_run)
    if args.dry_run:
        chunks = outputs["chunks"]
        print(f"Dry run complete: {len(chunks)} chunk(s) planned for {args.pair_id}")
        for row in outputs["chunk_audit"]:
            print(
                f"  {row['chunk_id']}: ledger={row['source_ledger']} "
                f"pages={row['page_range']} chars={row['input_char_count']}"
            )
        return

    logger.info("Standardized workbook: %s", outputs["pair_output"])
    logger.info("Central copy: %s", outputs["central_output"])
    print(f"Standardized workbook: {outputs['pair_output']}")
    print(f"Central copy:          {outputs['central_output']}")


if __name__ == "__main__":
    main()
