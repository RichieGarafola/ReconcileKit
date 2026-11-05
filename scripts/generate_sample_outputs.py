"""Regenerate (or check) the committed sample outputs in docs/sample-output/.

The outputs come from the bundled synthetic demo with its fixed as-of date, so they are
reproducible on any machine. Files in the export package that carry export timestamps
(summary.json, manifest.json, README.txt, review_history.csv) are deliberately excluded.

    python scripts/generate_sample_outputs.py          # rewrite docs/sample-output/
    python scripts/generate_sample_outputs.py --check  # exit 1 if they are stale

tests/test_sample_outputs.py runs the same comparison in CI.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import tempfile
import zipfile
from pathlib import Path

from reconcilekit import cli

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "sample-output"
PACKAGE_FILES = (
    "invoice_results.csv",
    "payment_results.csv",
    "exceptions.csv",
    "validation_errors.csv",
)


def _run_cli(args: list[str]) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.main(args)
    return buf.getvalue()


def build() -> dict[str, str]:
    """Return {filename: text} for every sample output, with temp paths removed."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db, export_dir = tmp_path / "demo.duckdb", tmp_path / "exports"
        text = _run_cli(["demo", "--db", str(db), "--export-dir", str(export_dir)])
        as_json = _run_cli(["demo", "--db", str(db), "--json"])
        (zip_path,) = export_dir.glob("reconcilekit_RUN-*.zip")
        with zipfile.ZipFile(zip_path) as zf:
            files = {name: zf.read(name).decode("utf-8") for name in PACKAGE_FILES}
    text = text.replace(str(export_dir), "exports").replace("\\", "/")
    files["cli-demo.txt"] = text
    files["cli-demo.json"] = as_json
    return {name: body.replace("\r\n", "\n") for name, body in files.items()}


def stale_files(out_dir: Path = OUT_DIR) -> list[str]:
    """Names of sample files that are missing or differ from a fresh build."""
    stale = []
    for name, body in build().items():
        path = out_dir / name
        current = path.read_text(encoding="utf-8").replace("\r\n", "\n") if path.exists() else None
        if current != body:
            stale.append(name)
    return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="Only report stale files")
    args = parser.parse_args(argv)
    if args.check:
        stale = stale_files()
        for name in stale:
            print(f"stale: docs/sample-output/{name}", file=sys.stderr)
        return 1 if stale else 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, body in build().items():
        (OUT_DIR / name).write_text(body, encoding="utf-8", newline="\n")
        print(f"wrote docs/sample-output/{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
