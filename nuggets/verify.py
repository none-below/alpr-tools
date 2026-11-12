"""`verify <fact>`: resolve every locator of a fact against the bytes it cites, record the file's structure and
render what was looked at, so a later reader sees exactly what the verifier saw.

Writes `_build/verify/<fact id>.toml` (structure, artifacts, siblings) and PNG/CSV artifacts beside it. It never
edits the fact: `[verification]` in the fact file is a person's (on, how); the lint reads this sidecar for
`structure` and `artifacts`. Readers go to the original bytes — pymupdf/pdfinfo/pdftoppm for PDFs, openpyxl or a
zip+XML walk for workbooks, ffprobe for media — never to the audit DB, which is a finding step under rule 4.
"""

from __future__ import annotations

import csv
import hashlib
import re
import subprocess
import zipfile
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

from .config import Config
from .pra import pdf_info, sha256_file
from .store import Store, load_store, parse_locator


def _xlsx_structure(path: Path) -> dict:
    """Sheet names and non-empty row counts from the workbook's own XML, no third-party library needed."""
    out = {"sheets": {}}
    with zipfile.ZipFile(path) as z:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
              "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rid_to_target = {r.get("Id"): r.get("Target") for r in rels}
        for s in wb.find("m:sheets", ns):
            name = s.get("name")
            rid = s.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            target = rid_to_target.get(rid, "")
            member = "xl/" + target if not target.startswith("/") else target[1:]
            rows = 0
            with z.open(member) as fh:
                for _, el in ET.iterparse(fh, events=("end",)):
                    if el.tag.endswith("}row"):
                        if any(c.tag.endswith("}c") and len(c) for c in el):
                            rows += 1
                        el.clear()
            out["sheets"][name] = {"rows_nonempty_incl_header": rows}
    return out


def _xlsx_rows(path: Path, sheet: str, rows: list[int]) -> list[list[str]]:
    """The cells of the given 1-based workbook rows, as text, via the XML (shared strings resolved)."""
    with zipfile.ZipFile(path) as z:
        ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(ns + "si"):
                shared.append("".join(t.text or "" for t in si.iter(ns + "t")))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rid_to_target = {r.get("Id"): r.get("Target") for r in rels}
        member = None
        for s in wb.find(ns + "sheets"):
            if s.get("name") == sheet:
                rid = s.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                member = "xl/" + rid_to_target[rid]
        if member is None:
            raise ValueError(f"no sheet named {sheet!r}")
        want, got = set(rows), {}
        with z.open(member) as fh:
            for _, el in ET.iterparse(fh, events=("end",)):
                if el.tag == ns + "row":
                    r = int(el.get("r"))
                    if r in want:
                        cells = []
                        for c in el:
                            v = c.find(ns + "v")
                            t = c.get("t")
                            val = v.text if v is not None else ""
                            if t == "s" and val:
                                val = shared[int(val)]
                            elif t == "inlineStr":
                                val = "".join(x.text or "" for x in c.iter(ns + "t"))
                            cells.append(val or "")
                        got[r] = cells
                    el.clear()
                    if len(got) == len(want):
                        break
        return [got.get(r, []) for r in rows]


