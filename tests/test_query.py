"""`query` on the coverage test's synthetic truth plus a two-table derived: every `sql_*` key runs as a block, a
`--param` override lands in the output stem and the summary, a query that reads `derived.` carries the linking build
in its pin and refuses to run without derived, and a release whose second row count disagrees is flagged. Nothing
here is real data."""

import csv
import shutil
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

from nuggets.build import build  # noqa: E402
from nuggets.config import Config  # noqa: E402
from nuggets.query import _slug, run_query  # noqa: E402
from test_coverage import _truth  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "store"

QUERY = '''
name = "example-count"
schema_version = 1
definition = "rows per release in the synthetic truth, and distinct ids through a derived view"
unit = "rows"
sql = "SELECT release_id, count(*) AS n FROM truth.flock_audit_rows WHERE release_id LIKE $prefix GROUP BY 1 ORDER BY 1"
sql_ids = "SELECT count(DISTINCT id) AS ids FROM derived.ids WHERE release_id LIKE $prefix"
releases_sql = "SELECT release_id, container_sha256, n_rows FROM truth.releases WHERE release_id LIKE $prefix ORDER BY 1"
[params]
prefix = "pp:jan%"
'''


def _derived(audit: Path):
    con = duckdb.connect(str(audit / "derived.duckdb"))
    con.execute(f"ATTACH '{audit / 'truth.duckdb'}' AS truth (READ_ONLY)")
    con.execute("CREATE SCHEMA cache")
    con.execute("CREATE TABLE cache.builds (name VARCHAR, built_at TIMESTAMP, truth_fingerprint VARCHAR, code_sha VARCHAR, duckdb_version VARCHAR)")
    con.execute("INSERT INTO cache.builds VALUES ('sighting_event', TIMESTAMP '2026-01-02 03:04:05', 'fp1', 'code1', 'v0')")
    con.execute('CREATE VIEW ids AS SELECT release_id, "ID" AS id FROM truth.flock_audit_rows')
    con.close()


def _setup(tmp_path: Path) -> Config:
    shutil.copytree(FIXTURE, tmp_path / "store")
    audit = tmp_path / "audit"
    audit.mkdir()
    _truth(audit / "truth.duckdb")
    con = duckdb.connect(str(audit / "truth.duckdb"))
    con.execute("ALTER TABLE releases ADD COLUMN count_mismatch_at VARCHAR")
    con.execute("UPDATE releases SET count_mismatch_at = 'totals 10 vs 11' WHERE release_id = 'pp:feb'")
    con.close()
    _derived(audit)
    cfg = Config.load(tmp_path / "store")
    cfg.audit_db = audit
    (cfg.store / "queries").mkdir()
    (cfg.store / "queries" / "example-count.toml").write_text(QUERY)
    assert build(cfg) == 0
    return cfg


def test_slug():
    assert _slug("Pacifica CA PD") == "pacifica-ca-pd" and _slug("pp:feb%") == "pp-feb" and _slug("***") == "x"


def test_blocks_params_and_pins(tmp_path, capsys):
    cfg = _setup(tmp_path)
    assert run_query(cfg, "example-count") == 0
    out = cfg.build_dir / "queries"
    rows = list(csv.DictReader((out / "example-count.csv").open()))
    assert [(r["release_id"], r["n"]) for r in rows] == [("pp:jan", "31"), ("pp:jan2", "15")]
    assert list(csv.DictReader((out / "example-count.ids.csv").open()))[0]["ids"] == "31"
    md = (out / "example-count.summary.md").read_text()
    assert "params: prefix = 'pp:jan%'" in md and "(--param)" not in md
    assert "derived = linking code1 built 2026-01-02 03:04:05 over truth md5:fp1 (duckdb v0)" in md
    assert "## Ids (1 rows; example-count.ids.csv)" in md
    assert "## Releases this count rests on (2)" in md and "flagged: none" in md
    # a --param override: its own stem, the override marked, the flagged release named
    assert run_query(cfg, "example-count", overrides={"prefix": "pp:feb%"}) == 0
    assert "query example-count--prefix=pp-feb:" in capsys.readouterr().out
    md2 = (out / "example-count--prefix=pp-feb.summary.md").read_text()
    assert "params: prefix = 'pp:feb%' (--param)" in md2
    assert "flagged: 1 of these releases" in md2 and "- pp:feb: diverges at totals 10 vs 11" in md2
    assert (out / "example-count.summary.md").exists()   # the default run was not overwritten


def test_derived_required_when_read(tmp_path, capsys):
    cfg = _setup(tmp_path)
    (cfg.audit_db / "derived.duckdb").unlink()
    assert run_query(cfg, "example-count") == 2
    assert "reads derived.*, and no derived.duckdb is attached" in capsys.readouterr().out
