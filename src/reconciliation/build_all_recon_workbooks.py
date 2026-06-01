"""Build deterministic reconciliation workbooks for every discovered pair.

Discovers each ledger-pair workspace under ``data/02_work_pairs/`` (any directory
that contains an ``input/`` folder), builds the reconciliation workbook for each,
prints the exact final output paths, and writes a run manifest JSON so a run can
be traced and audited later.

No pair id is hard-coded. The same bounded flags as the single-pair builder are
supported and forwarded to every pair.

Run from the project root (PowerShell, no venv activation needed)::

    .\\.venv\\Scripts\\python.exe -m src.reconciliation.build_all_recon_workbooks --refresh-formalized
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from src.config import settings
from src.reconciliation.build_recon_workbook import build_reconciliation_workbook

logger = logging.getLogger("reconciliation.build_all_recon_workbooks")


def discover_pair_ids(work_pairs_root: Path) -> list[str]:
    """Return sorted pair ids: subdirectories that contain an ``input`` folder."""
    if not work_pairs_root.exists():
        return []
    pairs = [
        p.name
        for p in sorted(work_pairs_root.iterdir())
        if p.is_dir() and (p / "input").is_dir()
    ]
    return pairs


def build_all(
    *,
    refresh_formalized: bool,
    ai_repair_batches: int,
    reference_workbook: str | None,
    strict: bool,
    dry_run: bool,
) -> dict[str, object]:
    """Build reconciliation workbooks for all discovered pairs; return a manifest."""
    work_pairs_root = settings.project_root / "data/02_work_pairs"
    pair_ids = discover_pair_ids(work_pairs_root)
    if not pair_ids:
        raise FileNotFoundError(
            f"No ledger pairs discovered under {work_pairs_root}. Each pair needs an "
            "'input' folder with the source PDFs."
        )

    logger.info("Discovered %s pair(s): %s", len(pair_ids), ", ".join(pair_ids))

    results: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    for pair_id in pair_ids:
        logger.info("Building reconciliation workbook for %s", pair_id)
        try:
            result = build_reconciliation_workbook(
                pair_id=pair_id,
                refresh_formalized=refresh_formalized,
                ai_repair_batches=ai_repair_batches,
                reference_workbook=reference_workbook,
                strict=strict,
                dry_run=dry_run,
            )
            results.append(result)
        except Exception as exc:  # noqa: BLE001 - record and continue other pairs
            logger.error("Failed to build %s: %s", pair_id, exc)
            failures.append({"pair_id": pair_id, "error": str(exc)})

    manifest = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "work_pairs_root": str(work_pairs_root),
        "refresh_formalized": refresh_formalized,
        "ai_repair_batches": ai_repair_batches,
        "dry_run": dry_run,
        "pair_count": len(pair_ids),
        "pair_ids": pair_ids,
        "succeeded": len(results),
        "failed": len(failures),
        "results": results,
        "failures": failures,
    }

    if not dry_run:
        manifest_dir = settings.project_root / "data/04_outputs/run_manifests"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        manifest_path = manifest_dir / f"recon_run_{stamp}.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        manifest["manifest_path"] = str(manifest_path)

    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build reconciliation workbooks for all discovered ledger pairs.",
    )
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

    manifest = build_all(
        refresh_formalized=args.refresh_formalized,
        ai_repair_batches=max(0, args.ai_repair_batches),
        reference_workbook=args.reference_workbook,
        strict=args.strict,
        dry_run=args.dry_run,
    )

    print("=" * 70)
    print(f"Pairs discovered: {manifest['pair_count']} | "
          f"succeeded: {manifest['succeeded']} | failed: {manifest['failed']}")
    for result in manifest["results"]:
        print("-" * 70)
        print(f"pair_id:            {result.get('pair_id')}")
        print(f"submission_path:    {result.get('submission_path')}")
        print(f"central_submission: {result.get('submission_central_path')}")
        print(f"recon_workbook:     {result.get('output_path')}")
    for failure in manifest["failures"]:
        print("-" * 70)
        print(f"FAILED {failure['pair_id']}: {failure['error']}")
    if manifest.get("manifest_path"):
        print("=" * 70)
        print(f"Run manifest: {manifest['manifest_path']}")


if __name__ == "__main__":
    main()
