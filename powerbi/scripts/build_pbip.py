"""Generate (or check) the Power BI project: RK.pbip, RK.SemanticModel (TMDL), RK.Report (PBIR).

Everything is derived from files that are already tested:

* table columns and types   <- dataset/model/*.parquet (via build_dataset.py's type map)
* measures                  <- model/measures.dax (the DEFINE block)
* measure descriptions      <- model/model.md (measure catalog table)
* relationships             <- RELATIONSHIPS below (documented in model/model.md)

    python powerbi/scripts/build_pbip.py           # rewrite the project files
    python powerbi/scripts/build_pbip.py --check   # exit 1 if they are stale

Power BI Desktop adds .pbi/ caches when it opens the project; those are git-ignored.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

POWERBI = Path(__file__).resolve().parents[1]
MODEL_FILES = POWERBI / "dataset" / "model"
PROJECT_FILES = ("RK.pbip", "RK.SemanticModel", "RK.Report")
# Default for the DataFolder parameter: the documented clone location (outside OneDrive).
DEFAULT_DATA_FOLDER = r"C:\dev\ReconcileKit\powerbi\dataset\model"
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric"
VERSIONS = {"visual": "2.13.0", "page": "2.1.0", "report": "3.3.0"}
BASE_THEME = "CY26SU10"  # shipped with Power BI Desktop 2.159 (October 2026)
# Report default (model.md): the run with the latest as-of date.
DEFAULT_SNAPSHOT = "2026-04 follow-up"
_DEF = f"{SCHEMA}/item/report/definition"
VISUAL_SCHEMA = f"{_DEF}/visualContainer/{VERSIONS['visual']}/schema.json"
REPORT_SCHEMA = f"{_DEF}/report/{VERSIONS['report']}/schema.json"


def _load_dataset_builder():
    spec = importlib.util.spec_from_file_location(
        "build_dataset", POWERBI / "scripts" / "build_dataset.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ds = _load_dataset_builder()

# --------------------------------------------------------------------------- semantic model

TMDL_TYPES = {
    "VARCHAR": "string",
    "BIGINT": "int64",
    "INTEGER": "int64",
    "DECIMAL(38,2)": "decimal",  # Fixed decimal number
    "BOOLEAN": "boolean",
    "DATE": "dateTime",
    "TIMESTAMP": "dateTime",
}
USD = r"\$#,0.00;-\$#,0.00;\$#,0.00"
COLUMN_FORMATS = {
    "DATE": "yyyy-mm-dd",
    "TIMESTAMP": "yyyy-mm-dd hh:nn",
    "BIGINT": "0",
    "INTEGER": "0",
}
HIDDEN_COLUMNS = {
    "run_invoice_key",
    "run_payment_key",
    "run_exception_key",
    "aging_order",
    "snapshot_order",
    "step_order",
    "invoice_record_id",
    "payment_record_id",
    "record_ids",
    "related_invoice_record_ids",
}
SORT_BY = {
    ("Run", "snapshot_label"): "snapshot_order",
    ("Open Invoice", "aging_bucket"): "aging_order",
    ("Bridge Step", "step_label"): "step_order",
}
MEASURE_FORMATS = {
    "Settled %": "0.00%",
    **{
        name: USD
        for name in (
            "Eligible Invoice Value",
            "Settled Invoice Value",
            "Outstanding Balance",
            "Overpayments",
            "Net Difference",
            "Payment Exception Amount",
            "Customer Outstanding",
            "Bridge Amount",
        )
    },
    **{
        name: "#,0"
        for name in (
            "Runs In Context",
            "Quarantined Rows",
            "Payment Exception Count",
            "Exception Count",
            "Unresolved Exceptions",
            "Never Reviewed",
            "Validation Exceptions",
            "Open Invoice Count",
            "Controls Passed",
        )
    },
}
DISPLAY_FOLDERS = {
    "Run": "KPIs",
    "Payment Exception": "Exceptions",
    "Exception": "Exceptions",
    "Open Invoice": "Exceptions",
    "Bridge Step": "Bridge",
}
# (from table, from column) -> (to table, to column); all many-to-one, single direction.
RELATIONSHIPS = (
    ("Open Invoice", "run_id", "Run", "run_id"),
    ("Payment Exception", "run_id", "Run", "run_id"),
    ("Exception", "run_id", "Run", "run_id"),
    ("Bridge Step", "run_id", "Run", "run_id"),
    ("Open Invoice", "customer_id", "Customer", "customer_id"),
    ("Payment Exception", "customer_id", "Customer", "customer_id"),
    ("Exception", "customer_id", "Customer", "customer_id"),
)
TABLE_DESCRIPTIONS = {
    "Run": "One row per reconciliation run (one complete snapshot). "
    "Engine totals; never summed across runs.",
    "Open Invoice": "Invoices with an outstanding balance, one row per invoice per run.",
    "Payment Exception": "Eligible payments that were not applied, one row per payment per run.",
    "Exception": "Review-queue exceptions with their latest disposition, "
    "one row per exception per run.",
    "Customer": "Customer IDs that appear in the fact tables.",
    "Bridge Step": "Reconciliation bridge lines per run; signed amounts drive the waterfall.",
}


def tmdl_name(name: str) -> str:
    return (
        name
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
        else "'" + name.replace("'", "''") + "'"
    )


def parse_measures(dax_text: str) -> list[tuple[str, str, str]]:
    """(table, name, expression) for every MEASURE in the DEFINE block of measures.dax."""
    define = dax_text.split("DEFINE", 1)[1].split("\nEVALUATE", 1)[0]
    define = "\n".join(line for line in define.splitlines() if not line.strip().startswith("//"))
    pattern = re.compile(r"MEASURE '([^']+)'\[([^\]]+)\] =(.*?)(?=\n\s*MEASURE |\Z)", re.S)
    out = []
    for table, name, expr in pattern.findall(define):
        lines = [ln.rstrip() for ln in expr.strip("\n").splitlines() if ln.strip()]
        indent = min(len(ln) - len(ln.lstrip()) for ln in lines)
        out.append((table, name, "\n".join(ln[indent:] for ln in lines)))
    return out


def measure_descriptions() -> dict[str, str]:
    doc = (POWERBI / "model" / "model.md").read_text(encoding="utf-8")
    return {
        name: text.strip() for name, text in re.findall(r"^\| `([^`]+)` \| ([^|]+) \|", doc, re.M)
    }


def _m_source(file: str, schema: list[tuple[str, str]]) -> list[str]:
    types = [f'{{"{col}", {ds.M_TYPES[dtype]}}}' for col, dtype in schema]
    lines = [
        "let",
        f'    Source = Parquet.Document(File.Contents(DataFolder & "\\{file}.parquet")),',
        "    Typed = Table.TransformColumnTypes(",
        "        Source,",
        "        {",
    ]
    lines += [f"            {t}," for t in types[:-1]] + [f"            {types[-1]}"]
    lines += ["        }", "    )", "in", "    Typed"]
    return lines


def table_tmdl(table: str, file: str, measures: list[tuple[str, str, str]], docs: dict) -> str:
    schema, _ = ds._parquet_rows(MODEL_FILES / f"{file}.parquet")
    out = [f"/// {TABLE_DESCRIPTIONS[table]}", f"table {tmdl_name(table)}", ""]
    for _, name, expr in (m for m in measures if m[0] == table):
        if name in docs:
            out.append(f"\t/// {docs[name]}")
        out.append(f"\tmeasure {tmdl_name(name)} =")
        out += [f"\t\t\t{ln}" for ln in expr.splitlines()]
        if name in MEASURE_FORMATS:
            out.append(f"\t\tformatString: {MEASURE_FORMATS[name]}")
        out += [f"\t\tdisplayFolder: {DISPLAY_FOLDERS[table]}", ""]
    for col, dtype in schema:
        out.append(f"\tcolumn {tmdl_name(col)}")
        out.append(f"\t\tdataType: {TMDL_TYPES[dtype]}")
        if dtype == "DECIMAL(38,2)":
            out.append(f"\t\tformatString: {'0.00' if col.endswith('_pct') else USD}")
        elif dtype in COLUMN_FORMATS:
            out.append(f"\t\tformatString: {COLUMN_FORMATS[dtype]}")
        if col in HIDDEN_COLUMNS:
            out.append("\t\tisHidden")
        out.append("\t\tsummarizeBy: none")
        out.append(f"\t\tsourceColumn: {col}")
        if (table, col) in SORT_BY:
            out.append(f"\t\tsortByColumn: {SORT_BY[(table, col)]}")
        out += ["", "\t\tannotation SummarizationSetBy = User", ""]
    out += [f"\tpartition {tmdl_name(table)} = m", "\t\tmode: import", "\t\tsource ="]
    out += [f"\t\t\t\t{ln}" for ln in _m_source(file, schema)]
    out += ["", "\tannotation PBI_ResultType = Table", ""]
    return "\n".join(out)


def semantic_model_files() -> dict[str, str]:
    dax = (POWERBI / "model" / "measures.dax").read_text(encoding="utf-8")
    measures = parse_measures(dax)
    docs = measure_descriptions()
    tables = ds.POWERBI_TABLES
    files = {
        "definition.pbism": json.dumps(
            {
                "$schema": f"{SCHEMA}/item/semanticModel/definitionProperties/1.0.0/schema.json",
                "version": "4.0",
                "settings": {},
            },
            indent=2,
        ),
        "definition/database.tmdl": "database\n\tcompatibilityLevel: 1600\n",
        "definition/model.tmdl": "\n".join(
            [
                "model Model",
                "\tculture: en-US",
                "\tdefaultPowerBIDataSourceVersion: powerBI_V3",
                "\tdiscourageImplicitMeasures",
                "\tsourceQueryCulture: en-US",
                "\tdataAccessOptions",
                "\t\tlegacyRedirects",
                "\t\treturnErrorValuesAsNull",
                "",
                "annotation __PBI_TimeIntelligenceEnabled = 0",
                "",
                "annotation PBI_QueryOrder = " + json.dumps(["DataFolder", *tables]),
                "",
                *[f"ref table {tmdl_name(t)}" for t in tables],
                "",
            ]
        ),
        "definition/expressions.tmdl": "\n".join(
            [
                "/// Absolute path of powerbi\\dataset\\model. "
                "Power Query does not resolve relative paths.",
                f'expression DataFolder = "{DEFAULT_DATA_FOLDER}" meta [IsParameterQuery=true, '
                'Type="Text", IsParameterQueryRequired=true]',
                "",
                "\tannotation PBI_ResultType = Text",
                "",
            ]
        ),
        "definition/relationships.tmdl": "\n".join(
            line
            for i, (ft, fc, tt, tc) in enumerate(RELATIONSHIPS, start=1)
            for line in (
                f"relationship rel_{i:02d}_{re.sub(r'[^a-z]', '', ft.lower())}_{fc}",
                f"\tfromColumn: {tmdl_name(ft)}.{fc}",
                f"\ttoColumn: {tmdl_name(tt)}.{tc}",
                "",
            )
        ),
    }
    for table, file in tables.items():
        files[f"definition/tables/{table}.tmdl"] = table_tmdl(table, file, measures, docs)
    known = {(t, n) for t, n, _ in measures}
    for name in MEASURE_FORMATS:
        assert any(n == name for _, n in known), f"format for unknown measure {name}"
    return files


# --------------------------------------------------------------------------- report helpers


def lit(value) -> dict:
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (int, float)):
        text = f"{value}D"
    else:
        text = "'" + str(value).replace("'", "''") + "'"
    return {"expr": {"Literal": {"Value": text}}}


def color(hex_code: str) -> dict:
    return {"solid": {"color": lit(hex_code)}}


def measure(entity: str, prop: str) -> dict:
    return {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


def column(entity: str, prop: str) -> dict:
    return {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


def proj(field: dict, display: str | None = None) -> dict:
    kind = "Measure" if "Measure" in field else "Column"
    entity = field[kind]["Expression"]["SourceRef"]["Entity"]
    prop = field[kind]["Property"]
    p = {"field": field, "queryRef": f"{entity}.{prop}", "nativeQueryRef": prop}
    if display:
        p["displayName"] = display
    return p


def title_objects(text: str, size: int = 11) -> dict:
    return {
        "title": [
            {
                "properties": {
                    "show": lit(True),
                    "text": lit(text),
                    "fontSize": lit(size),
                    "fontColor": color("#1F2A37"),
                    "bold": lit(True),
                }
            }
        ],
        "background": [{"properties": {"show": lit(True), "color": color("#FFFFFF")}}],
        "border": [
            {
                "properties": {
                    "show": lit(True),
                    "color": color("#E3E8EF"),
                    "radius": lit(6),
                }
            }
        ],
        "dropShadow": [{"properties": {"show": lit(False)}}],
    }


def categorical_filter(name: str, entity: str, prop: str, values: list[str]) -> dict:
    return {
        "name": name,
        "field": column(entity, prop),
        "type": "Categorical",
        "filter": {
            "Version": 2,
            "From": [{"Name": "t", "Entity": entity, "Type": 0}],
            "Where": [
                {
                    "Condition": {
                        "In": {
                            "Expressions": [
                                {
                                    "Column": {
                                        "Expression": {"SourceRef": {"Source": "t"}},
                                        "Property": prop,
                                    }
                                }
                            ],
                            "Values": [[lit(v)["expr"]] for v in values],
                        }
                    }
                }
            ],
        },
    }


class Page:
    def __init__(self, name: str, display: str):
        self.name, self.display = name, display
        self.visuals: dict[str, dict] = {}
        self.no_filter: list[tuple[str, str]] = []

    def add(self, name, x, y, w, h, visual, filters=None):
        container = {
            "$schema": VISUAL_SCHEMA,
            "name": name,
            "position": {
                "x": x,
                "y": y,
                "z": len(self.visuals) * 1000,
                "height": h,
                "width": w,
                "tabOrder": len(self.visuals) * 1000,
            },
            "visual": visual,
        }
        if filters:
            container["filterConfig"] = {"filters": filters}
        self.visuals[name] = container

    def files(self) -> dict[str, str]:
        page = {
            "$schema": f"{SCHEMA}/item/report/definition/page/{VERSIONS['page']}/schema.json",
            "name": self.name,
            "displayName": self.display,
            "displayOption": "FitToPage",
            "height": 720,
            "width": 1280,
            "objects": {
                "background": [{"properties": {"color": color("#F5F7FA"), "transparency": lit(0)}}]
            },
        }
        if self.no_filter:
            page["visualInteractions"] = [
                {"source": s, "target": t, "type": "NoFilter"} for s, t in self.no_filter
            ]
        out = {f"definition/pages/{self.name}/page.json": _dump(page)}
        for vname, container in self.visuals.items():
            out[f"definition/pages/{self.name}/visuals/{vname}/visual.json"] = _dump(container)
        return out


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def textbox(text: str, size: int = 18, bold: bool = True, hex_color: str = "#1F2A37") -> dict:
    style = {"fontSize": f"{size}pt", "color": hex_color}
    if bold:
        style["fontWeight"] = "bold"
    return {
        "visualType": "textbox",
        "objects": {
            "general": [
                {
                    "properties": {
                        "paragraphs": [{"textRuns": [{"value": text, "textStyle": style}]}]
                    }
                }
            ]
        },
        "visualContainerObjects": {"background": [{"properties": {"show": lit(False)}}]},
        "drillFilterOtherVisuals": True,
    }


def card(m: dict, title: str, size: int = 22, precision: int = 0) -> dict:
    # Display units "None" (1): show exact values, never auto-abbreviated ($4.19K).
    return {
        "visualType": "card",
        "query": {"queryState": {"Values": {"projections": [proj(m)]}}},
        "objects": {
            "labels": [
                {
                    "properties": {
                        "fontSize": lit(size),
                        "color": color("#1F2A37"),
                        "labelDisplayUnits": lit(1),
                        "labelPrecision": lit(precision),
                    }
                }
            ],
            "categoryLabels": [{"properties": {"show": lit(False)}}],
        },
        "visualContainerObjects": title_objects(title, 10),
        "drillFilterOtherVisuals": True,
    }


def snapshot_slicer(default: str) -> dict:
    # A slicer's saved selection lives in objects.general.filter (not in filterConfig, which
    # would filter the slicer's own items).
    selection = categorical_filter("unused", "Run", "snapshot_label", [default])["filter"]
    return {
        "visualType": "slicer",
        "query": {
            "queryState": {"Values": {"projections": [proj(column("Run", "snapshot_label"))]}}
        },
        "objects": {
            "general": [{"properties": {"filter": {"filter": selection}}}],
            "data": [{"properties": {"mode": lit("Dropdown")}}],
            "selection": [{"properties": {"singleSelect": lit(True)}}],
            "header": [{"properties": {"show": lit(True), "text": lit("Snapshot")}}],
        },
        "visualContainerObjects": {"background": [{"properties": {"show": lit(False)}}]},
        "syncGroup": {"groupName": "snapshot", "fieldChanges": True, "filterChanges": True},
        "drillFilterOtherVisuals": True,
    }


def bar(
    category: dict, value: dict, title: str, value_name: str | None = None, sort_desc=True
) -> dict:
    v = {
        "visualType": "clusteredBarChart",
        "query": {
            "queryState": {
                "Category": {"projections": [proj(category)]},
                "Y": {"projections": [proj(value, value_name)]},
            }
        },
        "objects": {
            "labels": [{"properties": {"show": lit(True)}}],
            "dataPoint": [{"properties": {"fill": color("#2B6CB0")}}],
            # Wider label margin so rule codes are not truncated to the same prefix.
            "categoryAxis": [
                {"properties": {"showAxisTitle": lit(False), "maxMarginFactor": lit(55)}}
            ],
            "valueAxis": [{"properties": {"show": lit(False)}}],
        },
        "visualContainerObjects": title_objects(title),
        "drillFilterOtherVisuals": True,
    }
    if sort_desc:
        v["query"]["sortDefinition"] = {"sort": [{"field": value, "direction": "Descending"}]}
    return v


def table(
    fields: list[tuple[dict, str | None]], title: str, sort: tuple[dict, str] | None = None
) -> dict:
    v = {
        "visualType": "tableEx",
        "query": {"queryState": {"Values": {"projections": [proj(f, d) for f, d in fields]}}},
        "objects": {
            "columnHeaders": [
                {"properties": {"fontColor": color("#1F2A37"), "backColor": color("#EDF2F7")}}
            ],
            "grid": [{"properties": {"gridHorizontal": lit(True)}}],
            "total": [{"properties": {"totals": lit(False)}}],
        },
        "visualContainerObjects": title_objects(title),
        "drillFilterOtherVisuals": True,
    }
    if sort:
        v["query"]["sortDefinition"] = {"sort": [{"field": sort[0], "direction": sort[1]}]}
    return v


def header(page: Page, title: str, subtitle: str):
    page.add("title", 24, 6, 760, 52, textbox(title, 18))
    page.add("subtitle", 24, 52, 760, 32, textbox(subtitle, 10, bold=False, hex_color="#52606D"))
    page.add("snapshot", 1020, 14, 236, 64, snapshot_slicer(DEFAULT_SNAPSHOT))
    page.add(
        "banner",
        24,
        86,
        1232,
        36,
        {
            "visualType": "card",
            "query": {
                "queryState": {"Values": {"projections": [proj(measure("Run", "Run Banner"))]}}
            },
            "objects": {
                "labels": [{"properties": {"fontSize": lit(10), "color": color("#334E68")}}],
                "categoryLabels": [{"properties": {"show": lit(False)}}],
            },
            "visualContainerObjects": {
                "background": [{"properties": {"show": lit(True), "color": color("#E6F0FA")}}],
                "border": [{"properties": {"show": lit(False)}}],
            },
            "drillFilterOtherVisuals": True,
        },
    )


# --------------------------------------------------------------------------- report pages


def overview() -> Page:
    p = Page("overview", "Reconciliation Overview")
    header(
        p,
        "Reconciliation Overview",
        "Invoice register vs. payment ledger. Figures cover eligible (validated) records only.",
    )
    kpis = [
        ("Run", "Eligible Invoice Value", "Eligible invoices"),
        ("Run", "Settled Invoice Value", "Settled invoice value"),
        ("Run", "Settled %", "Settled share"),
        ("Run", "Outstanding Balance", "Outstanding balance"),
        ("Payment Exception", "Payment Exception Amount", "Unapplied payments"),
        ("Exception", "Unresolved Exceptions", "Unresolved exceptions"),
    ]
    for i, (entity, name, label) in enumerate(kpis):
        precision = 2 if name in MEASURE_FORMATS and MEASURE_FORMATS[name] != "#,0" else 0
        p.add(
            f"kpi{i + 1}",
            24 + i * 207,
            134,
            195,
            96,
            card(measure(entity, name), label, 20, precision),
        )
    p.add(
        "bridge",
        24,
        244,
        760,
        250,
        {
            "visualType": "waterfallChart",
            "query": {
                "queryState": {
                    "Category": {"projections": [proj(column("Bridge Step", "step_label"))]},
                    "Y": {"projections": [proj(measure("Bridge Step", "Bridge Amount"), "Amount")]},
                },
                "sortDefinition": {
                    "sort": [
                        {"field": column("Bridge Step", "step_label"), "direction": "Ascending"}
                    ]
                },
            },
            "objects": {
                "labels": [
                    {
                        "properties": {
                            "show": lit(True),
                            "labelDisplayUnits": lit(1),
                            "labelPrecision": lit(2),
                        }
                    }
                ],
                "categoryAxis": [{"properties": {"showAxisTitle": lit(False)}}],
                "valueAxis": [
                    {"properties": {"showAxisTitle": lit(False), "labelDisplayUnits": lit(1)}}
                ],
            },
            "visualContainerObjects": title_objects(
                "Net difference explained: outstanding − overpayments − unapplied payments"
            ),
            "drillFilterOtherVisuals": True,
        },
        [categorical_filter("explainsOnly", "Bridge Step", "step_kind", ["explains"])],
    )
    p.add(
        "aging",
        796,
        244,
        460,
        250,
        bar(
            column("Open Invoice", "aging_bucket"),
            measure("Open Invoice", "Customer Outstanding"),
            "Outstanding by aging bucket",
            "Outstanding",
            sort_desc=False,
        ),
    )
    p.visuals["aging"]["visual"]["query"]["sortDefinition"] = {
        "sort": [{"field": column("Open Invoice", "aging_bucket"), "direction": "Ascending"}]
    }
    p.add(
        "compare",
        24,
        508,
        1232,
        196,
        table(
            [
                (column("Run", "snapshot_label"), "Snapshot"),
                (column("Run", "as_of_date"), "As of"),
                (measure("Run", "Eligible Invoice Value"), "Eligible invoices"),
                (measure("Run", "Settled %"), "Settled %"),
                (measure("Run", "Outstanding Balance"), "Outstanding"),
                (measure("Payment Exception", "Payment Exception Amount"), "Unapplied payments"),
                (measure("Exception", "Unresolved Exceptions"), "Unresolved"),
                (measure("Run", "Data Completeness"), "Completeness"),
                (measure("Run", "Arithmetic Status"), "Arithmetic"),
            ],
            "Snapshot comparison "
            "(not filtered by the snapshot selector; two snapshots, not a trend)",
            (column("Run", "snapshot_label"), "Ascending"),
        ),
    )
    p.no_filter.append(("snapshot", "compare"))
    return p


def exceptions() -> Page:
    p = Page("exceptions", "Exception Management")
    header(
        p,
        "Exception Management",
        "Unapplied payments, open balances by customer, "
        "and the review queue for the selected snapshot.",
    )
    kpis = [
        ("Payment Exception", "Payment Exception Count", "Unapplied payments (count)"),
        ("Payment Exception", "Payment Exception Amount", "Unapplied payments (amount)"),
        ("Open Invoice", "Open Invoice Count", "Open invoices"),
        ("Exception", "Unresolved Exceptions", "Unresolved exceptions"),
    ]
    for i, (entity, name, label) in enumerate(kpis):
        precision = 2 if MEASURE_FORMATS.get(name) not in (None, "#,0") else 0
        p.add(
            f"kpi{i + 1}",
            24 + i * 311,
            134,
            299,
            90,
            card(measure(entity, name), label, 20, precision),
        )
    p.add(
        "byStatus",
        24,
        238,
        400,
        210,
        bar(
            column("Payment Exception", "rule_code"),
            measure("Payment Exception", "Payment Exception Amount"),
            "Unapplied payments by reason",
            "Amount",
        ),
    )
    p.add(
        "byCustomer",
        436,
        238,
        400,
        210,
        bar(
            column("Customer", "customer_id"),
            measure("Open Invoice", "Customer Outstanding"),
            "Outstanding by customer",
            "Outstanding",
        ),
    )
    p.add(
        "queue",
        848,
        238,
        408,
        210,
        {
            "visualType": "pivotTable",
            "query": {
                "queryState": {
                    "Rows": {"projections": [proj(column("Exception", "category"), "Category")]},
                    "Columns": {
                        "projections": [
                            proj(column("Exception", "current_disposition"), "Disposition")
                        ]
                    },
                    "Values": {
                        "projections": [proj(measure("Exception", "Exception Count"), "Exceptions")]
                    },
                }
            },
            "objects": {"columnHeaders": [{"properties": {"backColor": color("#EDF2F7")}}]},
            "visualContainerObjects": title_objects("Review queue: category × current disposition"),
            "drillFilterOtherVisuals": True,
        },
    )
    p.add(
        "detail",
        24,
        462,
        1232,
        242,
        table(
            [
                (column("Payment Exception", "payment_id"), "Payment"),
                (column("Payment Exception", "customer_id"), "Customer"),
                (column("Payment Exception", "invoice_id_raw"), "Reference as received"),
                (column("Payment Exception", "payment_date"), "Paid"),
                (column("Payment Exception", "payment_amount_usd"), "Amount"),
                (column("Payment Exception", "status"), "Status"),
                (column("Payment Exception", "rule_code"), "Rule"),
                (column("Payment Exception", "current_disposition"), "Disposition"),
                (column("Payment Exception", "explanation"), "Explanation"),
            ],
            "Payment exceptions to investigate",
            (column("Payment Exception", "payment_amount_usd"), "Descending"),
        ),
    )
    return p


def quality() -> Page:
    p = Page("quality", "Data Quality and Auditability")
    header(
        p,
        "Data Quality and Auditability",
        "Validation exclusions, reconciliation controls, review activity, and source traceability.",
    )
    kpis = [
        ("Run", "Quarantined Rows", "Quarantined rows"),
        ("Exception", "Validation Exceptions", "Validation exceptions"),
        ("Exception", "Never Reviewed", "Never reviewed"),
        ("Run", "Controls Status", "Model controls"),
    ]
    for i, (entity, name, label) in enumerate(kpis):
        p.add(f"kpi{i + 1}", 24 + i * 311, 134, 299, 90, card(measure(entity, name), label, 18))
    p.add(
        "rules",
        24,
        238,
        400,
        250,
        bar(
            column("Exception", "rule_code"),
            measure("Exception", "Validation Exceptions"),
            "Validation exceptions by rule",
            "Exceptions",
        ),
    )
    controls = [
        (measure("Run", "Arithmetic Status"), "Engine invariants"),
        (measure("Run", "Check Outstanding Ties"), "Open items = O"),
        (measure("Run", "Check Payment Exceptions Tie"), "Exceptions = U+M+B"),
        (measure("Run", "Check Bridge"), "Net = O−X−U−M−B"),
        (measure("Run", "Check Bridge Steps"), "Bridge steps tie"),
        (measure("Run", "Check Settled % Matches SQL"), "DAX % = SQL %"),
    ]
    p.add(
        "controls",
        436,
        238,
        400,
        250,
        {
            "visualType": "multiRowCard",
            "query": {"queryState": {"Values": {"projections": [proj(f, d) for f, d in controls]}}},
            "objects": {
                "dataLabels": [{"properties": {"fontSize": lit(11)}}],
                "categoryLabels": [{"properties": {"fontSize": lit(9)}}],
                "card": [{"properties": {"outlineStyle": lit(0)}}],
            },
            "visualContainerObjects": title_objects("Controls (True = passes)"),
            "drillFilterOtherVisuals": True,
        },
    )
    p.add(
        "bridgeTable",
        848,
        238,
        408,
        250,
        table(
            [
                (column("Bridge Step", "step_label"), "Bridge line"),
                (column("Bridge Step", "amount_usd"), "Amount"),
            ],
            "Reconciliation bridge",
            (column("Bridge Step", "step_label"), "Ascending"),
        ),
    )
    p.add(
        "reviews",
        24,
        502,
        820,
        202,
        table(
            [
                (column("Exception", "exception_id"), "Exception"),
                (column("Exception", "rule_code"), "Rule"),
                (column("Exception", "current_disposition"), "Disposition"),
                (column("Exception", "review_event_count"), "Events"),
                (column("Exception", "latest_reviewed_at_utc"), "Latest review (UTC)"),
                (column("Exception", "latest_reviewer"), "Reviewer"),
                (column("Exception", "latest_note"), "Latest note"),
            ],
            "Reviewed exceptions (latest event per exception; full history is not in rk_v1)",
            (column("Exception", "exception_id"), "Ascending"),
        ),
        [
            categorical_filter(
                "reviewedOnly",
                "Exception",
                "current_disposition",
                ["ACKNOWLEDGED", "INVESTIGATING", "RESOLVED_EXTERNALLY"],
            )
        ],
    )
    p.add(
        "trace",
        856,
        502,
        400,
        202,
        table(
            [
                (column("Run", "run_id"), "Run"),
                (column("Run", "created_at_utc"), "Stored (UTC)"),
                (column("Run", "engine_version"), "Engine"),
                (column("Run", "rule_version"), "Rules"),
                (column("Run", "invoice_sha256"), "Invoice file SHA-256"),
                (column("Run", "payment_sha256"), "Payment file SHA-256"),
            ],
            "Traceability",
        ),
    )
    return p


THEME = {
    "name": "ReconcileKit",
    "dataColors": [
        "#2B6CB0",
        "#DD6B20",
        "#38A169",
        "#805AD5",
        "#D53F8C",
        "#718096",
        "#319795",
        "#B7791F",
    ],
    "foreground": "#1F2A37",
    "foregroundNeutralSecondary": "#52606D",
    "background": "#FFFFFF",
    "backgroundLight": "#F5F7FA",
    "tableAccent": "#2B6CB0",
    "good": "#38A169",
    "neutral": "#B7791F",
    "bad": "#C53030",
    "maximum": "#2B6CB0",
    "center": "#E2E8F0",
    "minimum": "#EDF2F7",
    "textClasses": {
        "label": {"fontFace": "Segoe UI", "fontSize": 10, "color": "#334E68"},
        "title": {"fontFace": "Segoe UI Semibold", "fontSize": 11, "color": "#1F2A37"},
        "callout": {"fontFace": "Segoe UI Semibold", "fontSize": 22, "color": "#1F2A37"},
    },
}


def report_files() -> dict[str, str]:
    theme_version = dict(VERSIONS)
    pages = [overview(), exceptions(), quality()]
    files = {
        "definition.pbir": _dump(
            {
                "$schema": f"{SCHEMA}/item/report/definitionProperties/2.0.0/schema.json",
                "version": "4.0",
                "datasetReference": {"byPath": {"path": "../RK.SemanticModel"}},
            }
        ),
        "definition/version.json": _dump(
            {
                "$schema": f"{SCHEMA}/item/report/definition/versionMetadata/1.0.0/schema.json",
                "version": "2.0.0",
            }
        ),
        "definition/report.json": _dump(
            {
                "$schema": REPORT_SCHEMA,
                "themeCollection": {
                    "baseTheme": {
                        "name": BASE_THEME,
                        "reportVersionAtImport": theme_version,
                        "type": "SharedResources",
                    },
                    "customTheme": {
                        "name": "ReconcileKit.json",
                        "reportVersionAtImport": theme_version,
                        "type": "RegisteredResources",
                    },
                },
                "resourcePackages": [
                    {
                        "name": "SharedResources",
                        "type": "SharedResources",
                        "items": [
                            {
                                "name": BASE_THEME,
                                "path": f"BaseThemes/{BASE_THEME}.json",
                                "type": "BaseTheme",
                            }
                        ],
                    },
                    {
                        "name": "RegisteredResources",
                        "type": "RegisteredResources",
                        "items": [
                            {
                                "name": "ReconcileKit.json",
                                "path": "ReconcileKit.json",
                                "type": "CustomTheme",
                            }
                        ],
                    },
                ],
                "settings": {
                    "useStylableVisualContainerHeader": True,
                    "allowChangeFilterTypes": True,
                },
            }
        ),
        "definition/pages/pages.json": _dump(
            {
                "$schema": f"{SCHEMA}/item/report/definition/pagesMetadata/1.1.0/schema.json",
                "pageOrder": [p.name for p in pages],
                "activePageName": pages[0].name,
            }
        ),
        "StaticResources/RegisteredResources/ReconcileKit.json": _dump(THEME),
    }
    for p in pages:
        files.update(p.files())
    return files


def project_files() -> dict[str, str]:
    files = {
        "RK.pbip": _dump(
            {
                "$schema": f"{SCHEMA}/pbip/pbipProperties/1.0.0/schema.json",
                "version": "1.0",
                "artifacts": [{"report": {"path": "RK.Report"}}],
                "settings": {"enableAutoRecovery": True},
            }
        )
    }
    files.update({f"RK.SemanticModel/{k}": v for k, v in semantic_model_files().items()})
    files.update({f"RK.Report/{k}": v for k, v in report_files().items()})
    return files


def write(root: Path) -> None:
    for rel, text in project_files().items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            text if text.endswith("\n") else text + "\n", encoding="utf-8", newline="\n"
        )


def _tracked(root: Path) -> set[str]:
    names = set()
    for item in PROJECT_FILES:
        target = root / item
        if target.is_file():
            names.add(item)
        elif target.is_dir():
            names |= {
                p.relative_to(root).as_posix()
                for p in target.rglob("*")
                if p.is_file() and ".pbi" not in p.relative_to(root).parts
            }
    return names


def stale_files() -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp)
        write(fresh)
        stale = []
        for name in sorted(_tracked(fresh) | _tracked(POWERBI)):
            a, b = fresh / name, POWERBI / name
            committed = b.read_text(encoding="utf-8").replace("\r\n", "\n") if b.exists() else None
            if not a.exists() or a.read_text(encoding="utf-8") != committed:
                stale.append(name)
        return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        stale = stale_files()
        for name in stale:
            print(f"stale: powerbi/{name}", file=sys.stderr)
        return 1 if stale else 0
    for item in (
        "RK.SemanticModel/definition",
        "RK.Report/definition",
        "RK.Report/StaticResources",
    ):
        if (POWERBI / item).exists():
            shutil.rmtree(POWERBI / item)
    write(POWERBI)
    print("wrote powerbi/RK.pbip, powerbi/RK.SemanticModel, powerbi/RK.Report")
    return 0


if __name__ == "__main__":
    sys.exit(main())
