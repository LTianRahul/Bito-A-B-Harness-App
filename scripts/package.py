#!/usr/bin/env python3
"""Build a clean, shareable zip of the A/B Benchmark for a client.

    python3 scripts/package.py

Produces  dist-package/ab-benchmark-<date>.zip  containing everything the client
needs to run (code + prebuilt UI + one-command launchers) and NOTHING private:
it excludes secrets (configs/ with your Bito token, results.db, indexed-repos.txt,
your prompt_sets/), runtime output, caches, the venv, and node_modules.

The client just unzips and runs the launcher for their OS — no manual setup.
"""
from __future__ import annotations

import fnmatch
import stat
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "dist-package"
PKG_NAME = f"ab-benchmark-{datetime.now():%Y%m%d}"

# Directory names excluded anywhere in the tree (secrets, runtime, caches, builds).
EXCLUDE_DIRS = {
    ".venv", "node_modules", "__pycache__", ".git", "dist-package",
    "configs", "runs", "judgments", "reports", "prompt_sets",
}
# File-name globs excluded anywhere.
EXCLUDE_GLOBS = [
    "*.pyc", ".DS_Store", "results.db", "results.db-*",
    "indexed-repos.txt", "*.bak-*", "prompts.json",
]
# Files that must be executable for the client (launchers).
EXEC_SUFFIXES = (".sh", ".command")


def excluded(rel: Path) -> bool:
    if set(rel.parts) & EXCLUDE_DIRS:
        return True
    return any(fnmatch.fnmatch(rel.name, g) for g in EXCLUDE_GLOBS)


def main() -> None:
    if not (ROOT / "frontend" / "dist" / "index.html").exists():
        print("WARNING: frontend/dist is missing — build the UI first "
              "(cd frontend && npm run build) so the client gets a prebuilt UI.")
    OUT_DIR.mkdir(exist_ok=True)
    zip_path = OUT_DIR / f"{PKG_NAME}.zip"
    n = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(ROOT.rglob("*")):
            if path.is_dir() or path.is_symlink():
                continue
            rel = path.relative_to(ROOT)
            if excluded(rel):
                continue
            arcname = str(Path(PKG_NAME) / rel)  # nest under a clean top-level folder
            info = zipfile.ZipInfo(arcname, date_time=datetime.now().timetuple()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            # Preserve an executable bit on the launcher scripts so double-click /
            # ./start.sh works straight out of the zip on macOS and Linux.
            mode = 0o755 if path.suffix in EXEC_SUFFIXES else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            z.writestr(info, path.read_bytes())
            n += 1
    size_mb = zip_path.stat().st_size / 1_048_576
    print(f"Packaged {n} files → {zip_path}  ({size_mb:.1f} MB)")
    print("Excluded: configs/ (Bito token), results.db, indexed-repos.txt, "
          "prompt_sets/, runs/, caches, .venv, node_modules.")
    print("Client runs:  macOS → start.command   Windows → start.bat   Linux → ./start.sh")


if __name__ == "__main__":
    main()
