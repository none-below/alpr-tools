"""Reading the store: roots (generated side merged with the hand side), facts, parties, vocabulary; the locator
grammar. Read-only. Tools write only generated sides and `_build/`; facts and hand sides are a person's.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config

# ---- locator grammar ---------------------------------------------------------------------------------------------
# locator = term (',' term)* ; term = kind '(' value ')' ; a value containing ) , ; is double-quoted.
# Value syntax per kind: integer | a-b range | a;b;c list | free text | time [h:]mm:ss[.d] with a hyphen range.

INT_KINDS = {"page", "line", "row", "message", "printed", "item"}
RANGE_KINDS = {"pages", "rows"}
TEXT_KINDS = {"file", "sheet", "cell", "section", "subdivision", "field", "item"}
TIME_RE = re.compile(r"^(?:\d+:)?\d{1,2}:\d{2}(?:\.\d+)?(?:-(?:\d+:)?\d{1,2}:\d{2}(?:\.\d+)?)?$")


@dataclass
class Term:
    kind: str
    value: str

    def __str__(self) -> str:
        v = self.value
        if any(c in v for c in ')' + ',' + ';') and not v.startswith('"'):
            v = '"' + v.replace('"', '\\"') + '"'
        return f"{self.kind}({v})"


def parse_locator(text: str) -> list[Term]:
    """Split on top-level commas, respecting parentheses and quotes; return Terms. Raises ValueError on bad syntax."""
    terms, i, n = [], 0, len(text)
    while i < n:
        while i < n and text[i] in " \t":
            i += 1
        if i >= n:
            break
        m = re.match(r"([a-z]+)\(", text[i:])
        if not m:
            raise ValueError(f"expected kind( at {text[i:i+20]!r}")
        kind = m.group(1)
        i += m.end()
        if i < n and text[i] == '"':
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    j += 1
                buf.append(text[j])
                j += 1
            if j >= n or text[j + 1 : j + 2] != ")":
                raise ValueError(f"unterminated quoted value in {kind}(")
            value, i = "".join(buf), j + 2
        else:
            j = text.find(")", i)
            if j < 0:
                raise ValueError(f"missing ) after {kind}(")
            value, i = text[i:j], j + 1
            if "," in value:
                raise ValueError(f"{kind}({value}): a comma separates terms; a list uses ; and a value with a comma is quoted")
            if ";" in value and kind not in RANGE_KINDS:
                raise ValueError(f"{kind}({value}): only rows()/pages() take a ;-list; quote the value otherwise")
        terms.append(Term(kind, value))
        while i < n and text[i] in " \t":
            i += 1
        if i < n:
            if text[i] != ",":
                raise ValueError(f"expected , between terms at {text[i:i+20]!r}")
            i += 1
    if not terms:
        raise ValueError("empty locator")
    return terms


def check_locator_values(terms: list[Term], kinds: set[str]) -> list[str]:
    """Value-shape problems for each term, given the vocabulary's locator kinds."""
    out = []
    for t in terms:
        if t.kind not in kinds:
            out.append(f"unknown locator kind {t.kind}()")
            continue
        v = t.value
        if t.kind in INT_KINDS and t.kind != "item" and not v.isdigit():
            out.append(f"{t.kind}({v}) is not an integer")
        elif t.kind in RANGE_KINDS:
            if not (re.fullmatch(r"\d+-\d+", v) or re.fullmatch(r"\d+(;\d+)+", v) or v.isdigit()):
                out.append(f"{t.kind}({v}) is not a-b, a;b;c or an integer")
        elif t.kind == "time" and not TIME_RE.fullmatch(v):
            out.append(f"time({v}) is not [h:]mm:ss[.d] with a hyphen range")
    return out


# ---- loading -----------------------------------------------------------------------------------------------------

GEN_OWNED_ROOT = {"id", "key", "kind", "schema_version", "ref", "url", "path", "access", "provenance", "retrieved",
                  "captured", "released_by", "agency", "portal", "tz", "meeting", "body", "archived", "messages",
                  "status", "files"}
HAND_ROOT_OK = {"schema_version", "title", "note", "also", "hold", "tags", "findings_n", "sender", "terms",
                "received", "hosted", "files", "messages"}
