"""Smoke tests: the project modules import cleanly (no PDF / network I/O)."""

from __future__ import annotations

import importlib
from pathlib import Path

MODULES = [
    ("src.config", "settings"),
    ("src.extraction.extract_pdf_pair_to_workbook", "main"),
    ("src.tools.dump_py_to_txt", "main"),
    ("src.providers.ai_client", "build_ai_client"),
    ("src.providers.openai_compatible_client", "OpenAICompatibleClient"),
    ("src.standardization.type_labeler", "label_type"),
    ("src.standardization.ledger_schema", "parse_ai_result"),
    ("src.standardization.prompt_builder", "build_messages"),
    ("src.standardization.standardize_pair_with_ai", "main"),
    ("src.excel.workbook_io", "write_workbook"),
    ("src.excel.profile_reference_workbook", "main"),
    ("src.excel.compare_standardized_to_reference", "main"),
    ("src.formalization.ledger_models", "LedgerRow"),
    ("src.formalization.ledger_cleaning", "parse_amount"),
    ("src.formalization.reference_normalization", "normalize_reference"),
    ("src.formalization.type_classification", "classify_type"),
    ("src.formalization.pdf_ledger_extractor", "extract_ledger"),
    ("src.formalization.ai_layout_profiler", "profile_layout"),
    ("src.formalization.validation", "build_validation_report"),
    ("src.formalization.workbook_writer", "write_formalized_workbook"),
    ("src.formalization.formalize_pair_ledgers", "main"),
]


def test_import_config() -> None:
    module = importlib.import_module("src.config")
    assert hasattr(module, "settings")


def test_import_extract_pdf_pair_to_workbook() -> None:
    module = importlib.import_module("src.extraction.extract_pdf_pair_to_workbook")
    assert hasattr(module, "main")


def test_import_dump_py_to_txt() -> None:
    module = importlib.import_module("src.tools.dump_py_to_txt")
    assert hasattr(module, "main")


def test_all_modules_import_with_expected_export() -> None:
    for module_name, export in MODULES:
        module = importlib.import_module(module_name)
        assert hasattr(module, export), f"{module_name} missing {export}"


def test_no_source_prints_api_key() -> None:
    """No project source may print the API key or its env var."""
    roots = [
        Path(__file__).resolve().parent.parent / "src",
        Path(__file__).resolve().parent.parent / "scripts",
    ]
    offenders: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        for py_file in root.rglob("*"):
            if py_file.suffix.lower() not in {".py", ".ps1", ".cmd"}:
                continue
            text = py_file.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                lowered = stripped.lower()
                if ("print(" in lowered or "logger." in lowered or "write-host" in lowered) and (
                    "ai_api_key" in lowered or "get-content .env" in lowered
                ):
                    offenders.append(f"{py_file.name}: {stripped}")
    assert not offenders, f"Potential API key logging found: {offenders}"
