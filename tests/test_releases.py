"""`releases twins` on a synthetic truth database: two loads of one invented sheet, one with trimmed cells and one
with a real difference. Nothing here touches real data."""

import json
import shutil
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

from nuggets.config import Config  # noqa: E402
from nuggets.releases import TYPED, twins  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "store"


def _truth(path: Path):
    con = duckdb.connect(str(path))
    con.execute("""CREATE TABLE releases (release_id VARCHAR, producer VARCHAR, pra_id VARCHAR, source_file VARCHAR, sheet VARCHAR,
                   audit VARCHAR, n_rows BIGINT, header VARCHAR[], container_sha256 VARCHAR, member_sha256 VARCHAR)""")
    con.execute("CREATE TABLE build_info (key VARCHAR, value VARCHAR)")
    con.execute("INSERT INTO build_info VALUES ('repo_commit', 'abc'), ('repo_web_url', 'https://example.invalid/x/example-repo')")
    cols = ", ".join(f'"{c}" VARCHAR' for c in TYPED)
    con.execute(f"CREATE TABLE flock_audit_rows (release_id VARCHAR, row_no BIGINT, src_row BIGINT, {cols}, extra JSON)")
    hdr = ["Name", "Org Name", "Reason"]
    for rid, path in (("xx:1-1:Example_Audit__MARCH", "xx"), ("pp:portal:1-1:Example Audit.xlsx#MARCH", "pp"),
                      ("xx:1-1:Example_Audit__APRIL", "xx"), ("pp:portal:1-1:Example Audit.xlsx#APRIL", "pp")):
        sheet = "MARCH" if "MARCH" in rid else "APRIL"
        con.execute("INSERT INTO releases VALUES (?, 'Example PD', ?, 'Example Audit.xlsx', ?, 'network', 2, ?, 'h' || ?, NULL)",
                    [rid, "portal:1-1" if path == "pp" else "1-1", sheet, hdr, rid])
    vals = lambda n, o, r: [None, n, o, None, None, None, None, r] + [None] * 6  # noqa: E731  (ID, Name, Org Name, …, Reason)
    con.execute("INSERT INTO flock_audit_rows VALUES (?, 1, 2, " + ", ".join("?" * 14) + ", NULL)", ["xx:1-1:Example_Audit__MARCH", *vals("A", "Example PD", "case 1")])
    con.execute("INSERT INTO flock_audit_rows VALUES (?, 2, 3, " + ", ".join("?" * 14) + ", NULL)", ["xx:1-1:Example_Audit__MARCH", *vals("B", "Example PD", None)])
    con.execute("INSERT INTO flock_audit_rows VALUES (?, 1, 2, " + ", ".join("?" * 14) + ", NULL)", ["pp:portal:1-1:Example Audit.xlsx#MARCH", *vals("A ", "Example PD", "case 1")])
    con.execute("INSERT INTO flock_audit_rows VALUES (?, 2, 3, " + ", ".join("?" * 14) + ", NULL)", ["pp:portal:1-1:Example Audit.xlsx#MARCH", *vals("B", "Example PD", "")])
    con.execute("INSERT INTO flock_audit_rows VALUES (?, 1, 2, " + ", ".join("?" * 14) + ", NULL)", ["xx:1-1:Example_Audit__APRIL", *vals("C", "Example PD", "case 2")])
    con.execute("INSERT INTO flock_audit_rows VALUES (?, 2, 3, " + ", ".join("?" * 14) + ", NULL)", ["xx:1-1:Example_Audit__APRIL", *vals("D", "Example PD", "case 3")])
    con.execute("INSERT INTO flock_audit_rows VALUES (?, 1, 2, " + ", ".join("?" * 14) + ", NULL)", ["pp:portal:1-1:Example Audit.xlsx#APRIL", *vals("C", "Example PD", "case 2")])
    con.execute("INSERT INTO flock_audit_rows VALUES (?, 2, 3, " + ", ".join("?" * 14) + ", NULL)", ["pp:portal:1-1:Example Audit.xlsx#APRIL", *vals("D", "Example PD", "case 9")])
    con.close()


def test_twins_verdicts(tmp_path, monkeypatch):
    shutil.copytree(FIXTURE, tmp_path / "store")
    audit = tmp_path / "audit"
    audit.mkdir()
    _truth(audit / "truth.duckdb")
    cfg = Config.load(tmp_path / "store")
    cfg.audit_db = audit
    assert twins(cfg, "%Example%") == 0
    out = json.loads((cfg.build_dir / "releases" / "twins.json").read_text())
    by_sheet = {("MARCH" if "MARCH" in r["a"] else "APRIL"): r for r in out["results"]}
    assert out["pairs"] == 2
    assert by_sheet["MARCH"]["verdict"] == "trimmed" and by_sheet["MARCH"]["trimmed_only"] == {"Name": 1, "Reason": 1}
    assert by_sheet["APRIL"]["verdict"] == "differs" and by_sheet["APRIL"]["residual_after_trim"] == {"Reason": 1}
    assert (cfg.build_dir / "releases" / "twins.md").exists()
