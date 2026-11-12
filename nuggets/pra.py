"""`sources add <folder> --kind pra`: a GovQA request folder → the root's generated side.

Reads nothing a person wrote: `metadata.json` for the title, every file's bytes for its hash, and the
Message_History export (its text sidecar when present, else `pdftotext`) for the messages, their page ranges
and the status edges. Rewrites `sources/gen/<key>.toml` on every run, add-only: a node whose bytes are gone gets
`missing`, a known message keeps its `n`. The hand side is never touched.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tomllib
from datetime import date, datetime
from pathlib import Path

from . import ids
from .config import Config

HEADER_RE = re.compile(r"^On (\d{1,2}/\d{1,2}/\d{4}) (\d{1,2}:\d{2}:\d{2} [AP]M), (.+?) wrote:\s*$", re.M)
PAGE_RE = re.compile(r"^--- page (\d+) ---\s*$", re.M)
FOOTER_RE = re.compile(r"^\s*Page \d+\s*$", re.M)
AUTO_ACK_RE = re.compile(r"Thank you for your interest in public records", re.I)
CLOSED_RE = re.compile(r"(?:now\s+)?considers?\s+(?:this|the)[\w\s\d\-#]{0,80}closed", re.I)
CYCLE_RE = re.compile(r"no later than|additional time|next update|rolling basis|will provide .{0,40}update", re.I)
# records go out in *this* message (not a reference to an earlier release such as "sent on 3/27")
RELEASE_RE = re.compile(r"see attached|\battached\b|has been released|records are available on|select the link below|made available on", re.I)
EXTENSION_RE = re.compile(r"\bextension\b|14[- ]day", re.I)
CREATED_RE = re.compile(r"^\s*Request (?:was )?created", re.I)
RECEIVED_RE = re.compile(r"received the following records request (?:from you )?on ([A-Z][a-z]+ \d{1,2}, \d{4})")
SIDECAR_RE = re.compile(r"\.[0-9a-f]{8}\.(?:txt|pdf)$")
AGENCY_WRITER_RE = re.compile(r"Public Records Center|Records Center", re.I)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def message_history_text(folder: Path, pdf: Path) -> str:
    side = sorted(folder.glob(pdf.name + ".*.txt"))
    if side:
        return side[0].read_text(errors="replace")
    # pdftotext per page, with the same page markers the site's sidecars use
    n = pdf_pages(pdf) or 1
    out = []
    for i in range(1, n + 1):
        r = subprocess.run(["pdftotext", "-f", str(i), "-l", str(i), "-layout", str(pdf), "-"], capture_output=True, text=True)
        out.append(f"--- page {i} ---\n" + r.stdout)
    return "\n".join(out)


def pdf_pages(pdf: Path) -> int | None:
    try:
        r = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    m = re.search(r"^Pages:\s+(\d+)", r.stdout, re.M)
    return int(m.group(1)) if m else None


def pdf_info(pdf: Path) -> dict:
    try:
        r = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return {}
    d = {}
    for line in r.stdout.splitlines():
        k, _, v = line.partition(":")
        d[k.strip()] = v.strip()
    return d


def parse_messages(text: str) -> list[dict]:
    """Messages in export order (newest first), each with page range, writer, timestamp, body and kind."""
    page_at = []  # (offset, page)
    for m in PAGE_RE.finditer(text):
        page_at.append((m.start(), int(m.group(1))))

    def page_of(pos: int) -> int:
        p = 1
        for off, pg in page_at:
            if off <= pos:
                p = pg
        return p

    heads = list(HEADER_RE.finditer(text))
    out = []
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[h.end():end]
        body = FOOTER_RE.sub("", PAGE_RE.sub("", body)).strip()
        when = datetime.strptime(h.group(1) + " " + h.group(2), "%m/%d/%Y %I:%M:%S %p")
        writer = h.group(3).strip()
        out.append({"at": when, "writer": writer, "body": body, "page_from": page_of(h.start()), "page_to": page_of(end - 1)})
    return out


def classify(msgs_chrono: list[dict]) -> None:
    """Writer and wording → `from` and `kind`. Order matters: the portal's auto-ack and "request created" lines,
    then a closure, then a message that releases records, then an extension, then a rolling-production cycle; what
    is left is a response. A "sent on <earlier date>" reference is not a release in this message."""
    seen_request = False
    for m in msgs_chrono:
        agency_side = bool(AGENCY_WRITER_RE.search(m["writer"]))
        body = m["body"]
        if not agency_side:
            m["from"] = "requester"
            m["kind"] = "clarification" if seen_request else "request"
            seen_request = True
            continue
        if CREATED_RE.search(body):
            m["from"], m["kind"] = "portal", "request"     # "Request was created by staff": the request's creation record
            seen_request = True
        elif AUTO_ACK_RE.search(body):
            m["from"], m["kind"] = "portal", "ack"
        elif CLOSED_RE.search(body):
            m["from"], m["kind"], m["closes"] = "agency", "closure", True
            m["closes_text"] = re.sub(r"\s+", " ", CLOSED_RE.search(body).group(0)).strip()
        elif RELEASE_RE.search(body):
            m["from"], m["kind"] = "agency", "response"
        elif EXTENSION_RE.search(body) and not CYCLE_RE.search(body):
            m["from"], m["kind"] = "agency", "extension"
        elif CYCLE_RE.search(body):
            m["from"], m["kind"] = "agency", "cycle"
        else:
            m["from"], m["kind"] = "agency", "response"


def _fmt_toml_date(d: date) -> str:
    return d.isoformat()


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def add_pra_folder(cfg: Config, folder: str, *, agency: str, portal: str = "govqa", dry_run: bool = False) -> int:
    f = Path(folder).resolve()
    if not f.is_dir():
        print(f"not a folder: {f}")
        return 2
    ref = f.name
    key = f"pra-{ref}"
    aliases = {}
    pt = cfg.store / "parties.toml"
    if pt.exists():
        aliases = tomllib.loads(pt.read_text()).get("aliases", {})
    agency_id = aliases.get(agency, agency)
    gen_path = cfg.store / "sources" / "gen" / f"{key}.toml"
    old = tomllib.loads(gen_path.read_text()) if gen_path.exists() else {}

    meta = json.loads((f / "metadata.json").read_text()) if (f / "metadata.json").exists() else {}
    title = meta.get("title") or ref
    mh = next((p for p in f.iterdir() if p.name.endswith("_Message_History.pdf")), None)
    msgs = []
    if mh:
        chrono = list(reversed(parse_messages(message_history_text(f, mh))))
        classify(chrono)
        # keep n for messages already known, matched on their timestamp (unique within an export), so a better
        # classifier changes `kind`, never an id; new messages take the next free n
        known = {m["at"]: m["n"] for m in old.get("messages", [])}
        next_n = max([m["n"] for m in old.get("messages", [])] + [0]) + 1
        for m in chrono:
            k = m["at"].strftime("%Y-%m-%d %H:%M:%S")
            if k in known:
                m["n"] = known[k]
            else:
                m["n"] = next_n
                next_n += 1
        chrono.sort(key=lambda m: m["n"])
        msgs = chrono

    # the relative path of the folder inside the site checkout, as a repo: path
    rel = f
    if cfg.site_repo and f.is_relative_to(cfg.site_repo):
        rel = f.relative_to(cfg.site_repo)
    root = {
        "key": key, "kind": "pra", "ref": ref, "agency": agency_id, "portal": portal, "tz": "America/Los_Angeles",
        "path": f"repo:{rel}/", "access": "repo", "provenance": "release",
    }
    root["id"] = ids.root_id(root)
    opened = next((m["at"].date() for m in msgs if m["kind"] == "request"), None)
    closed = next((m["at"].date() for m in msgs if m.get("closes")), None)
    last_release = next((m for m in reversed(msgs) if m["kind"] == "response"), None)
    retrieved = old.get("retrieved")
    if not retrieved and mh:
        cd = pdf_info(mh).get("CreationDate", "")
        try:
            retrieved = datetime.strptime(cd[:24], "%a %b %d %H:%M:%S %Y").date()
        except ValueError:
            retrieved = date.today()
    retrieved = retrieved or date.today()

    # nodes: every file that is not a sidecar, metadata or our own notes
    old_nodes = {n.get("sha256"): n for n in old.get("files", []) if n.get("sha256")}
    nodes = []
    for p in sorted(f.iterdir()):
        if not p.is_file() or p.name in ("metadata.json",) or SIDECAR_RE.search(p.name) or p.name.startswith("."):
            continue
        sha = sha256_file(p)
        n = {"name": p.name, "id": ids.node_id(sha), "sha256": sha, "path": f"repo:{rel}/{p.name}", "bytes": p.stat().st_size}
        if p == mh:
            n["from"] = "portal"
            cd = pdf_info(p).get("CreationDate", "")
            try:
                n["captured"] = datetime.strptime(cd[:24], "%a %b %d %H:%M:%S %Y").date()
            except ValueError:
                pass
        else:
            n["from"] = old_nodes.get(sha, {}).get("from", "agency")
            o = old_nodes.get(sha, {})
            if o.get("produced"):
                n["produced"] = o["produced"]
                if o.get("message"):
                    n["message"] = o["message"]
                if o.get("produced_basis"):
                    n["produced_basis"] = o["produced_basis"]
            elif last_release:
                # the export does not say which message carried which file; the latest releasing message is the
                # best machine reading and is labeled as such — correct it by hand in the fact's note, not here
                n["produced"], n["message"], n["produced_basis"] = last_release["at"].date(), last_release["n"], "latest response message in the export"
            elif closed:
                n["produced"], n["produced_basis"] = closed, "closure date; no releasing message found"
        nodes.append(n)
    present = {n["sha256"] for n in nodes}
    for sha, n in old_nodes.items():
        if sha not in present:
            n = dict(n)
            n.setdefault("missing", date.today())
            nodes.append(n)

    out = []
    out.append(f"# Generated side of {key}, written by `nuggets sources add` on {date.today()} from {rel}/. Rewritten on every run;")
    out.append("# hand notes, holds and aid methods belong in the .hand.toml beside it (README → Generated and hand sides).")
    for k in ("id", "key", "kind"):
        out.append(f"{k:<6} = {_q(root[k])}")
    out.append("schema_version = 1")
    out.append(f"ref    = {_q(ref)}")
    out.append(f"title  = {_q(title)}")
    for k in ("agency", "portal", "tz", "path", "access", "provenance"):
        out.append(f"{k:<6} = {_q(root[k])}")
    out.append(f"retrieved = {_fmt_toml_date(retrieved)}")
    for m in msgs:
        out.append("")
        out.append("[[messages]]")
        out.append(f"n    = {m['n']}")
        out.append(f"at   = {_q(m['at'].strftime('%Y-%m-%d %H:%M:%S'))}")
        out.append(f"from = {_q(m['from'])}")
        out.append(f"kind = {_q(m['kind'])}")
        if m.get("closes"):
            out.append("closes = true")
        out.append(f"id   = {_q(ids.message_id(root, m['n']))}")
        out.append(f"in   = {_q(mh.name)}")
        pr = f"pages({m['page_from']}-{m['page_to']})" if m["page_from"] != m["page_to"] else f"page({m['page_from']})"
        out.append(f"pages = {_q(pr)}")
    for n in nodes:
        out.append("")
        out.append("[[files]]")
        out.append(f"name     = {_q(n['name'])}")
        out.append(f"id       = {_q(n['id'])}")
        out.append(f"sha256   = {_q(n['sha256'])}")
        if n.get("path"):
            out.append(f"path     = {_q(n['path'])}")
        if n.get("bytes") is not None:
            out.append(f"bytes    = {n['bytes']}")
        out.append(f"from     = {_q(n['from'])}")
        for k in ("produced", "captured", "missing"):
            if n.get(k):
                out.append(f"{k:<8} = {_fmt_toml_date(n[k]) if isinstance(n[k], date) else n[k]}")
        if n.get("message"):
            out.append(f"message  = {n['message']}")
        if n.get("produced_basis"):
            out.append(f"produced_basis = {_q(n['produced_basis'])}")
    if opened:
        out += ["", "[[status]]", f"at    = {_fmt_toml_date(opened)}", 'state = "opened"', 'basis = "message(1)"']
    if closed:
        cm = next(m for m in msgs if m.get("closes"))
        out += ["", "[[status]]", f"at    = {_fmt_toml_date(closed)}", 'state = "closed"', f'basis = "message({cm["n"]})"']
        if cm.get("closes_text"):
            out.append(f"note  = {_q(cm['closes_text'])}")
    text = "\n".join(out) + "\n"
    tomllib.loads(text)  # never write what the store cannot read
    if dry_run:
        print(text)
        return 0
    gen_path.parent.mkdir(parents=True, exist_ok=True)
    gen_path.write_text(text)
    print(f"wrote {gen_path.relative_to(cfg.store)}: {len(msgs)} messages, {len(nodes)} nodes")
    return 0
