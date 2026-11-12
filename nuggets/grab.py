"""`grab <fact> [--for gj|public]`: the fact's citation block, in the one grammar the store's README fixes.

Per alias, line 1: `Source [<alias>]: <node> (sha256 <full hex>, <bytes> bytes)[ in <container> …], <root label>,
<provenance phrase>[, <date>]; <locator>.`  Line 2: the URL (a `repo:` path becomes a commit-pinned blob link,
pinned to the oldest first-parent commit on origin/main holding those bytes) or `local: <path> (not public: …)`,
then `[<status>]`. Then `also in:` for other roots holding the same hash, and `proof:` (today only the weakest
tier, "in public git by <date>"). Hashes print in full, never as a prefix.

With no `--for`, two blocks: the public-citable lines first (under `--- not for pasting: draft ---` when the fact
is not verified), then `--- internal: held or local, not for pasting ---` with the rest and each hold inline.
`--for <audience>` prints that audience's block alone and refuses, with one line saying why and what would
change it, when nothing remains. A fact with a lint error is refused the same way.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from .config import Config
from .lint import run_lint
from .store import Fact, Node, Root, Store, load_store, parse_locator

CITABLE_STATUS = {"verified", "observed"}
PUBLIC_ACCESS = {"public", "repo"}


# ---- names ---------------------------------------------------------------------------------------------------

def agency_names(cfg: Config) -> dict[str, str]:
    out: dict[str, str] = {}
    if cfg.site_repo:
        p = cfg.site_repo / "assets" / "agency_registry.json"
        if p.exists():
            for a in json.loads(p.read_text()):
                if a.get("agency_id"):
                    out[a["agency_id"]] = a.get("display_name") or (a.get("flock_names") or [a.get("slug", "?")])[0]
    return out


def party_names(store: Store) -> dict[str, str]:
    d: dict[str, str] = {}
    for k in ("vendor", "jurisdiction", "person"):
        for x in store.parties.get(k, []):
            if x.get("id"):
                d[x["id"]] = x.get("name") or x.get("slug", "")
    return d


def root_label(root: Root, names: dict[str, str]) -> str:
    g = root.merged()
    k = g.get("kind", "")
    ag = names.get(g.get("agency", ""), g.get("agency", ""))
    if k == "pra":
        return f"{ag} PRA {g.get('ref', '')}"
    if k == "muckrock":
        return f"MuckRock {g.get('ref') or str(g.get('key', '')).removeprefix('mr-')}"
    if k == "snapshot":
        return f"{ag} Flock transparency portal, snapshot {g.get('retrieved', '')}"
    if k == "recording":
        return f"{ag} meeting recording, {g.get('meeting', '')}"
    if k == "packet":
        return f"{ag} council packet, {g.get('meeting', '')}"
    if k == "sent":
        return f"sent by {g.get('sender', '?')}, received {g.get('received', '')}"
    if k == "email":
        return f"email {g.get('message_id', '')}"
    return g.get("title") or g.get("url") or f"{k} {g.get('key', '')}"


def provenance_phrase(prov: str, root: Root, names: dict[str, str]) -> str:
    g = root.merged()
    if prov == "release":
        who = g.get("released_by") or g.get("agency", "")
        return f"released by {names.get(who, who)}"
    if prov == "host":
        return f"hosted by {urlsplit(g.get('url', '')).netloc or g.get('hosted', '?')}"
    if prov == "capture":
        return "our capture"
    if prov == "sent":
        return f"sent by {g.get('sender', 'the sender')}"
    if prov == "observation":
        return "our observation"
    if prov == "leak":
        return "from a leak (internal)"
    return prov or "provenance not recorded"


# ---- git pinning ---------------------------------------------------------------------------------------------

def repo_web_url(site_repo: Path) -> str | None:
    try:
        u = subprocess.run(["git", "-C", str(site_repo), "remote", "get-url", "origin"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    m = re.match(r"(?:git@github\.com:|https://github\.com/)([^/]+)/([^/]+?)(?:\.git)?/?$", u)
    return f"https://github.com/{m.group(1)}/{m.group(2)}" if m else None


def git_pin(site_repo: Path, rel_path: str) -> tuple[str, str] | None:
    """(commit, date) of the oldest first-parent commit on origin/main that holds these exact bytes at this path."""
    p = site_repo / rel_path
    if not p.exists():
        return None
    try:
        blob = subprocess.run(["git", "-C", str(site_repo), "hash-object", str(p)], capture_output=True, text=True, check=True).stdout.strip()
        r = subprocess.run(["git", "-C", str(site_repo), "log", "--first-parent", "--format=%H %cs", f"--find-object={blob}", "origin/main", "--", rel_path],
                           capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    if not lines:
        return None
    h, d = lines[-1].split()[:2]
    return h, d


# ---- access and holds ------------------------------------------------------------------------------------------

def node_access(root: Root, node: Node) -> str:
    for n in node.chain():
        v = root.node_view(n)
        if v.get("access"):
            return v["access"]
    return root.merged().get("access", "local")


def holds_on(root: Root, node: Node, fact: Fact) -> list[dict]:
    hs = []
    for n in node.chain():
        v = root.node_view(n)
        if v.get("hold"):
            hs.append(dict(v["hold"], _on=f"node {n.name}"))
    if root.hold:
        hs.append(dict(root.hold, _on=f"root {root.key}"))
    if fact.data.get("hold"):
        hs.append(dict(fact.data["hold"], _on="fact"))
    return hs


def barring_hold(holds: list[dict], audience: str) -> dict | None:
    """A hold bars every audience but internal unless its `audiences` names the audience."""
    for h in holds:
        if audience not in (h.get("audiences") or []):
            return h
    return None


# ---- rendering -------------------------------------------------------------------------------------------------

def render_locator(root: Root, locator: str) -> str:
    try:
        terms = parse_locator(locator)
    except ValueError:
        return locator
    msgs = {m.get("n"): m for m in root.gen.get("messages", [])}
    parts = []
    for t in terms:
        if t.kind == "message" and t.value.isdigit() and int(t.value) in msgs:
            m = msgs[int(t.value)]
            pg = f" = {m['pages']}" if m.get("pages") else ""
            parts.append(f"message({t.value}){pg} [{m.get('at', '')}, {m.get('from', '')} {m.get('kind', '')}]")
        else:
            parts.append(str(t))
    return ", ".join(parts)


def status_tag(root: Root) -> str:
    st = root.gen.get("status", [])
    if not st:
        return ""
    last = st[-1]
    return f"[{last.get('state', '')} {last.get('at', '')}]".replace("  ", " ")


def node_date(view: dict, root: Root) -> str:
    for k in ("produced", "captured", "received", "posted"):
        if view.get(k):
            return f"{k} {view[k]}"
    g = root.merged()
    if g.get("retrieved"):
        return f"retrieved {g['retrieved']}"
    return ""


def alias_block(cfg: Config, store: Store, fact: Fact, alias: str, src: dict, names: dict[str, str], web: str | None) -> dict:
    """Everything `grab` prints for one alias, plus what gates it."""
    out = {"alias": alias, "lines": [], "public": False, "gj": False, "why": ""}
    node = store.nodes.get(src.get("source", ""))
    if not node:
        out["lines"].append(f"Source [{alias}]: unregistered node {src.get('source')}")
        out["why"] = "the node is not registered"
        return out
    root = node.root
    view = root.node_view(node)
    chain = node.chain()
    head = f"Source [{alias}]: {node.name} (sha256 {node.sha256}, {view.get('bytes', '?')} bytes)"
    for c in chain[1:]:
        cv = root.node_view(c)
        url = f"{cv['url']}, " if cv.get("url") else ""
        head += f" in {c.name} ({url}sha256 {c.sha256}, {cv.get('bytes', '?')} bytes)"
    prov = view.get("provenance") or root.merged().get("provenance", "")
    head += f", {root_label(root, names)}, {provenance_phrase(prov, root, names)}"
    d = node_date(view, root)
    if d:
        head += f", {d}"
    head += f"; {render_locator(root, src.get('locator', ''))}."
    out["lines"].append(head)
    # line 2: where
    access = node_access(root, node)
    url = view.get("url") or (root.node_view(chain[-1]).get("url") if len(chain) > 1 else None)
    path = view.get("path", "")
    pin = None
    if url:
        where = url
    elif path.startswith("repo:") and cfg.site_repo:
        rel = path[len("repo:"):]
        pin = git_pin(cfg.site_repo, rel)
        if pin and web:
            where = f"{web}/blob/{pin[0]}/{rel}"
        else:
            where = f"local: {path} (not public: not on origin/main yet)"
    elif path:
        where = f"local: {path} (not public: {access} copy)"
    else:
        where = "local: path not recorded"
    out["lines"].append(f"{where}  {status_tag(root)}".rstrip())
    # also in
    others = [r for r in store.roots.values() if r.id != root.id and any(n.sha256 == node.sha256 for n in r.nodes)]
    for r in others:
        if r.hold:
            continue  # a held root never prints as "also in"
        on = next(n for n in r.nodes if n.sha256 == node.sha256)
        ov = r.node_view(on)
        out["lines"].append(f"also in: {root_label(r, names)} ({r.merged().get('provenance', '')}, {ov.get('produced') or ov.get('captured') or ''}) — same bytes")
    # proof
    out["lines"].append(f"proof: in public git by {pin[1]} ({pin[0][:12]})" if pin else "proof: none recorded")
    # gates
    holds = holds_on(root, node, fact)
    for h in holds:
        out["lines"].append(f"hold ({h.get('_on')}): {h.get('reason', '')}" + (f" — {h['note']}" if h.get("note") else "") + (f"; until {h['until']}" if h.get("until") else ""))
    citable_status = fact.data.get("status") in CITABLE_STATUS
    hp, hg = barring_hold(holds, "public"), barring_hold(holds, "gj")
    out["public"] = citable_status and access in PUBLIC_ACCESS and hp is None
    out["gj"] = citable_status and hg is None
    if not citable_status:
        out["why"] = f"is {fact.data.get('status')}"
    elif hp:
        out["why"] = f"is held: {hp.get('reason')}"
    elif access not in PUBLIC_ACCESS:
        out["why"] = "is local only"
    return out


def build_stamp_line(cfg: Config) -> str:
    def head(p: Path | None) -> str:
        if not p or not p.exists():
            return "?"
        try:
            h = subprocess.run(["git", "-C", str(p), "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
            dirty = subprocess.run(["git", "-C", str(p), "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True).stdout.strip()
            return h + ("+dirty" if dirty else "")
        except (OSError, subprocess.CalledProcessError):
            return "?"
    return f"stamp: data-repo {head(cfg.store)}, site-repo {head(cfg.site_repo)}, tools {head(cfg.tools_repo)}"


def what_would_make_it_citable(fact: Fact, blocks: list[dict]) -> str:
    st = fact.data.get("status")
    if st == "draft":
        return "run `verify`, open every locator, set status = verified with a per-alias how"
    if st == "retracted":
        return "nothing; cite the fact that replaced it"
    held = [b for b in blocks if "held" in b["why"]]
    if held:
        return "the hold lifted (its `until`), or an unheld copy of the same bytes"
    return "a public or repo copy of the bytes (commit it or deposit it)"


def grab(cfg: Config, fact_id: str, audience: str | None = None) -> int:
    store = load_store(cfg)
    fact = store.facts.get(fact_id)
    if not fact:
        print(f"refused: no fact {fact_id!r}")
        return 2
    errs = [f for f in run_lint(store) if f.level == "error" and (fact_id in f.where or str(fact.path) in f.where)]
    if errs:
        print(f"refused: {fact_id} has {len(errs)} lint error(s); fix them first:")
        for e in errs:
            print("  " + str(e))
        return 1
    names = {**agency_names(cfg), **party_names(store)}
    web = repo_web_url(cfg.site_repo) if cfg.site_repo else None
    srcs = fact.data.get("sources", {})
    blocks = [alias_block(cfg, store, fact, a, s, names, web) for a, s in srcs.items()]
    d = fact.data
    when = d.get("date") or (f"{d.get('start')} → {d.get('end')}" if d.get("start") else d.get("window", ""))
    header = [fact_id, d.get("title", ""), f"status: {d.get('status')}; kind: {d.get('kind')}; {when}", build_stamp_line(cfg)]
    if audience:
        ok = [b for b in blocks if b[audience]]
        if not ok:
            why = blocks[0]["why"] if blocks else "has no sources"
            print(f"refused: {fact_id} {why}; what would make it citable: {what_would_make_it_citable(fact, blocks)}")
            return 1
        print("\n".join(header))
        if len(ok) < len(blocks):
            print(f"partial: {len(ok)} of {len(blocks)} sources are {audience}-citable; the rest are not")
        for b in ok:
            print()
            print("\n".join(ln for ln in b["lines"] if not ln.startswith("hold (")))
        return 0
    print("\n".join(header))
    pub = [b for b in blocks if b["public"]]
    rest = [b for b in blocks if not b["public"]]
    draft = d.get("status") not in CITABLE_STATUS
    if draft:
        print("--- not for pasting: draft ---")
        for b in blocks:
            print()
            print("\n".join(b["lines"]))
        return 0
    for b in pub:
        print()
        print("\n".join(b["lines"]))
    if rest:
        print()
        print("--- internal: held or local, not for pasting ---")
        for b in rest:
            print()
            print("\n".join(b["lines"]))
            print(f"not public: {b['why']}")
    return 0
