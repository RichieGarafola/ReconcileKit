# Third-party dependencies

ReconcileKit installs these packages directly. Each keeps its own license, and transitive
dependencies bring theirs. The license names below are given for orientation only;
confirm them against each project's current distribution before redistributing.

| Package | Purpose | License (as published by the project) |
|---|---|---|
| pandas | Tabular ingestion and reporting frames | BSD-3-Clause |
| numpy | Array support used by pandas and exports | BSD-3-Clause |
| pandera | Declarative dataframe validation | MIT |
| duckdb | Embedded database and SQL reporting | MIT |
| streamlit | Web interface | Apache-2.0 |
| pytest (dev) | Tests | MIT |
| ruff (dev) | Linting and formatting | MIT |

Exact tested versions are listed in `requirements-lock.txt`.

ReconcileKit itself is licensed under the Apache License, Version 2.0 (see `LICENSE` in the
repository root).
