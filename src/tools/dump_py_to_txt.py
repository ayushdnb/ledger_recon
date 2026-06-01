"""Dump project source (no financial data) into a single timestamped .txt.

Collects the project tree, key config files, Cursor rules, and all Python sources
under src/ and tests/ into one text file for sharing / review.

It intentionally excludes financial PDFs, Excel workbooks, .env secrets, generated
outputs, and the staged ledger data folders.

Run from the project root:

    python -m src.tools.dump_py_to_txt

Or directly (from any directory):

    python src/tools/dump_py_to_txt.py
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path


def _bootstrap_import_path() -> None:
    """Allow direct script execution when the project root is not on sys.path."""
    if __package__ is not None:
        return
    project_root = Path(__file__).resolve().parents[2]
    root_str = str(project_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)


_bootstrap_import_path()
from src.config import settings

logger = logging.getLogger("dump_py_to_txt")

WARNING_LINE = (
    "This dump intentionally excludes financial PDFs, Excel workbooks, .env secrets, "
    "and generated outputs."
)

# Directory names (relative to project root) whose contents are never walked.
EXCLUDED_DIR_NAMES = {
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".git",
}

# Relative directory paths (POSIX style) that are excluded.
EXCLUDED_REL_DIRS = {
    "data/00_original_uploads",
    "data/01_source_ledgers",
    "data/02_work_pairs",
    "data/03_reference_workbooks",
    "data/04_outputs",
    "data/99_archive",
}

# File names that must never be included (secrets).
EXCLUDED_FILE_NAMES = {".env"}

# File suffixes that must never be included (financial / binary content).
EXCLUDED_SUFFIXES = {
    ".pdf",
    ".xls",
    ".xlsx",
    ".xlsm",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
    ".pyc",
}


def _rel_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_excluded_dir(path: Path, root: Path) -> bool:
    if path.name in EXCLUDED_DIR_NAMES:
        return True
    rel = _rel_posix(path, root)
    return any(rel == d or rel.startswith(d + "/") for d in EXCLUDED_REL_DIRS)


def _is_excluded_file(path: Path, root: Path) -> bool:
    if path.name in EXCLUDED_FILE_NAMES:
        return True
    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return True
    rel = _rel_posix(path, root)
    return any(rel.startswith(d + "/") for d in EXCLUDED_REL_DIRS)


def build_project_tree(root: Path) -> str:
    """Render an indented tree of the project, pruning excluded dirs/files."""
    lines: list[str] = [root.name + "/"]

    def walk(directory: Path, prefix: str) -> None:
        try:
            entries = sorted(
                directory.iterdir(),
                key=lambda p: (p.is_file(), p.name.lower()),
            )
        except PermissionError:
            return
        for entry in entries:
            if entry.is_dir():
                if _is_excluded_dir(entry, root):
                    lines.append(f"{prefix}{entry.name}/  [excluded]")
                    continue
                lines.append(f"{prefix}{entry.name}/")
                walk(entry, prefix + "  ")
            else:
                if _is_excluded_file(entry, root):
                    continue
                lines.append(f"{prefix}{entry.name}")

    walk(root, "  ")
    return "\n".join(lines)


def _read_text_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"<<could not read {path.name}: {exc}>>"


def _section(title: str) -> str:
    bar = "=" * 80
    return f"\n{bar}\n{title}\n{bar}\n"


def _file_block(path: Path, root: Path) -> str:
    rel = _rel_posix(path, root)
    return _section(f"FILE: {rel}") + _read_text_safe(path) + "\n"


def collect_dump(root: Path) -> str:
    """Assemble the full dump text."""
    timestamp = datetime.now(timezone.utc).isoformat()
    parts: list[str] = []

    parts.append(_section("PROJECT CONTEXT DUMP"))
    parts.append(f"Generated (UTC, timezone-aware ISO 8601): {timestamp}\n")
    parts.append(f"WARNING: {WARNING_LINE}\n")

    parts.append(_section("PROJECT TREE"))
    parts.append(build_project_tree(root) + "\n")

    # Root-level config / docs.
    for rel_name in ("requirements.txt", "pyproject.toml", "README.md", ".env.example"):
        candidate = root / rel_name
        if candidate.is_file():
            parts.append(_file_block(candidate, root))

    # Cursor rules.
    rules_dir = root / ".cursor" / "rules"
    if rules_dir.is_dir():
        for mdc in sorted(rules_dir.glob("*.mdc")):
            parts.append(_file_block(mdc, root))

    # All .py under src/ and tests/.
    for base in ("src", "tests"):
        base_dir = root / base
        if not base_dir.is_dir():
            continue
        for py_file in sorted(base_dir.rglob("*.py")):
            if _is_excluded_dir(py_file.parent, root):
                continue
            if _is_excluded_file(py_file, root):
                continue
            parts.append(_file_block(py_file, root))

    return "\n".join(parts)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    root = settings.project_root
    dump_dir = settings.resolved(settings.project_context_dump_dir)
    dump_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = dump_dir / f"project_context_dump__{stamp}.txt"

    dump_text = collect_dump(root)
    output_path.write_text(dump_text, encoding="utf-8")

    logger.info("Project context dump written: %s", output_path)
    print(f"Project context dump: {output_path}")


if __name__ == "__main__":
    main()
