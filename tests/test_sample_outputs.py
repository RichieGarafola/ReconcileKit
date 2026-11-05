"""The committed docs/sample-output/ files must match what the code produces today."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "generate_sample_outputs.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("generate_sample_outputs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_sample_outputs_are_current():
    stale = _load_script().stale_files()
    assert not stale, (
        f"Sample outputs are stale: {stale}. Run: python scripts/generate_sample_outputs.py"
    )


def test_sample_outputs_contain_no_machine_paths():
    gen = _load_script()
    for name in gen.build():
        text = (gen.OUT_DIR / name).read_text(encoding="utf-8")
        assert "AppData" not in text and "/tmp/" not in text and "\\Temp\\" not in text, name


def test_readme_bridge_block_matches_sample_output():
    # read_text() uses universal newlines, so CRLF checkouts compare equal.
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```text\n(.*?)```", readme, re.S)
    sample = (_load_script().OUT_DIR / "cli-demo.txt").read_text(encoding="utf-8")
    assert sample in blocks, "README demo output differs from docs/sample-output/cli-demo.txt"


def test_bundled_demo_bytes_are_platform_independent():
    """Run IDs hash input bytes, so CRLF conversion on checkout would change the demo's ID."""
    from reconcilekit.demo import load_demo

    for data in load_demo():
        assert b"\r" not in data, "demo CSVs must keep LF line endings (see .gitattributes)"
