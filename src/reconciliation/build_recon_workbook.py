"""CLI entrypoint for deterministic-first reconciliation workbook generation."""

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
from src.config import AI_RECON_ARBITRATE
from src.reconciliation.ai_match_arbitration import arbitrate_unresolved_matches
from src.reconciliation.annexure_builder import build_annexure_plans
from src.reconciliation.ledger_loader import load_formalized_ledgers
from src.reconciliation.matching_engine import reconcile_rows
from src.reconciliation.recon_models import ReconRow, ValidationItem
from src.reconciliation import workbook_style as st
from src.reconciliation.reference_profile import (
    discover_reference_workbook,
    load_existing_reference_profile,
    profile_reference_workbook,
)
from src.reconciliation.reference_style import derive_theme
from src.reconciliation.review_queue_builder import build_review_queue
from src.reconciliation.summary_builder import build_summary_layout
from src.reconciliation.team_workbook_builder import build_team_workbook
from src.reconciliation.validation import build_validation_items
from src.reconciliation.workbook_writer import write_reconciliation_workbook
from src.reconciliation.working_ledger_builder import apply_match_records, build_working_rows
from src.tools.inspect_recon_workbook import inspect as inspect_recon_workbook

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
                "AI_FORMALIZATION_MODE": "repair_failed_rows",
                "AI_MAX_FAILED_ROWS_PER_REQUEST": "10",
                "AI_MAX_AI_PAGES_PER_LEDGER": "1",
                "AI_MAX_LAYOUT_SAMPLE_LINES": "12",
                "AI_FORMALIZATION_CACHE_ENABLED": "true",
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


def _copy_output(source: Path, destination: Path, warnings: list[str]) -> None:
    """Copy an output artifact without letting a locked archive copy block delivery."""
    try:
        shutil.copyfile(source, destination)
    except PermissionError:
        warning = f"Could not overwrite locked copy: {destination}"
        logger.warning(warning)
        warnings.append(warning)