def _media_duration(path: Path) -> str | None:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, check=True)
        s = float(r.stdout.strip())
        return f"{int(s // 60)}:{s % 60:04.1f}"
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def _render_page(pdf: Path, page: int, out_dir: Path, stem: str) -> Path | None:
    out = out_dir / f"{stem}-p{page}"
    try:
        subprocess.run(["pdftoppm", "-r", "80", "-png", "-f", str(page), "-l", str(page), str(pdf), str(out)],
                       check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    cands = sorted(out_dir.glob(f"{stem}-p{page}*.png"))
    return cands[0] if cands else None


def _q(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def verify_fact(cfg: Config, fact_id: str, render: bool = True) -> int:
    store = load_store(cfg)
    fact = store.facts.get(fact_id)
    if not fact:
        print(f"no fact {fact_id!r}")
        return 2
    out_dir = cfg.build_dir / "verify" / fact_id.replace("/", "__")
    out_dir.mkdir(parents=True, exist_ok=True)
    structure, artifacts, siblings, problems = {}, [], {}, []
    for alias, s in fact.data.get("sources", {}).items():
        node = store.nodes.get(s.get("source", ""))
        if not node:
            problems.append(f"{alias}: source not registered")
            continue
        view = node.root.node_view(node)
        path = None
        try:
            path = cfg.resolve(view.get("path", "")) if view.get("path") else None
        except ValueError as e:
            problems.append(f"{alias}: {e}")
        if not path or not path.exists():
            for a in view.get("also", []):
                try:
                    cand = cfg.resolve(a)
                except ValueError:
                    continue
                if cand and cand.exists():
                    path = cand
                    break
        if not path or not path.exists():
            problems.append(f"{alias}: bytes not reachable from this machine ({view.get('path')})")
            continue
        actual = sha256_file(path)
        if actual != node.sha256:
            problems.append(f"{alias}: bytes at {path} hash {actual[:12]}…, registry says {node.sha256[:12]}…")
            continue
        suffix = path.suffix.lower()
        st = {"bytes": path.stat().st_size}
        if suffix == ".pdf":
            info = pdf_info(path)
            st.update({k.lower().replace(" ", "_"): v for k, v in info.items()
                       if k in ("Pages", "Creator", "Producer", "CreationDate", "ModDate", "Title")})
        elif suffix in (".xlsx", ".xlsm"):
            try:
                st.update(_xlsx_structure(path))
            except Exception as e:  # noqa: BLE001 — report, don't crash the run
                problems.append(f"{alias}: workbook structure: {e}")
        elif suffix in (".mp4", ".m4a", ".mp3", ".wav", ".mov"):
            d = _media_duration(path)
            if d:
                st["duration"] = d
        structure[alias] = st
        # siblings: files produced under the same message as this node
        msg = node.data.get("message")
        if msg is not None:
            sib = [n.name for n in node.root.nodes if n.data.get("message") == msg and n.id != node.id]
            if sib:
                siblings[alias] = sib
        if not render:
            continue
        try:
            terms = parse_locator(s.get("locator", ""))
        except ValueError as e:
            problems.append(f"{alias}: locator: {e}")
            continue
        stem = re.sub(r"[^A-Za-z0-9]+", "_", alias)
        sheet = next((t.value for t in terms if t.kind == "sheet"), None)
        for t in terms:
            if t.kind == "page" and suffix == ".pdf":
                png = _render_page(path, int(t.value), out_dir, stem)
                if png:
                    artifacts.append({"alias": alias, "locator": str(t), "file": png.name, "sha256": sha256_file(png), "renderer": "pdftoppm -r 80"})
            elif t.kind == "pages" and suffix == ".pdf" and re.fullmatch(r"\d+-\d+", t.value):
                a, b = map(int, t.value.split("-"))
                for p in range(a, min(b, a + 40) + 1):
                    png = _render_page(path, p, out_dir, stem)
                    if png:
                        artifacts.append({"alias": alias, "locator": f"page({p})", "file": png.name, "sha256": sha256_file(png), "renderer": "pdftoppm -r 80"})
            elif t.kind in ("row", "rows") and suffix in (".xlsx", ".xlsm") and sheet:
                if "-" in t.value:
                    a, b = map(int, t.value.split("-"))
                    rows = list(range(a, min(b, a + 200) + 1))
                else:
                    rows = [int(x) for x in t.value.split(";")]
                try:
                    data = _xlsx_rows(path, sheet, rows)
                except Exception as e:  # noqa: BLE001
                    problems.append(f"{alias}: rows: {e}")
                    continue
                dump = out_dir / f"{stem}-{t.kind}.csv"
                with dump.open("w", newline="") as fh:
                    w = csv.writer(fh)
                    for r, cells in zip(rows, data):
                        w.writerow([r, *cells])
                artifacts.append({"alias": alias, "locator": str(t), "file": dump.name, "sha256": sha256_file(dump), "renderer": "zip+xml row dump"})
    lines = [f"# written by `nuggets verify` on {date.today()}; the fact's own [verification] stays a person's", f"fact = {_q(fact_id)}", f"on = {date.today().isoformat()}", "schema_version = 1", ""]
    lines.append("[structure]")
    for alias, st in structure.items():
        parts = ", ".join(f"{k} = {_q(v) if isinstance(v, str) else (v if not isinstance(v, dict) else _q(str(v)))}" for k, v in st.items())
        lines.append(f"{alias} = {{ {parts} }}")
    if siblings:
        lines += ["", "[siblings]"]
        for alias, sib in siblings.items():
            lines.append(f"{alias} = [" + ", ".join(_q(x) for x in sib) + "]   # mark each opened | not relevant in the fact's how")
    for a in artifacts:
        lines += ["", "[[artifacts]]"] + [f"{k} = {_q(v)}" for k, v in a.items()]
    if problems:
        lines += ["", "[[problems]]"] + [f"text = {_q(p)}" for p in problems]
    (out_dir.parent / (fact_id.replace("/", "__") + ".toml")).write_text("\n".join(lines) + "\n")
    print(f"verify {fact_id}: {len(structure)} sources, {len(artifacts)} artifacts, {len(problems)} problems → {out_dir.parent / (fact_id.replace('/', '__') + '.toml')}")
    for p in problems:
        print("  problem:", p)
    return 1 if problems else 0
