"""The lint on the synthetic fixture store, and the locator grammar. Nothing here touches real data."""

import shutil
from pathlib import Path

import pytest

from nuggets import ids
from nuggets.config import Config
from nuggets.lint import run_lint
from nuggets.store import Term, check_locator_values, load_store, parse_locator

FIXTURE = Path(__file__).parent / "fixtures" / "store"
KINDS = {"file", "sheet", "page", "pages", "printed", "line", "row", "rows", "cell", "section", "subdivision",
         "time", "field", "item", "message"}


def lint(store: Path):
    return run_lint(load_store(Config.load(store)))


def errors(findings):
    return [str(f) for f in findings if f.level == "error"]


def test_fixture_store_is_clean():
    assert errors(lint(FIXTURE)) == []


def test_ids_derive():
    assert ids.node_id("ab" * 32) == ids.u5("sha256:" + "ab" * 32)
    r = {"kind": "pra", "agency": "A", "ref": "R1"}
    assert ids.root_id(r) == ids.u5("ref:pra:A/R1")
    assert ids.message_id(r, 3) == ids.u5("ref:pra:A/R1/msg/3")
    assert ids.canonical_url("HTTPS://Example.COM/a?x=1&utm_source=t#frag") == "https://example.com/a?x=1"


def test_locator_grammar():
    terms = parse_locator('file("PRA 1, 2 (final).xlsx"), sheet(Sheet1), rows(1;5;9), time(2:22.4-2:47.9)')
    assert [t.kind for t in terms] == ["file", "sheet", "rows", "time"]
    assert terms[0].value == "PRA 1, 2 (final).xlsx"
    assert check_locator_values(terms, KINDS) == []
    assert str(Term("file", "a, b")) == 'file("a, b")'
    with pytest.raises(ValueError):
        parse_locator("rows(1, 2)")          # a list uses ; not ,
    with pytest.raises(ValueError):
        parse_locator("page(3) row(2)")     # terms are comma-separated
    assert check_locator_values(parse_locator("page(x)"), KINDS) == ["page(x) is not an integer"]
    assert check_locator_values(parse_locator("bogus(1)"), KINDS) == ["unknown locator kind bogus()"]


def test_broken_fact_fails(tmp_path):
    shutil.copytree(FIXTURE, tmp_path / "store")
    f = next((tmp_path / "store" / "facts").rglob("*.toml"))
    t = f.read_text()
    t = t.replace('status = "verified"', 'status = "verified"\nresolved_by = ""')          # "" as unknown
    t = t.replace('how = { export = "native:', 'how = { export = "sidecar:')             # finding step on verified
    t = t.replace("rows(1-10)", "rows(1-10)")
    f.write_text(t)
    e = errors(lint(tmp_path / "store"))
    assert any('= ""' in m for m in e)
    assert any("finding step" in m for m in e)


def test_hand_side_rules(tmp_path):
    shutil.copytree(FIXTURE, tmp_path / "store")
    h = next((tmp_path / "store" / "sources").glob("*.hand.toml"))
    h.write_text(h.read_text() + '\n[[files]]\nname = "by-name-only.pdf"\nnote = "no hash"\n')
    e = errors(lint(tmp_path / "store"))
    assert any("without sha256" in m for m in e)


def test_root_id_drift(tmp_path):
    shutil.copytree(FIXTURE, tmp_path / "store")
    g = next((tmp_path / "store" / "sources" / "gen").glob("*.toml"))
    g.write_text(g.read_text().replace('ref    = "R000001-010126"', 'ref    = "R000002-010126"'))
    e = errors(lint(tmp_path / "store"))
    assert any("does not derive from seed" in m for m in e)
