"""The lint: every rule the store's README says is checked, as code. Returns findings; never edits.

Levels: "error" fails the lint; "warn" is printed and never blocks a backup. Each finding names the file and the
rule in one line, so the message is the documentation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from . import ids
from .config import SCHEMES
from .store import (GEN_OWNED_ROOT, HAND_MSG_OK, HAND_NODE_OK, HAND_ROOT_OK, Fact, Root, Store,
                    check_locator_values, parse_locator)

BANNED = ("admitted", "quietly", "despite", "only", "merely", "failed to", "contradicts")
CORPUS_WORDS = ("produced", "exists", "attached", "no other")
VERIFIED_HOW = {"rendered", "native", "audio", "query"}
FINDING_HOW = {"sidecar", "transcript", "db"}


@dataclass
class Finding:
    level: str
    where: str
    msg: str

    def __str__(self) -> str:
        return f"{self.level.upper()} {self.where}: {self.msg}"


class Lint:
    def __init__(self, store: Store):
        self.s = store
        self.out: list[Finding] = []
        v = store.vocab
        self.V = {
            "fact_kind": set(v.get("fact", {}).get("kind", [])),
            "status": set(v.get("fact", {}).get("status", [])),
            "hold_source": set(v.get("hold", {}).get("source", [])),
            "hold_fact": set(v.get("hold", {}).get("fact", [])),
            "audiences": set(v.get("hold", {}).get("audiences", [])),
            "from": set(v.get("node", {}).get("from", [])),
            "access": set(v.get("node", {}).get("access", [])),
            "provenance": set(v.get("node", {}).get("provenance", [])),
            "root_kind": set(v.get("root", {}).get("kind", [])),
            "msg_kind": set(v.get("message", {}).get("kind", [])),
            "locator": set(v.get("locator", {}).get("kind", [])),
            "role": set(v.get("party", {}).get("role", [])),
            "topics": set(v.get("topics", {}).get("topic", [])) | set(v.get("topics", {}).get("vendor", [])),
            "mark_kind": set(v.get("mark", {}).get("kind", [])),
            "basis": set(v.get("observation", {}).get("basis", [])),
            "saw": set(v.get("observation", {}).get("saw", [])),
        }

    def err(self, where, msg):
        self.out.append(Finding("error", where, msg))

    def warn(self, where, msg):
        self.out.append(Finding("warn", where, msg))

    # ---- entry point ---------------------------------------------------------------------------------------------
    def run(self) -> list[Finding]:
        for p in self.s.problems:
            self.err(p.split(":")[0], "does not parse: " + p.split(":", 1)[1].strip())
        if not self.s.vocab:
            self.err("vocab.toml", "missing; the lint reads every enumeration from it")
        self.check_parties()
        for r in self.s.roots.values():
            self.check_root(r)
        for f in self.s.facts.values():
            self.check_fact(f)
        return self.out

    # ---- parties -------------------------------------------------------------------------------------------------
    def check_parties(self):
        p = self.s.parties
        if not p:
            return
        if "schema_version" not in p:
            self.err("parties.toml", "no schema_version")
        for kind in ("vendor", "jurisdiction", "person"):
            for x in p.get(kind, []):
                slug = x.get("slug", "")
                if kind == "person" and "/" in slug:
                    self.err("parties.toml", f"person slug carries an agency: {slug}")
                if x.get("id") and ids.party_id(kind, slug) != x["id"]:
                    self.err("parties.toml", f"{kind} {slug}: id does not derive from {kind}:{slug}")
                if kind == "person" and not x.get("roles"):
                    self.err("parties.toml", f"person {slug} has no [[person.roles]]")
                for role in x.get("roles", []) if kind == "person" else []:
                    b = role.get("basis")
                    if isinstance(b, dict) and b.get("source") and b["source"] not in self.s.nodes:
                        self.warn("parties.toml", f"person {slug}: role basis names an unregistered node")
                    elif isinstance(b, str):
                        self.warn("parties.toml", f"person {slug}: role basis is prose, not a node + locator")

    # ---- roots ---------------------------------------------------------------------------------------------------
    def check_root(self, r: Root):
        w = r.path.name
        g = r.gen
        for k in ("id", "key", "kind", "schema_version", "access", "provenance"):
            if k not in g:
                self.err(w, f"missing {k}")
        if g.get("key") and g["key"] != r.path.stem:
            self.err(w, f"key {g['key']!r} != file name")
        if g.get("kind") not in self.V["root_kind"]:
            self.err(w, f"kind {g.get('kind')!r} not in vocab")
        if g.get("access") not in self.V["access"]:
            self.err(w, f"access {g.get('access')!r} not in vocab (held is a hold, not an access)")
        if g.get("provenance") not in self.V["provenance"]:
            self.err(w, f"provenance {g.get('provenance')!r} not in vocab")
        if g.get("provenance") == "sent" and not r.hand.get("terms"):
            self.err(w, "a sent root needs `terms` on its hand side")
        try:
            want = ids.root_id(g)
            if g.get("id") != want:
                self.err(w, f"id does not derive from seed {ids.root_seed(g)!r} (expected {want[:8]}…)")
        except KeyError as e:
            self.err(w, f"cannot derive the id: missing {e}")
        if "hold" in g:
            self.err(w, "[hold] on the generated side; it belongs in the hand file")
        if g.get("path") and ":" not in g["path"].split("/")[0]:
            self.err(w, f"bare path {g['path']!r}: needs a scheme ({', '.join(SCHEMES)})")
        seen_n = set()
        for m in g.get("messages", []):
            n = m.get("n")
            if n in seen_n:
                self.err(w, f"message n={n} repeated")
            seen_n.add(n)
            if m.get("kind") not in self.V["msg_kind"]:
                self.err(w, f"message {n}: kind {m.get('kind')!r} not in vocab")
            if m.get("id") != ids.message_id(g, n):
                self.err(w, f"message {n}: id does not derive from its seed")
        shas = {}
        for node in r.nodes:
            d = node.data
            nm = d.get("name", "?")
            sha = d.get("sha256")
            if sha:
                if ids.node_id(sha) != d.get("id"):
                    self.err(w, f"node {nm}: id does not derive from sha256")
                shas.setdefault(sha, []).append(nm)
            else:
                self.warn(w, f"node {nm}: no sha256 yet (an error once a fact cites it)")
            if d.get("access") not in (None, *self.V["access"]):
                self.err(w, f"node {nm}: access {d.get('access')!r}")
            if d.get("from") not in (None, *self.V["from"]):
                self.err(w, f"node {nm}: from {d.get('from')!r} not in vocab")
            for k in ("path",):
                if d.get(k) and ":" not in d[k].split("/")[0]:
                    self.err(w, f"node {nm}: bare path {d[k]!r}")
            for a in d.get("also", []):
                if ":" not in a.split("/")[0]:
                    self.err(w, f"node {nm}: bare also entry {a!r}")
            if d.get("from") == "us" and "produced" in d:
                self.warn(w, f"node {nm}: a node we made carries `produced`; use `captured`")
            for k, v in d.items():
                if v == "":
                    self.err(w, f"node {nm}: {k} = \"\" (unknown is an absent key)")
            if "derived_from" in d:
                parents = d["derived_from"] if isinstance(d["derived_from"], list) else [d["derived_from"]]
                for pid in parents:
                    if pid and pid not in self.s.nodes:
                        self.warn(w, f"node {nm}: derived_from {pid[:8]}… is not a registered node")
        for k, v in g.items():
            if v == "":
                self.err(w, f"{k} = \"\" (unknown is an absent key)")
        # hand side
        h = r.hand
        hw = r.path.stem + ".hand.toml"
        if h:
            if "schema_version" not in h:
                self.err(hw, "no schema_version")
            for k in h:
                if k not in HAND_ROOT_OK:
                    self.err(hw, f"sets {k}, which the generator owns" if k in GEN_OWNED_ROOT else f"unknown hand key {k}")
            hold = h.get("hold")
            if hold:
                if hold.get("reason") not in self.V["hold_source"]:
                    self.err(hw, f"hold reason {hold.get('reason')!r} not in vocab")
                if "audiences" in hold and not isinstance(hold["audiences"], list):
                    self.err(hw, "hold.audiences must be a list")
            ns = {m.get("n") for m in g.get("messages", [])}
            for e in h.get("messages", []):
                if set(e) - HAND_MSG_OK:
                    self.err(hw, f"hand message entry sets {sorted(set(e) - HAND_MSG_OK)}")
                if e.get("n") not in ns:
                    self.err(hw, f"hand message n={e.get('n')} matches no generated message")
            for e in h.get("files", []):
                if set(e) - HAND_NODE_OK:
                    self.err(hw, f"hand node entry sets {sorted(set(e) - HAND_NODE_OK)}")
                if not e.get("sha256"):
                    self.err(hw, "hand node entry without sha256 (no name matching)")
                elif e["sha256"] not in shas:
                    self.err(hw, f"hand entry {e['sha256'][:12]}… matches no generated node")

    # ---- facts ---------------------------------------------------------------------------------------------------
    def check_fact(self, f: Fact):
        d, w = f.data, f.path.name
        fid = d.get("id", "")
        rel = str(f.path.relative_to(self.s.cfg.store / "facts")).removesuffix(".toml")
        if fid != rel:
            self.err(w, f"id {fid!r} != path {rel!r}")
        if "schema_version" not in d:
            self.err(w, "no schema_version")
        if d.get("kind") not in self.V["fact_kind"]:
            self.err(w, f"kind {d.get('kind')!r} not in vocab")
        if d.get("status") not in self.V["status"]:
            self.err(w, f"status {d.get('status')!r} not in vocab")
        # ordering trap: a top-level key written after the first table header belongs to that table
        lines = f.text.splitlines()
        first = next((i for i, l in enumerate(lines) if re.match(r"^\[\[?[A-Za-z_][\w.-]*\]\]?\s*(#.*)?$", l)), None)
        if first is not None:
            for i, l in enumerate(lines[first:], first):
                # keys that exist only at the top level of a fact (`in` and `fact` also live in rows/contradicts)
                if re.match(r"^(does_not_show|related|clear_plates|speaker_basis|topics|parties|title|profile)\s*=", l):
                    self.err(w, f"line {i+1}: top-level key after the first table (the TOML ordering trap)")
        for k, v in d.items():
            if v == "":
                self.err(w, f"{k} = \"\" (unknown is an absent key)")
        # banned words in record-level fields
        for k in ("title", "fact"):
            text = d.get(k, "")
            if isinstance(text, str):
                text = re.sub(r'"[^"]*"', '""', text)   # quoted record text is exempt; the rule is about our words
                for b in BANNED:
                    if re.search(rf"\b{re.escape(b)}\b", text, re.I):
                        self.warn(w, f"{k} uses {b!r}; record-level language only (rule 1)")
        # topics and parties
        for t in d.get("topics", []):
            if t not in self.V["topics"]:
                self.warn(w, f"topic {t} not in vocab")
        for p in d.get("parties", []):
            if not re.fullmatch(r"[0-9a-f-]{36}", str(p.get("id", ""))):
                self.err(w, f"party id is not a UUID: {p.get('id')}")
            if p.get("role") not in self.V["role"]:
                self.err(w, f"party role {p.get('role')!r} not in vocab")
        # speaker
        if d.get("speaker"):
            if d["speaker"] not in self.s.party_ids:
                self.err(w, "speaker is not in parties.toml")
            sb = d.get("speaker_basis")
            if not isinstance(sb, dict) or not sb.get("in") or not sb.get("locator"):
                self.err(w, "speaker needs speaker_basis = { by, in, locator, how }")
            elif sb["in"] not in d.get("sources", {}):
                self.err(w, f"speaker_basis names alias {sb['in']!r}, which the fact does not define")
        # aliases
        srcs = d.get("sources", {})
        used = set(d.get("in", []))
        for r in d.get("rows", []):
            used |= set(r.get("in", []))
        for k, v in d.items():
            if k.endswith("_in"):
                used |= set(v)
            if (k == "fact" or k.endswith("_basis")) and isinstance(v, str):
                used |= set(re.findall(r"\[([a-z0-9-]+)\]", v))
        for a in used:
            if a not in srcs:
                self.err(w, f"alias [{a}] is used but has no [sources.{a}]")
        for a, s in srcs.items():
            if a not in used:
                self.warn(w, f"alias {a} is defined but never used")
            src = s.get("source")
            if not src:
                self.err(w, f"alias {a}: no source")
            elif src not in self.s.nodes:
                self.err(w, f"alias {a}: source {src[:8]}… is not a registered node")
            else:
                node = self.s.nodes[src]
                if node.root.node_view(node).get("aid"):
                    self.err(w, f"alias {a} cites a finding aid ({node.name}); cite its parent")
            rt = s.get("root")
            if not rt:
                self.err(w, f"alias {a}: names no root")
            elif rt not in self.s.roots:
                self.err(w, f"alias {a}: root {rt[:8]}… is not registered")
            elif src and src in self.s.nodes and not any(n.id == src for n in self.s.roots[rt].nodes):
                self.err(w, f"alias {a}: node {src[:8]}… is not under root {self.s.roots[rt].key}")
            loc = s.get("locator")
            if not loc:
                self.err(w, f"alias {a}: no locator")
            else:
                try:
                    terms = parse_locator(loc)
                    for m in check_locator_values(terms, self.V["locator"]):
                        self.err(w, f"alias {a}: {m}")
                    kinds = [t.kind for t in terms]
                    if any(k in ("row", "rows", "cell") for k in kinds) and "sheet" not in kinds:
                        self.err(w, f"alias {a}: row()/rows()/cell() needs a sheet() on a workbook")
                    if "file" in kinds and src in self.s.nodes and not self.s.nodes[src].data.get("files"):
                        self.warn(w, f"alias {a}: file() on a node that is the file; the builder prints the path from the tree")
                except ValueError as e:
                    self.err(w, f"alias {a}: locator {loc!r}: {e}")
            if s.get("replacement") and not isinstance(s["replacement"], dict):
                self.err(w, f"alias {a}: replacement must be {{ node, checked, locator }}")
        # holds on the fact
        h = d.get("hold")
        if h:
            if h.get("reason") not in self.V["hold_fact"]:
                self.err(w, f"fact hold reason {h.get('reason')!r} not in vocab")
            if "audiences" in h and not isinstance(h["audiences"], list):
                self.err(w, "hold.audiences must be a list")
        # does_not_show: corpus-level negatives name their search
        for e in d.get("does_not_show", []):
            if isinstance(e, str) and any(wd in e.lower() for wd in CORPUS_WORDS):
                self.warn(w, f"does_not_show speaks about the corpus without a search: {e[:60]!r}")
            if isinstance(e, dict) and not e.get("searched"):
                self.err(w, "does_not_show table entry without `searched`")
        # observation rows: local date against the record's instant
        tz = ZoneInfo("America/Los_Angeles")
        for i, r in enumerate(d.get("rows", []), 1):
            if d.get("profile") == "observation":
                if r.get("saw") not in self.V["saw"]:
                    self.err(w, f"row {i}: saw {r.get('saw')!r} not in vocab")
                if r.get("basis") not in self.V["basis"]:
                    self.err(w, f"row {i}: basis {r.get('basis')!r} not in vocab")
                t = r.get("time")
                if t and r.get("at"):
                    try:
                        inst = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
                        local = inst.astimezone(tz).date()
                        at = r["at"] if isinstance(r["at"], date) else date.fromisoformat(str(r["at"]))
                        if local != at:
                            self.err(w, f"row {i}: at {at} is not the local date of {t} ({local} Pacific)")
                    except ValueError as e:
                        self.err(w, f"row {i}: time {t!r}: {e}")
        # verification
        ver = d.get("verification", {})
        how = ver.get("how")
        if d.get("status") == "verified":
            if not isinstance(how, dict):
                self.err(w, "verified but [verification].how is not per-alias")
            else:
                for a, v in how.items():
                    pre = str(v).split(":")[0].strip()
                    if pre in FINDING_HOW:
                        self.err(w, f"verified on a finding step: {a} = {pre}:")
                    elif pre not in VERIFIED_HOW:
                        self.err(w, f"how[{a}] must start with one of {sorted(VERIFIED_HOW)}")
                    if pre == "query" and not ver.get("audit_db"):
                        self.err(w, "query-verified fact needs [verification].audit_db = { code, state }")
                for a in srcs:
                    if a not in how:
                        self.err(w, f"verified but how has no entry for alias {a}")
            if "structure" not in ver:
                self.warn(w, "verified without [verification].structure (run `verify`)")
            if "artifacts" not in ver:
                self.warn(w, "verified without [verification].artifacts (run `verify`)")
            # whole-file claims need a whole-file locator
            text = (d.get("title", "") + " " + d.get("fact", "")).lower()
            if re.search(r"\b\d+ (pdf )?pages\b|\bin all\b|none in the production|\bnot in\b", text):
                whole = any(t.kind in ("pages", "file", "sheet") for s in srcs.values() if s.get("locator")
                            for t in _safe_terms(s["locator"]))
                if not whole:
                    self.err(w, "a whole-file claim (page count, 'in all', 'none in the production', 'not in') needs an alias whose locator spans the file")
        if d.get("kind") == "span":
            eb = str(d.get("end_basis", ""))
            if eb.startswith("nominal:") and d.get("status") == "verified":
                self.err(w, "a nominal: end on a verified fact needs how to cover the term clause (mark it ~ or set end = \"open\")")


def _safe_terms(loc: str):
    try:
        return parse_locator(loc)
    except ValueError:
        return []


def run_lint(store: Store) -> list[Finding]:
    return Lint(store).run()
