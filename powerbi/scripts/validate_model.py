"""Validate the semantic model running in Power BI Desktop against the expected results.

Connects to the Analysis Services engine that Power BI Desktop starts on localhost, then:

1. checks every table's row count against its Parquet file,
2. checks that money columns are Fixed decimal (TOM DataType.Decimal) in the engine,
3. runs model/measures.dax unmodified (DEFINE block + all EVALUATE queries) through ADOMD.NET,
4. compares every KPI, control, and customer balance with dataset/expected_kpis.json,

and writes the outcome to powerbi/validation/desktop-validation.md.

Requirements (developer tooling, not ReconcileKit dependencies): Windows, Power BI Desktop with
RK.pbip open and refreshed, ``pip install pythonnet duckdb``, and a folder containing
Microsoft.AnalysisServices.AdomdClient.dll and the TOM DLLs (Microsoft.AnalysisServices.Tabular,
.Core, Microsoft.AnalysisServices) from Microsoft's NuGet packages
``Microsoft.AnalysisServices.AdomdClient.retail.amd64`` and
``Microsoft.AnalysisServices.retail.amd64`` (lib/net45).

    python powerbi/scripts/validate_model.py --dll-dir C:\\tools\\as-client [--refresh]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb

POWERBI = Path(__file__).resolve().parents[1]
OUT = POWERBI / "validation" / "desktop-validation.md"
PBI_EXE = r"C:\Program Files\Microsoft Power BI Desktop\bin\PBIDesktop.exe"
TABLE_FILES = {
    "Run": "run",
    "Open Invoice": "open_invoice",
    "Payment Exception": "payment_exception",
    "Exception": "exception",
    "Customer": "customer",
    "Bridge Step": "bridge_step",
}
KPI_COLUMNS = {  # EVALUATE 1 column -> expected_kpis.json key
    "Eligible Invoice Value": "eligible_invoice_value",
    "Settled Invoice Value": "settled_invoice_value",
    "Outstanding Balance": "outstanding_balance",
    "Overpayments": "overpayments",
    "Net Difference": "net_difference",
    "Quarantined Rows": "quarantined_rows",
    "Arithmetic Status": "arithmetic_status",
    "Data Completeness": "data_completeness",
    "Payment Exception Count": "payment_exception_count",
    "Payment Exception Amount": "payment_exception_amount",
    "Exception Count": "exception_count",
    "Unresolved Exceptions": "unresolved_exceptions",
    "Validation Exceptions": "validation_exceptions",
    "Open Invoice Count": "open_invoice_count",
}


def find_port() -> int:
    root = (
        Path(os.environ["LOCALAPPDATA"])
        / "Microsoft"
        / "Power BI Desktop"
        / "AnalysisServicesWorkspaces"
    )
    files = sorted(
        root.glob("*/Data/msmdsrv.port.txt"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not files:
        raise SystemExit("No running Power BI Desktop engine found. Open powerbi/RK.pbip first.")
    # Desktop writes the port as UTF-16 LE without a byte-order mark.
    return int(files[0].read_bytes().decode("utf-16-le").strip("﻿\x00 \r\n"))


def load_clients(dll_dir: str):
    from pythonnet import load

    load("netfx")
    import clr

    sys.path.append(dll_dir)
    for name in (
        "Microsoft.AnalysisServices.Core",
        "Microsoft.AnalysisServices",
        "Microsoft.AnalysisServices.Tabular",
        "Microsoft.AnalysisServices.AdomdClient",
    ):
        clr.AddReference(name)
    import Microsoft.AnalysisServices.AdomdClient as adomd
    import Microsoft.AnalysisServices.Tabular as tom
    from System.Globalization import CultureInfo

    return tom, adomd, CultureInfo.InvariantCulture


def to_py(value, invariant):
    """Convert a .NET value without passing decimals through floating point."""
    if value is None:
        return None
    name = value.GetType().FullName if hasattr(value, "GetType") else ""
    if name == "System.Decimal":
        return Decimal(value.ToString(invariant))
    if name == "System.DBNull":
        return None
    return value


def run_dax(adomd, invariant, conn_str: str, query: str) -> list[tuple[list[str], list[tuple]]]:
    conn = adomd.AdomdConnection(conn_str)
    conn.Open()
    try:
        cmd = conn.CreateCommand()
        cmd.CommandText = query
        reader = cmd.ExecuteReader()
        results = []
        while True:
            cols = [reader.GetName(i) for i in range(reader.FieldCount)]
            rows = []
            while reader.Read():
                rows.append(
                    tuple(to_py(reader.GetValue(i), invariant) for i in range(reader.FieldCount))
                )
            results.append((cols, rows))
            if not reader.NextResult():
                break
        reader.Close()
        return results
    finally:
        conn.Close()


def input_hashes() -> dict[str, str]:
    """SHA-256 of everything the validation depends on (line endings normalized)."""
    groups = {
        "model/measures.dax": [POWERBI / "model" / "measures.dax"],
        "dataset/expected_kpis.json": [POWERBI / "dataset" / "expected_kpis.json"],
        "dataset/model/*.parquet": sorted((POWERBI / "dataset" / "model").glob("*.parquet")),
        "RK.SemanticModel/definition/**/*.tmdl": sorted(
            (POWERBI / "RK.SemanticModel" / "definition").rglob("*.tmdl")
        ),
    }
    out = {}
    for name, files in groups.items():
        digest = hashlib.sha256()
        for f in files:
            data = f.read_bytes()
            if f.suffix != ".parquet":
                data = data.replace(b"\r\n", b"\n")
            digest.update(f.relative_to(POWERBI).as_posix().encode() + b"\0" + data)
        out[name] = digest.hexdigest()
    return out


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return f"{value:.2f}"  # Fixed decimal values are exact; show cents
    return str(value)


def short(col: str) -> str:
    """'Run'[run_id] -> run_id ; [Eligible Invoice Value] -> Eligible Invoice Value."""
    m = re.search(r"\[([^\]]+)\]$", col)
    return m.group(1) if m else col


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dll-dir", required=True)
    parser.add_argument("--port", type=int)
    parser.add_argument("--refresh", action="store_true", help="Full refresh before validating")
    args = parser.parse_args(argv)
    tom, adomd, invariant = load_clients(args.dll_dir)
    port = args.port or find_port()

    server = tom.Server()
    server.Connect(f"Data Source=localhost:{port}")
    db = server.Databases[0]
    model = db.Model
    if args.refresh:
        model.RequestRefresh(tom.RefreshType.Full)
        model.SaveChanges()
    checks: list[tuple[str, bool, str]] = []

    con = duckdb.connect()
    for table, file in TABLE_FILES.items():
        path = (POWERBI / "dataset" / "model" / f"{file}.parquet").as_posix()
        expected = con.execute(f"SELECT count(*) FROM read_parquet('{path}')").fetchone()[0]
        t = model.Tables.Find(table)
        states = {str(p.State) for p in t.Partitions}
        checks.append(
            (f"{table}: partitions loaded", states == {"Ready"}, ", ".join(sorted(states)))
        )
        cols = run_dax(
            adomd,
            invariant,
            f"Data Source=localhost:{port};Catalog={db.ID}",
            f"EVALUATE ROW(\"n\", COUNTROWS('{table}'))",
        )[0]
        actual = cols[1][0][0]
        checks.append(
            (f"{table}: row count", actual == expected, f"{actual} (Parquet: {expected})")
        )
        for c in t.Columns:
            if c.Name.endswith(("_usd", "_pct")):
                ok = str(c.DataType) == "Decimal"
                checks.append((f"{table}[{c.Name}] is Fixed decimal", ok, str(c.DataType)))
    con.close()

    conn_str = f"Data Source=localhost:{port};Catalog={db.ID}"
    dax = (POWERBI / "model" / "measures.dax").read_text(encoding="utf-8")
    results = run_dax(adomd, invariant, conn_str, dax)
    expected = {
        r["run_id"]: r
        for r in json.loads(
            (POWERBI / "dataset" / "expected_kpis.json").read_text(encoding="utf-8")
        )["runs"]
    }

    kpi_cols, kpi_rows = results[0]
    kpi_names = [short(c) for c in kpi_cols]
    for row in kpi_rows:
        rec = dict(zip(kpi_names, row, strict=True))
        exp = expected[rec["run_id"]]
        for col, key in KPI_COLUMNS.items():
            got, want = rec[col], exp[key]
            if isinstance(got, Decimal):
                ok = got == Decimal(want)
            elif isinstance(want, int):
                ok = int(got) == want
            else:
                ok = str(got) == str(want)
            checks.append((f"{rec['run_id']} {col}", ok, f"DAX {got} | expected {want}"))
        pct = Decimal(str(round(float(rec["Settled %"]) * 100, 2))).quantize(Decimal("0.01"))
        checks.append(
            (
                f"{rec['run_id']} Settled %",
                pct == Decimal(exp["settled_pct"]),
                f"DAX {rec['Settled %']!r} -> {pct}% | expected {exp['settled_pct']}%",
            )
        )

    ctl_cols, ctl_rows = results[1]
    ctl_names = [short(c) for c in ctl_cols]
    for row in ctl_rows:
        rec = dict(zip(ctl_names, row, strict=True))
        for name, value in rec.items():
            if name == "run_id":
                continue
            ok = value == 5 if name == "Controls passed" else value is True
            checks.append((f"{rec['run_id']} control: {name}", ok, str(value)))

    _, cust_rows = results[2]
    got_customers: dict[str, dict[str, str]] = {}
    for run_id, cust, amount in cust_rows:
        got_customers.setdefault(run_id, {})[cust] = f"{amount:.2f}"
    for run_id, exp in expected.items():
        ok = got_customers.get(run_id) == exp["customer_outstanding"]
        checks.append(
            (
                f"{run_id} customer outstanding",
                ok,
                f"DAX {got_customers.get(run_id)} | expected {exp['customer_outstanding']}",
            )
        )
    server.Disconnect()

    passed = sum(ok for _, ok, _ in checks)
    probe = f"(Get-Item '{PBI_EXE}').VersionInfo.ProductVersion"
    version = (
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", probe],
            capture_output=True,
            text=True,
            check=False,
        )
        .stdout.strip()
        .split(" ")[0]
    )
    lines = [
        "# Power BI Desktop validation",
        "",
        "Generated by `powerbi/scripts/validate_model.py` against the model loaded in Power BI",
        f"Desktop {version or '(version unknown)'} from `RK.pbip`,",
        "refreshed from `dataset/model/`.",
        f"Date: {date.today().isoformat()}. DAX was executed by the Desktop "
        "Analysis Services engine",
        "through ADOMD.NET; `model/measures.dax` was run unmodified.",
        "",
        "Inputs (SHA-256; tests/test_powerbi_pbip.py flags this report as stale if they change):",
        "",
        *[f"- `{name}`: `{digest}`" for name, digest in input_hashes().items()],
        "",
        f"**Result: {passed} of {len(checks)} checks passed.**",
        "",
        "| Check | Result | Detail |",
        "|---|---|---|",
        *[
            f"| {name} | {'PASS' if ok else 'FAIL'} | {detail.replace('|', '/')} |"
            for name, ok, detail in checks
        ],
        "",
        "## Query results",
        "",
    ]
    for i, (cols, rows) in enumerate(results, start=1):
        names = [short(c) for c in cols]
        lines += [
            f"### EVALUATE {i}",
            "",
            "| " + " | ".join(names) + " |",
            "|" + "---|" * len(names),
        ]
        lines += ["| " + " | ".join(_cell(v) for v in r) + " |" for r in rows]
        lines.append("")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"{passed}/{len(checks)} checks passed; wrote {OUT.relative_to(POWERBI.parent)}")
    for name, ok, detail in checks:
        if not ok:
            print(f"FAIL {name}: {detail}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
