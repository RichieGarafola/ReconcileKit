"""The Power BI project (RK.pbip): generated files, model/report consistency, and evidence.

Power BI Desktop is not available in CI. Execution was verified in Desktop by
powerbi/scripts/validate_model.py; these tests check that the committed project is what
the generator produces, that every report field exists in the model, and that the
committed validation report still matches its inputs.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWERBI = ROOT / "powerbi"
SM = POWERBI / "RK.SemanticModel" / "definition"
REPORT = POWERBI / "RK.Report" / "definition"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, POWERBI / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


pbip = _load("build_pbip")
validator = _load("validate_model")


def _tmdl_names(text: str, kind: str) -> set[str]:
    return {
        n.strip("'").replace("''", "'") for n in re.findall(rf"^\t{kind} ('[^']+'|\S+)", text, re.M)
    }


def tmdl_objects() -> dict[str, dict[str, set[str]]]:
    """table -> {"columns": {...}, "measures": {...}} parsed from the TMDL files."""
    out = {}
    for f in (SM / "tables").glob("*.tmdl"):
        text = f.read_text(encoding="utf-8")
        table = re.search(r"^table (.+)$", text, re.M).group(1).strip("'")
        out[table] = {
            "columns": _tmdl_names(text, "column"),
            "measures": _tmdl_names(text, "measure"),
        }
    return out


def visual_files() -> list[Path]:
    return sorted(REPORT.glob("pages/*/visuals/*/visual.json"))


def field_refs(obj):
    """Yield (kind, entity, property) for every Column/Measure reference in a visual."""
    if isinstance(obj, dict):
        for kind in ("Column", "Measure"):
            node = obj.get(kind)
            if isinstance(node, dict) and "Property" in node:
                source = node.get("Expression", {}).get("SourceRef", {})
                if "Entity" in source:
                    yield kind, source["Entity"], node["Property"]
        for value in obj.values():
            yield from field_refs(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from field_refs(value)


def test_committed_project_matches_the_generator():
    assert pbip.stale_files() == [], "Run: python powerbi/scripts/build_pbip.py"


def test_model_tables_and_measures_match_the_catalog():
    objects = tmdl_objects()
    assert set(objects) == set(pbip.ds.POWERBI_TABLES)
    dax = (POWERBI / "model" / "measures.dax").read_text(encoding="utf-8")
    catalog = {(t, n) for t, n, _ in pbip.parse_measures(dax)}
    model = {(t, m) for t, o in objects.items() for m in o["measures"]}
    assert model == catalog
    assert len(model) == 31


def test_money_columns_are_fixed_decimal_and_cents_are_whole_numbers():
    for f in (SM / "tables").glob("*.tmdl"):
        text = f.read_text(encoding="utf-8")
        for name, body in re.findall(r"^\tcolumn (\S+)\n((?:\t\t.*\n)+)", text, re.M):
            dtype = re.search(r"dataType: (\w+)", body).group(1)
            if name.endswith(("_usd", "_pct")):
                assert dtype == "decimal", (f.name, name)  # Fixed decimal, not double
            if name.endswith("_cents"):
                assert dtype == "int64", (f.name, name)
            assert dtype != "double", (f.name, name)


def test_relationships_are_the_documented_seven():
    text = (SM / "relationships.tmdl").read_text(encoding="utf-8")
    pairs = re.findall(r"fromColumn: (.+)\n\ttoColumn: (.+)", text)
    assert len(pairs) == 7
    assert all(to.split(".")[0] in ("Run", "Customer") for _, to in pairs)
    assert "crossFilteringBehavior" not in text  # default: single direction
    assert "isActive: false" not in text


def test_model_discourages_implicit_measures():
    assert "\tdiscourageImplicitMeasures" in (SM / "model.tmdl").read_text(encoding="utf-8")


def test_every_report_field_exists_in_the_model():
    objects = tmdl_objects()
    refs = [(f, r) for f in visual_files() for r in field_refs(json.loads(f.read_text("utf-8")))]
    assert refs
    for f, (kind, entity, prop) in refs:
        assert entity in objects, (f, entity)
        bucket = "columns" if kind == "Column" else "measures"
        assert prop in objects[entity][bucket], (f.relative_to(POWERBI), kind, entity, prop)


def test_numeric_visual_values_are_measures_not_implicit_aggregations():
    for f in visual_files():
        visual = json.loads(f.read_text("utf-8"))["visual"]
        state = visual.get("query", {}).get("queryState", {})
        if visual["visualType"] in ("card", "clusteredBarChart", "waterfallChart", "multiRowCard"):
            role = "Values" if "Values" in state else "Y"
            for p in state.get(role, {}).get("projections", []):
                assert "Measure" in p["field"], f.relative_to(POWERBI)


def test_three_pages_each_with_a_synced_single_select_snapshot_slicer():
    pages = json.loads((REPORT / "pages" / "pages.json").read_text("utf-8"))
    assert pages["pageOrder"] == ["overview", "exceptions", "quality"]
    assert pages["activePageName"] == "overview"
    for page in pages["pageOrder"]:
        slicer = json.loads(
            (REPORT / "pages" / page / "visuals" / "snapshot" / "visual.json").read_text("utf-8")
        )
        visual = slicer["visual"]
        assert visual["syncGroup"]["groupName"] == "snapshot"
        assert (
            visual["objects"]["selection"][0]["properties"]["singleSelect"]["expr"]["Literal"][
                "Value"
            ]
            == "true"
        )
        selected = visual["objects"]["general"][0]["properties"]["filter"]["filter"]["Where"]
        assert json.dumps(selected).count(pbip.DEFAULT_SNAPSHOT) == 1


def test_default_snapshot_is_the_report_default_run():
    data = json.loads((POWERBI / "dataset" / "expected_kpis.json").read_text("utf-8"))
    (default,) = [r for r in data["runs"] if r["is_report_default"]]
    assert default["snapshot_label"] == pbip.DEFAULT_SNAPSHOT


def test_comparison_table_is_not_filtered_by_the_slicer():
    page = json.loads((REPORT / "pages" / "overview" / "page.json").read_text("utf-8"))
    assert {"source": "snapshot", "target": "compare", "type": "NoFilter"} in page[
        "visualInteractions"
    ]


def test_desktop_validation_report_passed_and_is_current():
    text = (POWERBI / "validation" / "desktop-validation.md").read_text(encoding="utf-8")
    m = re.search(r"\*\*Result: (\d+) of (\d+) checks passed\.\*\*", text)
    assert m and m.group(1) == m.group(2) and int(m.group(2)) >= 76
    assert "| FAIL |" not in text
    for name, digest in validator.input_hashes().items():
        assert f"- `{name}`: `{digest}`" in text, (
            f"{name} changed since the Desktop validation; rerun powerbi/scripts/validate_model.py"
        )


@pytest.mark.parametrize(
    "name",
    [
        "1-reconciliation-overview.png",
        "2-exception-management.png",
        "3-data-quality-auditability.png",
    ],
)
def test_screenshots_exist_and_are_real_images(name):
    data = (POWERBI / "screenshots" / name).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 20_000