def build_reconciliation_workbook(
    *,
    pair_id: str,
    refresh_formalized: bool,
    ai_repair_batches: int,
    reference_workbook: str | None,
    strict: bool,
    dry_run: bool,
    ai_reconciliation: bool = False,
    ai_recon_max_batches: int | None = None,
    strict_ai_reconciliation: bool = False,
) -> dict[str, object]:
    """Build a deterministic-first reconciliation workbook for one pair."""
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
    match_result = reconcile_rows(
        loaded.org_rows,
        loaded.party_rows,
        tolerance_policy=settings.reconciliation_tolerance_policy(),
    )
    runtime_settings = settings.model_copy(
        update={
            **(
                {
                    "ai_reconciliation_enabled": True,
                    "ai_reconciliation_mode": AI_RECON_ARBITRATE,
                }
                if ai_reconciliation
                else {}
            ),
            **(
                {"ai_recon_max_batches": max(0, ai_recon_max_batches)}
                if ai_recon_max_batches is not None
                else {}
            ),
        }
    )
    arbitration = None
    if runtime_settings.ai_reconciliation_active() and not dry_run:
        arbitration = arbitrate_unresolved_matches(
            loaded.org_rows,
            loaded.party_rows,
            match_result,
            config=runtime_settings,
            strict=strict_ai_reconciliation,
        )
        matches = arbitration.records
    else:
        matches = match_result.records

    org_working = build_working_rows(loaded.org_rows)
    party_working = build_working_rows(loaded.party_rows)
    apply_match_records(org_working, party_working, matches)
    annex_plans = build_annexure_plans(labels)
    annex_sheet_by_label = {p.label: p.sheet_name for p in annex_plans}
    summary_layout = build_summary_layout(
        labels, include_zero_labels=True, annex_sheet_by_label=annex_sheet_by_label
    )

    org_ledger_name = _detected_name(loaded.org_rows, loaded.org_sheet)
    party_ledger_name = _detected_name(loaded.party_rows, loaded.party_sheet)
    recon_period = _recon_period(loaded.org_rows, loaded.party_rows)
    review_queue_rows = build_review_queue(
        loaded.org_rows, loaded.party_rows, matches
    )

    ai_status = {
        "enabled": runtime_settings.ai_reconciliation_active(),
        "approved": (not runtime_settings.is_hosted_provider()) or runtime_settings.hosted_approved(),
        "attempted": bool(arbitration and arbitration.provider_call_count),
        "available": bool(
            runtime_settings.ai_reconciliation_active()
            and arbitration
            and (arbitration.provider_call_count or arbitration.cache_hit_count or not arbitration.packets)
        ),
        "mode": runtime_settings.ai_reconciliation_mode,
        "detail": (
            "Dry run: arbitration enabled but provider calls intentionally skipped."
            if dry_run and runtime_settings.ai_reconciliation_active()
            else "Bounded AI reconciliation arbitration completed."
            if arbitration
            else "Deterministic-only reconciliation path used."
        ),
        "provider_calls": arbitration.provider_call_count if arbitration else 0,
        "cache_hits": arbitration.cache_hit_count if arbitration else 0,
        "cache_misses": arbitration.cache_miss_count if arbitration else 0,
        "accepted_decisions": arbitration.accepted_decision_count if arbitration else 0,
        "review_required": arbitration.review_required_count if arbitration else 0,
    }
    logger.info("Reconciliation mode=%s detail=%s", ai_status["mode"], ai_status["detail"])

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
    team_submission_path = pair_output_dir / f"team_recon_submission__{pair_id}.xlsx"
    team_submission_central_dir = settings.project_root / "data/04_outputs/team_recon_submissions"
    team_submission_central_dir.mkdir(parents=True, exist_ok=True)
    team_submission_central_path = team_submission_central_dir / team_submission_path.name

    reference_path_str = str(ref_path or (ref_profile or {}).get("workbook_path", ""))

    if dry_run:
        return {
            "pair_id": pair_id,
            "formalized_path": str(formalized_path),
            "output_path": str(output_path),
            "central_output_path": str(central_output_path),
            "submission_path": str(submission_path),
            "submission_central_path": str(submission_central_path),
            "team_submission_path": str(team_submission_path),
            "team_submission_central_path": str(team_submission_central_path),
            "org_sheet": loaded.org_sheet,
            "party_sheet": loaded.party_sheet,
            "labels": labels,
            "reference_found": ref_found,
            "reference_profile_sheet_count": int((ref_profile or {}).get("sheet_count", 0)),
            "match_rows": len(matches),
            "ai_available": ai_status["available"],
            "ai_reconciliation_enabled": runtime_settings.ai_reconciliation_active(),
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
        matches=matches,
        ai_status=ai_status,
        include_master_match_table=runtime_settings.recon_include_master_match_table,
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
        matches=matches,
        formula_audit_failures=formula_fails,
        formula_audit=formula_audit,
        review_queue_count=len(review_queue_rows),
        review_queue_rows=review_queue_rows,
    )
    formula_audit = write_reconciliation_workbook(
        validation_items=validation_items, **writer_kwargs
    )
    inspection = inspect_recon_workbook(output_path)
    inspection_failures: list[str] = []
    if not inspection.get("required_sheets_present"):
        inspection_failures.append("required_sheets")
    if inspection.get("formula_audit_fail_count") != 0:
        inspection_failures.append("formula_audit")
    if inspection.get("validation_fail_count") != 0:
        inspection_failures.append("validation_report")
    if inspection.get("amount_difference_formula_missing") != 0:
        inspection_failures.append("amount_difference_formulas")
    if inspection.get("evidence_formula_violations"):
        inspection_failures.append("evidence_formulas")
    if inspection.get("strong_matches_missing_reference") != 0:
        inspection_failures.append("strong_match_references")
    if not inspection.get("review_queue_columns_ok"):
        inspection_failures.append("review_queue_columns")
    if inspection.get("review_queue_manual_fields_nonblank") != 0:
        inspection_failures.append("review_queue_manual_fields")
    validation_items.extend(
        [
            ValidationItem(
                "required_sheets_present",
                pair_id,
                "PASS" if inspection.get("required_sheets_present") else "FAIL",
                len(inspection.get("missing_required_sheets", [])),
                (
                    "All required internal workbook sheets are present."
                    if inspection.get("required_sheets_present")
                    else f"Missing: {inspection.get('missing_required_sheets', [])}"
                ),
            ),
            ValidationItem(
                "generated_workbook_inspection_status",
                pair_id,
                "PASS" if not inspection_failures else "FAIL",
                len(inspection_failures),
                (
                    "Read-only generated-workbook inspection passed."
                    if not inspection_failures
                    else f"Inspection failures: {', '.join(inspection_failures)}."
                ),
            ),
        ]
    )
    formula_audit = write_reconciliation_workbook(
        validation_items=validation_items, **writer_kwargs
    )

    copy_warnings: list[str] = []
    _copy_output(output_path, central_output_path, copy_warnings)
    # Submission copy: same polished content under the final submission name.
    _copy_output(output_path, submission_path, copy_warnings)
    _copy_output(output_path, submission_central_path, copy_warnings)
    try:
        build_team_workbook(
            internal_path=output_path,
            output_path=team_submission_path,
            pair_id=pair_id,
            raw_sheet_names=[loaded.org_sheet[:31], loaded.party_sheet[:31]],
        )
        _copy_output(team_submission_path, team_submission_central_path, copy_warnings)
    except PermissionError as exc:
        warning = str(exc)
        logger.warning(warning)
        copy_warnings.append(warning)
        fallback = team_submission_path.with_name(
            f"{team_submission_path.stem}__locked_fallback{team_submission_path.suffix}"
        )
        if fallback.exists():
            _copy_output(fallback, team_submission_central_path, copy_warnings)

    return {
        "pair_id": pair_id,
        "formalized_path": str(formalized_path),
        "output_path": str(output_path),
        "central_output_path": str(central_output_path),
        "submission_path": str(submission_path),
        "submission_central_path": str(submission_central_path),
        "team_submission_path": str(team_submission_path),
        "team_submission_central_path": str(team_submission_central_path),
        "org_sheet": loaded.org_sheet,
        "party_sheet": loaded.party_sheet,
        "org_ledger_name": org_ledger_name,
        "party_ledger_name": party_ledger_name,
        "recon_period": recon_period,
        "labels": labels,
        "reference_found": ref_found,
        "reference_profile_sheet_count": int((ref_profile or {}).get("sheet_count", 0)),
        "match_rows": len(matches),
        "strong_matches": sum(1 for match in matches if match.match_status == "matched_strong"),
        "supported_matches": sum(1 for match in matches if match.match_status == "matched_supported"),
        "ai_accepted_matches": sum(1 for match in matches if match.match_status == "matched_ai"),
        "review_matches": sum(1 for match in matches if match.review_required),
        "unmatched_org_rows": sum(1 for match in matches if match.match_status == "unmatched_org"),
        "unmatched_party_rows": sum(1 for match in matches if match.match_status == "unmatched_party"),
        "review_queue_rows": len(review_queue_rows),
        "ai_available": ai_status["available"],
        "ai_detail": ai_status["detail"],
        "ai_provider_calls": ai_status["provider_calls"],
        "ai_cache_hits": ai_status["cache_hits"],
        "ai_cache_misses": ai_status["cache_misses"],
        "formula_audit_failures": sum(1 for item in formula_audit if item.status != "PASS"),
        "validation_failures": sum(1 for v in validation_items if v.status == "FAIL"),
        "copy_warnings": copy_warnings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build deterministic-first reconciliation workbook.")
    parser.add_argument("--pair-id", required=True)
    parser.add_argument("--refresh-formalized", action="store_true")
    parser.add_argument("--ai-repair-batches", type=int, default=0)
    parser.add_argument("--reference-workbook", default=None)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--ai-reconciliation", action="store_true")
    parser.add_argument("--ai-recon-max-batches", type=int, default=None)
    parser.add_argument("--strict-ai-reconciliation", action="store_true")
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
        ai_reconciliation=args.ai_reconciliation,
        ai_recon_max_batches=args.ai_recon_max_batches,
        strict_ai_reconciliation=args.strict_ai_reconciliation,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
