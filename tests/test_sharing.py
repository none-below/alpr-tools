"""`sharing` on synthetic portal snapshots: an invented agency whose outbound list shows one partner, then two, then
no section at all, then one partner plus a name the registry lacks, then a declared none. Checks the spans and their
brackets, the no-observation rule, local capture dates and the unresolved-name id. Nothing here is a real agency."""

import json
import shutil
from pathlib import Path

from nuggets import ids
from nuggets.config import Config
from nuggets.sharing import build_sharing, observe, read_snapshots, resolve_agency, spans_from

FIXTURE = Path(__file__).parent / "fixtures" / "store"

REG = [
    {"agency_id": "agency-1", "slug": "example-pd", "flock_names": ["Example PD"]},
    {"agency_id": "agency-2", "slug": "partner-a-pd", "flock_names": ["Partner A PD"]},
    {"agency_id": "agency-3", "slug": "partner-b-so", "flock_names": ["Partner B SO"]},
]


def _snap(crawled, outbound=None, **flags):
    s = {"crawled_at": crawled, "sharing_outbound": outbound or [], "sharing_inbound": []}
    s.update(flags)
    return s


def _site(tmp_path: Path) -> Path:
    site = tmp_path / "site"
    (site / "assets").mkdir(parents=True)
    (site / "assets" / "agency_registry.json").write_text(json.dumps(REG))
    d = site / "assets" / "transparency.flocksafety.com" / "example-pd"
    d.mkdir(parents=True)
    snaps = {
        "2026-01-01": _snap("2026-01-01T22:00:00+00:00", ["Partner A PD"]),
        "2026-01-08": _snap("2026-01-08T05:30:00+00:00", ["Partner A PD", "Partner B SO"]),   # Jan 7 in Los Angeles
        "2026-01-15": _snap("2026-01-15T22:00:00+00:00", [], sharing_outbound_section_present=False),
        "2026-01-22": _snap("2026-01-22T22:00:00+00:00", ["Partner B SO", "Mystery Org"], sharing_outbound_section_present=True),
        "2026-01-29": _snap("2026-01-29T22:00:00+00:00", [], sharing_outbound_declared_none=True, sharing_outbound_section_present=True),
    }
    for k, v in snaps.items():
        (d / f"{k}.json").write_text(json.dumps(v))
    (d / "2026-01-01.pdf").write_bytes(b"%PDF-")   # a sibling capture, not a snapshot file
    return site


def test_observe_rules():
    assert observe({"sharing_outbound": ["A", " B "]}, "outbound") == ({"A", "B"}, "listed")
    assert observe({"sharing_outbound": [{"name": "A"}]}, "outbound") == ({"A"}, "listed")
    assert observe({"sharing_outbound": [], "sharing_outbound_declared_none": True}, "outbound") == (set(), "declared none")
    assert observe({"sharing_outbound": [], "sharing_outbound_section_present": True}, "outbound") == (set(), "section shown, empty")
    assert observe({"sharing_outbound": [], "sharing_outbound_section_present": False}, "outbound")[0] is None
    assert observe({"sharing_outbound": []}, "outbound")[0] is None
    assert observe({}, "inbound")[0] is None


def test_spans_and_brackets(tmp_path):
    site = _site(tmp_path)
    snaps = read_snapshots(site / "assets" / "transparency.flocksafety.com" / "example-pd", "America/Los_Angeles")
    assert [s["seen"] for s in snaps] == ["2026-01-01", "2026-01-07", "2026-01-15", "2026-01-22", "2026-01-29"]
    spans, last = spans_from(snaps, "outbound")
    assert last == "2026-01-29"
    by = {sp["partner"]: sp for sp in spans}
    a, b, m = by["Partner A PD"], by["Partner B SO"], by["Mystery Org"]
    # A: present on the first two observations; the 01-15 capture observed nothing, so its end is bracketed by 01-22
    assert (a["first_seen"], a["last_seen"], a["absent_before"], a["absent_after"], a["n_snapshots"], a["status"]) == \
        ("2026-01-01", "2026-01-07", None, "2026-01-22", 2, "ended")
    assert (b["first_seen"], b["last_seen"], b["absent_before"], b["absent_after"], b["status"]) == \
        ("2026-01-07", "2026-01-22", "2026-01-01", "2026-01-29", "ended")
    assert (m["first_seen"], m["absent_before"], m["absent_after"]) == ("2026-01-22", "2026-01-07", "2026-01-29")
    assert spans_from(snaps, "inbound") == ([], None)


def test_resolve_and_build(tmp_path, capsys):
    shutil.copytree(FIXTURE, tmp_path / "store")
    site = _site(tmp_path)
    cfg = Config.load(tmp_path / "store")
    cfg.site_repo = site
    assert resolve_agency(REG, {}, "example-pd")["agency_id"] == "agency-1"
    assert resolve_agency(REG, {}, "example pd")["agency_id"] == "agency-1"
    assert resolve_agency(REG, {"EX": "agency-1"}, "EX")["slug"] == "example-pd"
    assert resolve_agency(REG, {}, "nobody") is None
    assert build_sharing(cfg, "nobody") == 2
    assert build_sharing(cfg, "Example PD") == 0
    out = capsys.readouterr().out
    assert "spans outbound 3 / inbound 0, 1 unresolved names" in out
    md = (cfg.build_dir / "by-agency" / "example-pd.sharing.md").read_text()
    assert "## Outbound spans (3; open 0)" in md
    assert "| Partner A PD | agency-2 | <=2026-01-01 | >=2026-01-07, <2026-01-22 | 2 | ended | snapshot |" in md
    assert "| Partner B SO | agency-3 | >2026-01-01, <=2026-01-07 | >=2026-01-22, <2026-01-29 | 2 | ended | snapshot |" in md
    assert f"| Mystery Org | {ids.u5('flock-name:Mystery Org')} |" in md and "name not in registry" in md
    assert "- inbound: none observed (empty list; the parse did not record whether the section was shown)" in md
    assert "- 2026-01-15 outbound: section absent from the page" in md
    j = json.loads((cfg.build_dir / "by-agency" / "example-pd.sharing.json").read_text())
    assert j["latest"]["counts"] == {"outbound": 0, "inbound": None}
    assert j["unresolved"] == {"Mystery Org": ids.u5("flock-name:Mystery Org")}
    assert [c["seen"] for c in j["changes"]] == ["2026-01-01", "2026-01-07", "2026-01-22", "2026-01-29"]
    assert j["changes"][2]["added"] == ["Mystery Org"] and j["changes"][2]["removed"] == ["Partner A PD"]