HAND_NODE_OK = {"sha256", "access", "note", "also", "aid", "variant", "derived_from", "method", "duration",
                "hold", "findings_n"}
HAND_MSG_OK = {"n", "note", "hold"}


@dataclass
class Node:
    data: dict
    root: "Root"
    parent: "Node | None" = None

    @property
    def id(self) -> str | None:
        return self.data.get("id")

    @property
    def sha256(self) -> str | None:
        return self.data.get("sha256")

    @property
    def name(self) -> str:
        return self.data.get("name", "")

    def chain(self) -> list["Node"]:
        out, n = [], self
        while n:
            out.append(n)
            n = n.parent
        return out  # leaf first


@dataclass
class Root:
    path: Path
    gen: dict
    hand: dict = field(default_factory=dict)
    nodes: list[Node] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.gen["id"]

    @property
    def key(self) -> str:
        return self.gen["key"]

    @property
    def kind(self) -> str:
        return self.gen["kind"]

    @property
    def hold(self) -> dict | None:
        return self.hand.get("hold")

    def merged(self) -> dict:
        """The root as the builder sees it: hand over generated, for the keys the hand side may carry."""
        d = dict(self.gen)
        for k, v in self.hand.items():
            if k in ("files", "messages", "schema_version"):
                continue
            d[k] = v
        return d

    def hand_node_entries(self) -> dict[str, dict]:
        return {e["sha256"]: e for e in self.hand.get("files", []) if e.get("sha256")}

    def node_view(self, node: Node) -> dict:
        d = dict(node.data)
        h = self.hand_node_entries().get(node.sha256 or "")
        if h:
            d.update({k: v for k, v in h.items() if k != "sha256"})
        return d


@dataclass
class Fact:
    path: Path
    data: dict
    text: str

    @property
    def id(self) -> str:
        return self.data["id"]


@dataclass
class Store:
    cfg: Config
    roots: dict[str, Root] = field(default_factory=dict)       # by root id
    nodes: dict[str, Node] = field(default_factory=dict)       # by node id
    facts: dict[str, Fact] = field(default_factory=dict)       # by fact id
    parties: dict = field(default_factory=dict)
    vocab: dict = field(default_factory=dict)
    readme: str = ""
    problems: list[str] = field(default_factory=list)          # load-time errors (bad TOML), reported by the lint

    @property
    def party_ids(self) -> set[str]:
        return {x["id"] for k in ("vendor", "jurisdiction", "person") for x in self.parties.get(k, []) if x.get("id")}

    def node_under_root(self, node_id: str, root_id: str) -> bool:
        n = self.nodes.get(node_id)
        return bool(n) and n.root.id == root_id or any(
            x.id == node_id for r in [self.roots.get(root_id)] if r for x in r.nodes)


def _load_toml(p: Path, problems: list[str]) -> dict | None:
    try:
        return tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as e:
        problems.append(f"{p}: {e}")
        return None


def load_store(cfg: Config) -> Store:
    s = Store(cfg=cfg)
    root = cfg.store
    readme = root / "README.md"
    s.readme = readme.read_text() if readme.exists() else ""
    v = root / "vocab.toml"
    s.vocab = _load_toml(v, s.problems) or {} if v.exists() else {}
    p = root / "parties.toml"
    s.parties = _load_toml(p, s.problems) or {} if p.exists() else {}
    for g in sorted((root / "sources" / "gen").glob("*.toml")):
        gen = _load_toml(g, s.problems)
        if gen is None:
            continue
        hp = root / "sources" / (g.stem + ".hand.toml")
        hand = (_load_toml(hp, s.problems) or {}) if hp.exists() else {}
        r = Root(path=g, gen=gen, hand=hand)

        def walk(files, parent=None):
            for f in files:
                n = Node(data=f, root=r, parent=parent)
                r.nodes.append(n)
                if n.id:
                    s.nodes.setdefault(n.id, n)
                walk(f.get("files", []), n)

        walk(gen.get("files", []))
        s.roots[gen.get("id", g.stem)] = r
    for fp in sorted((root / "facts").rglob("*.toml")):
        text = fp.read_text()
        d = _load_toml(fp, s.problems)
        if d is None:
            continue
        s.facts[d.get("id", str(fp))] = Fact(path=fp, data=d, text=text)
    return s
