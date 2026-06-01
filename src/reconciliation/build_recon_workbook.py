"""CLI entrypoint for deterministic reconciliation workbook generation."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from datetime import datetime

from src.config import settings
from src.reconciliation.ai_availability import check_ai_availability
from src.reconciliation.annexure_builder import build_annexure_plans
from src.reconciliation.ledger_loader import load_formalized_ledgers
from src.reconciliation.matching_engine import reconcile_rows
from src.reconciliation.recon_models import ReconRow
from src.reconciliation import workbook_style as st
from src.reconciliation.reference_profile import (
    discover_reference_workbook,
    load_existing_reference_profile,
    profile_reference_workbook,
)
from src.reconciliation.reference_style import derive_theme
from src.reconciliation.review_queue_builder import build_review_queue
from src.reconciliation.summary_builder import build_summary_layout
from src.reconciliation.validation import build_validation_items
from src.reconciliation.workbook_writer import write_reconciliation_workbook
from src.reconciliation.working_ledger_builder import build_working_rows

logger = logging.getLogger("reconciliation.build_recon_workbook")


def _load_labels(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.keys())


def _detected_name(rows: list[ReconRow], fallback: str) -> str:
    for row in rows[:30]:
        name = str(row.data.get("detected_ledger_name", "")).strip()
        if name:
            return name
    return fallback


def _recon_period(org_rows: list[ReconRow], party_rows: list[ReconRow]) -> str:
    dates: list[datetime] = []
    for row in org_rows + party_rows:
        text = (row.date or "")[:10]
        try:
            dates.append(datetime.strptime(text, "%Y-%m-%d"))
        except ValueError:
            continue
    if not dates:
        return "Not specified"
    lo, hi = min(dates), max(dates)
    return f"{lo.strftime('%b' + chr(39) + '%y')} to {hi.strftime('%b' + chr(39) + '%y')}"


def _run_formalization(pair_id: str, *, repair_mode: bool = False) -> None:
    env = dict(os.environ)
    if repair_mode:
        env.update(
            {
                "AI_ENABLED": "true",
                "AI_FORMALIZATION_MODE": "repair_failed_rows",
                "AI_MAX_FAILED_ROWS_PER_REQUEST": "10",
                "AI_MAX_AI_PAGES_PER_LEDGER": "1",
                "AI_MAX_LAYOUT_SAMPLE_LINES": "12",
                "AI_FORMALIZATION_CACHE_ENABLED": "true",
                "AI_DATA_APPROVAL": "hosted_approved",
            }
        )
    # Non-repair passes inherit AI_ENABLED / AI_FORMALIZATION_MODE from the
    # parent process and .env (do not force "off" here).
    cmd = [
        sys.executable,
        "-m",
        "src.formalization.formalize_pair_ledgers",
        "--pair-id",
        pair_id,
    ]
    subprocess.run(cmd, check=True, env=env, cwd=settings.project_root)


def build_reconciliation_workbook(
    *,
    pair_id: str,
    refresh_formalized: bool,
    ai_repair_batches: int,
    reference_workbook: str | None,
    strict: bool,
    dry_run: bool,
) -> dict[str, object]:
    """Build deterministic reconciliation workbook for one pair."""
    if refresh_formalized:
        logger.info("Refreshing formalized workbook first.")
        _run_formalization(pair_id, repair_mode=False)

    if ai_repair_batches > 0:
        logger.info("Running %s bounded formalization repair batch(es).", ai_repair_batches)
        for _ in range(ai_repair_batches):
            _run_formalization(pair_id, repair_mode=True)

    pair_output_dir = settings.project_root / "data/02_work_pairs" / pair_id / "output"
    formalized_path = pair_output_dir / f"formalized_ledgers__{pair_id}.xlsx"
    if not formalized_path.exists():
        raise FileNotFoundError(
            f"Required formalized workbook missing: {formalized_path}. "
            "Run formalization or pass --refresh-formalized."
        )

    labels = _load_labels(settings.project_root / "config/type_labels.json")
    loaded = load_formalized_ledgers(formalized_path)
    org_working = build_working_rows(loaded.org_rows)
    party_working = build_working_rows(loaded.party_rows)
    match_result = reconcile_rows(loaded.org_rows, loaded.party_rows)
    annex_plans = build_annexure_plans(labels)
    annex_sheet_by_label = {p.label: p.sheet_name for p in annex_plans}
    summary_layout = build_summary_layout(
        labels, include_zero_labels=True, annex_sheet_by_label=annex_sheet_by_label
    )

    org_ledger_name = _detected_name(loaded.org_rows, loaded.org_sheet)
    party_ledger_name = _detected_name(loaded.party_rows, loaded.party_sheet)
    recon_period = _recon_period(loaded.org_rows, loaded.party_rows)
    review_queue_rows = build_review_queue(
        loaded.org_rows, loaded.party_rows, match_result.records
    )

    # Bounded, content-free AI availability probe. Never sends financial data and
    # never blocks the deterministic pipeline.
    ai_status = check_ai_availability().as_dict()
    logger.info("AI availability: available=%s detail=%s", ai_status["available"], ai_status["detail"])

    ref_path = discover_reference_workbook(reference_workbook)
    ref_found = ref_path is not None
    ref_profile = None
    if ref_path is not None:
        ref_profile = profile_reference_workbook(ref_path)
    else:
        ref_profile = load_existing_reference_profile()
        if strict:
            raise FileNotFoundError(
                "Reference workbook not found under data/03_reference_workbooks/old_recon_workbooks."
            )

    output_path = pair_output_dir / f"recon_workbook__{pair_id}.xlsx"
    central_dir = settings.project_root / "data/04_outputs/reconciliation_workbooks"
    central_dir.mkdir(parents=True, exist_ok=True)
    central_output_path = central_dir / output_path.name

    submission_path = pair_output_dir / f"final_recon_submission__{pair_id}.xlsx"
    submission_central_dir = settings.project_root / "data/04_outputs/final_recon_submissions"
    submission_central_dir.mkdir(parents=True, exist_ok=True)
    submission_central_path = submission_central_dir / submission_path.name

    reference_path_str = str(ref_path or (ref_profile or {}).get("workbook_path", ""))

    if dry_run:
        return {
            "pair_id": pair_id,
            "formalized_path": str(formalized_path),
            "output_path": str(output_path),
            "central_output_path": str(central_output_path),
            "submission_path": str(submission_path),
            "submission_central_path": str(submission_central_path),
            "org_sheet": loaded.org_sheet,
            "party_sheet": loaded.party_sheet,
            "labels": labels,
            "reference_found": ref_found,
            "reference_profile_sheet_count": int((ref_profile or {}).get("sheet_count", 0)),
            "match_rows": len(match_result.records),
            "ai_available": ai_status["available"],
        }

    # Derive and apply a curated visual theme cloned from the reference workbook
    # (STARTLING ACHIEVEMENT) on top of the clean default palette. Falls back to
    # the curated default when no enriched reference profile is available.
    theme = derive_theme()
    st.apply_theme(theme)
    logger.info("Applied workbook theme (source=%s).", theme.source)

    writer_kwargs = dict(
        output_path=output_path,
        pair_id=pair_id,
        formalized_path=formalized_path,
        reference_path=reference_path_str,
        labels=labels,
        org_sheet_name=loaded.org_sheet,
        party_sheet_name=loaded.party_sheet,
        org_rows=loaded.org_rows,
        party_rows=loaded.party_rows,
        org_working=org_working,
        party_working=party_working,
        summary_layout=summary_layout,
        annex_plans=annex_plans,
        matches=match_result.records,
        ai_status=ai_status,
        recon_period=recon_period,
        org_ledger_name=org_ledger_name,
        party_ledger_name=party_ledger_name,
    )

    # Write once with empty validation placeholder, then validate against the
    # formulas actually produced, then rewrite with the finalized validation.
    formula_audit = write_reconciliation_workbook(validation_items=[], **writer_kwargs)
    formula_fails = sum(1 for item in formula_audit if item.status != "PASS")
    validation_items = build_validation_items(
        pair_id=pair_id,
        formalized_path=formalized_path,
        reference_workbook_found=ref_found,
        labels=labels,
        org_rows=loaded.org_rows,
        party_rows=loaded.party_rows,
        matches=match_result.records,
        formula_audit_failures=formula_fails,
        formula_audit=formula_audit,
        review_queue_count=len(review_queue_rows),
    )
    formula_audit = write_reconciliation_workbook(
        validation_items=validation_items, **writer_kwargs
    )

    shutil.copyfile(output_path, central_output_path)
    # Submission copy: same polished content under the final submission name.
    shutil.copyfile(output_path, submission_path)
    shutil.copyfile(output_path, submission_central_path)

    return {
        "pair_id": pair_id,
        "formalized_path": str(formalized_path),
        "output_path": str(output_path),
        "central_output_path": str(central_output_path),
        "submission_path": str(submission_path),
        "submission_central_path": str(submission_central_path),
        "org_sheet": loaded.org_sheet,
        "party_sheet": loaded.party_sheet,
        "org_ledger_name": org_ledger_name,
        "party_ledger_name": party_ledger_name,
        "recon_period": recon_period,
        "labels": labels,
        "reference_found": ref_found,
        "reference_profile_sheet_count": int((ref_profile or {}).get("sheet_count", 0)),
        "match_rows": len(match_result.records),
        "strong_matches": match_result.strong_match_count,
        "review_matches": match_result.review_match_count,
        "unmatched_org_rows": match_result.unmatched_org_count,
        "unmatched_party_rows": match_result.unmatched_party_count,
        "review_queue_rows": len(review_queue_rows),
        "ai_available": ai_status["available"],
        "ai_detail": ai_status["detail"],
        "formula_audit_failures": sum(1 for item in formula_audit if item.status != "PASS"),
        "validation_failures": sum(1 for v in validation_items if v.status == "FAIL"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build deterministic reconciliation workbook.")
    parser.add_argument("--pair-id", required=True)
    parser.add_argument("--refresh-formalized", action="store_true")
    parser.add_argument("--ai-repair-batches", type=int, default=0)
    parser.add_argument("--reference-workbook", default=None)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    result = build_reconciliation_workbook(
        pair_id=args.pair_id,
        refresh_formalized=args.refresh_formalized,
        ai_repair_batches=max(0, args.ai_repair_batches),
        reference_workbook=args.reference_workbook,
        strict=args.strict,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

