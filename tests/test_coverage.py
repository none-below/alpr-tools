"""`coverage` on a synthetic truth database: one invented agency with a full month, a partial month, a claimed but
empty month and a hole, and the window parsers. Nothing here touches real data."""

import csv
import shutil
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

from nuggets.config import Config  # noqa: E402
from nuggets.coverage import build_coverage, claimed_months, coverage_lines  # noqa: E402
from nuggets.releases import TYPED  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "store"


def test_claimed_months():
    assert claimed_months("x/1_1_2026-1_31_2026-Example_PD-Audit.pdf", None, None, None) == ["2026-01"]
    assert claimed_months(None, "REDACTED_11_1_2025-1_31_2026-Example.xlsx", None, None) == ["2025-11", "2025-12", "2026-01"]
    assert claimed_months("a/Example Audit 2024.xlsx", None, "Example Audit 2024.xlsx", "APRIL") == ["2024-04"]
    assert claimed_months("a/Example Audit through July 6 2026.xlsx", None, None, "May") == ["2026-05"]
    assert claimed_months("a/Example.xlsx", None, None, "Sheet1") == []


def _ts(day, month=1, year=2026, hour=10):
    return f"{month:02d}/{day:02d}/{year}, {hour:02d}:00:00 AM UTC"


def _truth(path: Path):
    con = duckdb.connect(str(path))
    con.execute("""CREATE TABLE releases (release_id VARCHAR, producer VARCHAR, producer_agency_id VARCHAR, audit VARCHAR,
                   container_path VARCHAR, member VARCHAR, source_file VARCHAR, sheet VARCHAR, n_rows BIGINT,
                   container_sha256 VARCHAR, member_sha256 VARCHAR)""")
    con.execute("CREATE TABLE build_info (key VARCHAR, value VARCHAR)")
    con.execute("INSERT INTO build_info VALUES ('repo_commit', 'abc'), ('repo_web_url', 'https://example.invalid/x/example-repo')")
    cols = ", ".join(f'"{c}" VARCHAR' for c in TYPED)
    con.execute(f"CREATE TABLE flock_audit_rows (release_id VARCHAR, row_no BIGINT, src_row BIGINT, {cols}, extra JSON)")
    rels = [("pp:jan", "x/1_1_2026-1_31_2026-Example_PD-Audit.xlsx"), ("pp:feb", "x/2_1_2026-2_28_2026-Example_PD-Audit.xlsx"),
            ("pp:mar", "x/3_1_2026-3_31_2026-Example_PD-Audit.xlsx"), ("pp:may", "x/5_1_2026-5_31_2026-Example_PD-Audit.xlsx"),
            ("pp:jan2", "x/1_1_2026-1_15_2026-Example_PD-Audit-again.xlsx")]
    for rid, path_ in rels:
        con.execute("INSERT INTO releases VALUES (?, 'Example PD', 'agency-1', 'own', ?, NULL, NULL, NULL, 0, 'h', NULL)", [rid, path_])
    idx = {c: i for i, c in enumerate(TYPED)}

    def row(rid, n, ts, sid):
        vals = [None] * len(TYPED)
        vals[idx["ID"]], vals[idx["Search Time"]] = sid, ts
        con.execute("INSERT INTO flock_audit_rows VALUES (?, ?, ?, " + ", ".join("?" * len(TYPED)) + ", NULL)", [rid, n, n + 1, *vals])
    for d in range(1, 32):
        row("pp:jan", d, _ts(d), f"jan-{d}")
    for d in range(1, 16):
        row("pp:jan2", d, _ts(d), f"jan-{d}")        # a re-release of the first half: same IDs
    for d in range(1, 11):
        row("pp:feb", d, _ts(d, 2), f"feb-{d}")      # partial: stops on the 10th
    # pp:mar claims March and holds nothing; April has no release at all; May full
    for d in range(1, 32):
        row("pp:may", d, _ts(d, 5), f"may-{d}")
    con.close()


def test_coverage_marks(tmp_path):
    shutil.copytree(FIXTURE, tmp_path / "store")
    audit = tmp_path / "audit"
    audit.mkdir()
    _truth(audit / "truth.duckdb")
    cfg = Config.load(tmp_path / "store")
    cfg.audit_db = audit
    assert build_coverage(cfg, "%Example%") == 0
    rows = {r["month"]: r for r in csv.DictReader((cfg.build_dir / "coverage" / "coverage.csv").open())}
    assert rows["2026-01"]["mark"] == "full" and rows["2026-01"]["releases"] == "2" and rows["2026-01"]["dup_rows"] == "15"
    assert rows["2026-02"]["mark"] == "partial"
    assert rows["2026-03"]["mark"] == "empty" and "pp:mar" in rows["2026-03"]["claimed_by"]
    assert rows["2026-04"]["mark"] == "hole"
    assert rows["2026-05"]["mark"] == "full"
    md = (cfg.build_dir / "coverage" / "coverage.md").read_text()
    assert "| 2026 | F+2 | p | 0 | . | F |" in md
    lines = coverage_lines(cfg, "%Example%", "own", ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05"])
    assert lines[0].startswith("coverage (%Example%, own): 3 of 5 months hold rows")
    assert any(ln.startswith("holes: 2026-04") for ln in lines)
    assert any("2026-01 ×2" in ln for ln in lines)
