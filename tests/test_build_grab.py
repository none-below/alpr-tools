"""`build` and `grab` on the synthetic fixture store. Nothing here touches real data."""

import json
import shutil
from pathlib import Path

from nuggets.build import build
from nuggets.config import Config
from nuggets.grab import grab

FIXTURE = Path(__file__).parent / "fixtures" / "store"


def test_build_writes_tables(tmp_path):
    shutil.copytree(FIXTURE, tmp_path / "store")
    cfg = Config.load(tmp_path / "store")
    assert build(cfg) == 0
    facts = json.loads((cfg.build_dir / "facts.json").read_text())["facts"]
    assert [f["id"] for f in facts] == ["example/export-row-count-2026-01-02"]
    assert facts[0]["content_sha256"] and facts[0]["sources"][0]["alias"] == "export"
    pub = json.loads((cfg.build_dir / "for-public" / "facts.json").read_text())
    assert pub["audience"] == "public"
    try:
        import duckdb
    except ImportError:
        return
    con = duckdb.connect(str(cfg.build_dir / "facts.duckdb"), read_only=True)
    assert con.execute("select count(*) from facts").fetchone()[0] == 1
    assert con.execute("select count(*) from fact_sources").fetchone()[0] == 1
    assert con.execute("select count(*) from contracts").fetchone()[0] == 0


def test_grab_prints_full_hash(tmp_path, capsys):
    shutil.copytree(FIXTURE, tmp_path / "store")
    cfg = Config.load(tmp_path / "store")
    assert grab(cfg, "example/export-row-count-2026-01-02") == 0
    out = capsys.readouterr().out
    assert "Source [export]:" in out
    assert "sha256 " + "0" * 0 in out
    line = next(ln for ln in out.splitlines() if ln.startswith("Source [export]:"))
    assert len([t for t in line.replace(",", " ").split() if len(t) == 64]) >= 1   # the full hash, never a prefix
    assert "proof:" in out


def test_grab_refuses_draft_for_public(tmp_path, capsys):
    shutil.copytree(FIXTURE, tmp_path / "store")
    f = next((tmp_path / "store" / "facts").rglob("*.toml"))
    f.write_text(f.read_text().replace('status = "verified"', 'status = "draft"'))
    cfg = Config.load(tmp_path / "store")
    assert grab(cfg, "example/export-row-count-2026-01-02", audience="public") == 1
    assert capsys.readouterr().out.startswith("refused:")
