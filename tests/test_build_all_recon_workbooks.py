"""Tests for the all-pairs reconciliation runner discovery logic."""

from __future__ import annotations

from pathlib import Path

from src.reconciliation.build_all_recon_workbooks import discover_pair_ids
from src.reconciliation import build_recon_workbook


def test_discovers_only_dirs_with_input_folder(tmp_path: Path) -> None:
    (tmp_path / "pair_002_b" / "input").mkdir(parents=True)
    (tmp_path / "pair_001_a" / "input").mkdir(parents=True)
    (tmp_path / "not_a_pair").mkdir()  # no input folder -> ignored
    (tmp_path / "loose_file.txt").write_text("x", encoding="utf-8")

    pairs = discover_pair_ids(tmp_path)
    # Sorted, and only the two directories that contain an input folder.
    assert pairs == ["pair_001_a", "pair_002_b"]


def test_missing_root_returns_empty(tmp_path: Path) -> None:
    assert discover_pair_ids(tmp_path / "does_not_exist") == []


def test_repair_formalization_inherits_explicit_ai_gate(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_run(cmd, *, check, env, cwd):  # noqa: ARG001
        captured.update(env)

    monkeypatch.setenv("AI_ENABLED", "false")
    monkeypatch.setenv("AI_DATA_APPROVAL", "local_only")
    monkeypatch.setattr(build_recon_workbook.subprocess, "run", fake_run)
    build_recon_workbook._run_formalization("pair_x", repair_mode=True)

    assert captured["AI_ENABLED"] == "false"
    assert captured["AI_DATA_APPROVAL"] == "local_only"
    assert captured["AI_FORMALIZATION_MODE"] == "repair_failed_rows"
