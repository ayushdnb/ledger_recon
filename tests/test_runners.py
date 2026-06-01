"""Tests for Windows runner scripts and documentation."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_runner_scripts_exist() -> None:
    ps1 = REPO_ROOT / "scripts" / "run_ai_standardization.ps1"
    cmd = REPO_ROOT / "scripts" / "run_ai_standardization.cmd"
    assert ps1.is_file(), f"Missing {ps1}"
    assert cmd.is_file(), f"Missing {cmd}"


def test_runners_use_venv_python_not_activation() -> None:
    ps1_text = (REPO_ROOT / "scripts" / "run_ai_standardization.ps1").read_text(
        encoding="utf-8"
    )
    cmd_text = (REPO_ROOT / "scripts" / "run_ai_standardization.cmd").read_text(
        encoding="utf-8"
    )
    assert "activate.ps1" not in ps1_text.lower()
    assert r".venv\Scripts\python.exe" in ps1_text.replace("/", "\\")
    assert r".venv\Scripts\python.exe" in cmd_text.replace("/", "\\")
    assert "standardize_pair_with_ai" in ps1_text
    assert "standardize_pair_with_ai" in cmd_text


def test_runners_do_not_print_secrets() -> None:
    for name in ("run_ai_standardization.ps1", "run_ai_standardization.cmd"):
        text = (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8").lower()
        assert "ai_api_key" not in text
        assert "get-content .env" not in text
        assert "type .env" not in text


def test_readme_documents_windows_runners() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "run_ai_standardization.ps1" in readme
    assert "run_ai_standardization.cmd" in readme
    assert "Activate.ps1" in readme
    assert "Do not paste API keys" in readme or "Do **not** paste API keys" in readme
