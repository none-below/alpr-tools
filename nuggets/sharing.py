"""`sharing <agency> [--tz <zone>]`: the sharing lists an agency's Flock transparency portal has shown, snapshot by
snapshot, as one span per partner and direction — the observed half of the store README's "Sharing at scale".

Reads `assets/transparency.flocksafety.com/<slug>/<YYYY-MM-DD>.json` in the site repo: the parse of each capture.
The capture itself (the PDF print, the page as served) is what a fact cites; the JSON is the aid that located the
entry. A snapshot either *observes* a direction's list — names shown; the section shown with nothing in it; the
portal declaring none — or does not: the section absent from the page, or an older parse that recorded an empty
list without saying whether the section was there. A span is a run of snapshots that list the partner, unbroken by
a snapshot that observed the list without it; snapshots with no observation neither extend nor break it. Its edges
are bracketed, never bare: the start lies after the last observation that lacked the partner and at or before the
first that showed it; the end at or after the last that showed it and before the first that lacked it, or open at
the latest observation. A partner removed and later listed again is two spans (rule 8).

Partner names resolve to registry agencies through `flock_names`; a name the registry lacks gets
uuid5(NS, "flock-name:<name>") so it still has an id (the README's rule for `parties.gen.toml`, which this command
does not write yet). Dates are the capture's local date in the agency's zone (`crawled_at`; the file name carries
the UTC date). Writes `_build/by-agency/<slug>.sharing.md` and `.sharing.json`. Nothing here is a fact: the
generated span facts of "Sharing at scale" are the next step, and this output is what they will be built from.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import ids
from .config import Config
from .store import load_store

DIRECTIONS = ("outbound", "inbound")
SNAP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")
DEFAULT_TZ = "America/Los_Angeles"


def registry(cfg: Config) -> list[dict]:
    p = (cfg.site_repo or Path(".")) / "assets" / "agency_registry.json"
    if not p.exists():
        raise SystemExit(f"no agency registry at {p} (nuggets.toml site_repo)")
    return json.loads(p.read_text())


def resolve_agency(reg: list[dict], aliases: dict, text: str) -> dict | None:
    """The registry entry for an alias from parties.toml, an agency id, a slug or a Flock org name."""
    key = str(aliases.get(text, text)).strip()
    low = key.lower()
    for e in reg:
        if e.get("agency_id") == key or (e.get("slug") or "").lower() == low:
            return e
    for e in reg:
        if any(str(n).strip().lower() == low for n in e.get("flock_names") or []):
            return e
    return None


def name_index(reg: list[dict]) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for e in reg:
        for n in e.get("flock_names") or []:
            idx.setdefault(str(n).strip().lower(), e)
    return idx


def observe(snapshot: dict, direction: str) -> tuple[set[str] | None, str]:
    """The names a snapshot observes on one list, or None with the reason it observed nothing."""
    names = set()
    for x in snapshot.get(f"sharing_{direction}") or []:
        n = x.get("name") if isinstance(x, dict) else x
        if isinstance(n, str) and n.strip():
            names.add(n.strip())
    shown = snapshot.get(f"sharing_{direction}_section_present")
    none = snapshot.get(f"sharing_{direction}_declared_none")
    if names:
        return names, "listed"
    if none is True:
        return set(), "declared none"
    if shown is True:
        return set(), "section shown, empty"
    if shown is False:
        return None, "section absent from the page"
    return None, "empty list; the parse did not record whether the section was shown"


def local_date(crawled_at, file_date: str, tz: str) -> str:
    if isinstance(crawled_at, str) and crawled_at:
        try:
            dt = datetime.fromisoformat(crawled_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(ZoneInfo(tz)).date().isoformat()
        except ValueError:
            pass
    return file_date


def read_snapshots(folder: Path, tz: str) -> list[dict]:
    out = []
    for p in sorted(folder.iterdir()):
        m = SNAP_RE.match(p.name)
        if not m:
            continue
        try:
            d = json.loads(p.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            out.append({"file": p.name, "date": m.group(1), "seen": m.group(1), "error": str(e)[:80], "manual": False, "obs": {}, "why": {}})
            continue
        if not isinstance(d, dict):
            continue
        s = {"file": p.name, "date": m.group(1), "seen": local_date(d.get("crawled_at"), m.group(1), tz),
             "crawled_at": d.get("crawled_at"), "manual": d.get("manual") is True, "obs": {}, "why": {}}
        for dr in DIRECTIONS:
            names, why = observe(d, dr)
            s["obs"][dr] = names
            s["why"][dr] = why
        out.append(s)
    return out


def spans_from(snaps: list[dict], direction: str) -> tuple[list[dict], str | None]:
    """Spans per partner from the ordered snapshots, and the date of the last snapshot that observed this list."""
    open_: dict[str, dict] = {}
    done: list[dict] = []
    last_obs = None
    for s in snaps:
        names = s["obs"].get(direction)
        if names is None:
            continue
        d = s["seen"]
        for n in sorted(names):
            sp = open_.get(n)
            if sp:
                sp["last_seen"], sp["n_snapshots"] = d, sp["n_snapshots"] + 1
                sp["manual"] = sp["manual"] or s["manual"]
            else:
                open_[n] = {"partner": n, "direction": direction, "first_seen": d, "last_seen": d, "absent_before": last_obs,
                            "absent_after": None, "n_snapshots": 1, "manual": s["manual"], "status": "ended"}
        for n in list(open_):
            if n not in names:
                sp = open_.pop(n)
                sp["absent_after"] = d
                done.append(sp)
        last_obs = d
    for sp in open_.values():
        sp["status"] = "open"
        done.append(sp)
    done.sort(key=lambda x: (x["partner"].lower(), x["first_seen"]))
    return done, last_obs


def changes(snaps: list[dict], direction: str) -> list[dict]:
    out, prev = [], None
    for s in snaps:
        names = s["obs"].get(direction)
        if names is None:
            continue
        if prev is not None:
            added, removed = sorted(names - prev), sorted(prev - names)
            if added or removed:
                out.append({"seen": s["seen"], "file": s["file"], "direction": direction, "added": added, "removed": removed, "n": len(names)})
        else:
            out.append({"seen": s["seen"], "file": s["file"], "direction": direction, "added": sorted(names), "removed": [], "n": len(names), "first": True})
        prev = names
    return out


def _edges(sp: dict) -> tuple[str, str]:
    start = (f">{sp['absent_before']}, " if sp["absent_before"] else "") + f"<={sp['first_seen']}"
    end = f">={sp['last_seen']}" + (f", <{sp['absent_after']}" if sp["absent_after"] else " (open at the latest observation)")
    return start, end


def _git_head(repo: Path | None, sub: str) -> str:
    if not repo:
        return "?"
    try:
        r = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%h", "--", sub], capture_output=True, text=True, check=True)
        return r.stdout.strip() or "?"
    except (OSError, subprocess.CalledProcessError):
        return "?"


def build_sharing(cfg: Config, agency: str, tz: str = DEFAULT_TZ) -> int:
    store = load_store(cfg)
    aliases = store.parties.get("aliases", {})
    reg = registry(cfg)
    e = resolve_agency(reg, aliases, agency)
    if not e:
        print(f"sharing: {agency!r} is not an alias, agency id, slug or Flock org name in the registry")
        return 2
    slug, aid = e["slug"], e["agency_id"]
    name = (e.get("flock_names") or [slug])[0]
    folder = (cfg.site_repo or Path(".")) / "assets" / "transparency.flocksafety.com" / slug
    if not folder.is_dir():
        print(f"sharing: no portal snapshots at {folder}")
        return 2
    snaps = read_snapshots(folder, tz)
    if not snaps:
        print(f"sharing: no <YYYY-MM-DD>.json snapshots in {folder}")
        return 2
    idx = name_index(reg)
    spans, latest_obs, unresolved = [], {}, {}
    for dr in DIRECTIONS:
        ss, last = spans_from(snaps, dr)
        latest_obs[dr] = last
        for sp in ss:
            hit = idx.get(sp["partner"].lower())
            if hit:
                sp["partner_id"], sp["partner_slug"] = hit["agency_id"], hit.get("slug")
            else:
                sp["partner_id"], sp["partner_slug"] = ids.u5("flock-name:" + sp["partner"]), None
                unresolved[sp["partner"]] = sp["partner_id"]
            spans.append(sp)
    chg = [c for dr in DIRECTIONS for c in changes(snaps, dr)]
    chg.sort(key=lambda c: (c["seen"], c["direction"]))
    latest = snaps[-1]
    counts = {dr: (len(latest["obs"][dr]) if latest["obs"].get(dr) is not None else None) for dr in DIRECTIONS}
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    site = _git_head(cfg.site_repo, f"assets/transparency.flocksafety.com/{slug}")
    result = {"agency": {"agency_id": aid, "slug": slug, "name": name}, "tz": tz, "generated": generated,
              "site_repo": f"sm-alpr@{site}", "snapshot_folder": f"repo:assets/transparency.flocksafety.com/{slug}",
              "snapshots": [{k: v for k, v in s.items() if k != "obs"} | {"n": {dr: (len(s["obs"][dr]) if s["obs"].get(dr) is not None else None) for dr in DIRECTIONS}} for s in snaps],
              "latest": {"seen": latest["seen"], "file": latest["file"], "counts": counts, "why": latest["why"]},
              "latest_observation": latest_obs, "spans": spans, "changes": chg, "unresolved": unresolved}
    out_dir = cfg.build_dir / "by-agency"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{slug}.sharing.json").write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n")

    n_manual = sum(1 for s in snaps if s["manual"])
    lines = [f"# {name} — sharing, as the portal showed it ({len(snaps)} snapshots, {snaps[0]['seen']} → {snaps[-1]['seen']})", "",
             f"generated {generated}; agency {aid} ({slug}); snapshots `repo:assets/transparency.flocksafety.com/{slug}` @ sm-alpr@{site}; "
             f"dates are capture days in {tz}" + (f"; {n_manual} manual transcriptions" if n_manual else ""), "",
             "An observed bound is never a recorded start or end: `>d1, <=d2` means absent in the capture of d1, present in the capture of d2; "
             "`>=d3, <d4` the mirror; a snapshot that did not show the section observes nothing and neither extends nor ends a span.", ""]
    lines += [f"## Latest snapshot ({latest['seen']}, {latest['file']})", ""]
    for dr in DIRECTIONS:
        c = counts[dr]
        lines.append(f"- {dr}: " + (f"{c} listed" if c else f"none observed ({latest['why'][dr]})") + (f"; last observed {latest_obs[dr]}" if latest_obs[dr] and latest_obs[dr] != latest["seen"] else ""))
    lines.append("")
    for dr in DIRECTIONS:
        ss = [sp for sp in spans if sp["direction"] == dr]
        n_open = sum(1 for sp in ss if sp["status"] == "open")
        lines += [f"## {dr.capitalize()} spans ({len(ss)}; open {n_open})", ""]
        if not ss:
            lines += ["(no partner ever observed on this list)", ""]
            continue
        lines += ["| partner | partner id | start | end | snapshots | status | basis |", "|---|---|---|---|---|---|---|"]
        for sp in ss:
            start, end = _edges(sp)
            basis = "snapshot" + (" (manual transcription)" if sp["manual"] else "") + ("" if sp["partner_slug"] else "; name not in registry")
            lines.append(f"| {sp['partner']} | {sp['partner_id']} | {start} | {end} | {sp['n_snapshots']} | {sp['status']} | {basis} |")
        lines.append("")
    lines += [f"## Changes by snapshot ({len(chg)})", ""]
    if chg:
        lines += ["| seen | direction | listed | added | removed |", "|---|---|---|---|---|"]
        for c in chg:
            tag = " (first observation)" if c.get("first") else ""
            lines.append(f"| {c['seen']}{tag} | {c['direction']} | {c['n']} | {len(c['added'])}: {', '.join(c['added'][:12])}{' …' if len(c['added']) > 12 else ''} | "
                         f"{len(c['removed'])}: {', '.join(c['removed'][:12])}{' …' if len(c['removed']) > 12 else ''} |")
        lines.append("")
    no_obs = [(s["seen"], dr, s["why"][dr]) for s in snaps for dr in DIRECTIONS if s.get("obs") is not None and s["obs"].get(dr) is None]
    lines += [f"## Snapshots that observed nothing ({len(no_obs)} direction-captures)", ""]
    lines += [f"- {d} {dr}: {why}" for d, dr, why in no_obs] or ["(none)"]
    lines.append("")
    lines += [f"## Names the registry cannot resolve ({len(unresolved)})", ""]
    lines += [f"- {n} → {i} (uuid5 of flock-name:<name>)" for n, i in sorted(unresolved.items())] or ["(none)"]
    lines.append("")
    (out_dir / f"{slug}.sharing.md").write_text("\n".join(lines))
    n_sp = {dr: sum(1 for sp in spans if sp["direction"] == dr) for dr in DIRECTIONS}
    print(f"sharing {slug}: {len(snaps)} snapshots, spans outbound {n_sp['outbound']} / inbound {n_sp['inbound']}, "
          f"{len(unresolved)} unresolved names → {out_dir / (slug + '.sharing.md')}, .sharing.json")
    return 0
