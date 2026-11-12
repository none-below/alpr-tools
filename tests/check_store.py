import sys, re, tomllib, pathlib, uuid
from urllib.parse import urlsplit, urlunsplit
root = pathlib.Path(sys.argv[1]); readme = (root/"README.md").read_text(); bad = 0
def fail(m):
    global bad; bad += 1; print("FAIL", m)
NSU = uuid.uuid5(uuid.NAMESPACE_DNS, "nuggets.sm-alpr"); U = lambda s: str(uuid.uuid5(NSU, s))
def canon(url):
    p = urlsplit(url); return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, ""))
def seed(d):
    k = d["kind"]
    if k == "pra": return f"ref:pra:{d['agency']}/{d['ref']}"
    if k == "muckrock": return "ref:muckrock:" + d["key"].removeprefix("mr-")
    if k == "snapshot": return f"ref:portal:{d['agency']}@{d['retrieved']}"
    if k in ("recording","notes","survey","sent"): return f"ref:{k}:{d['agency']}@{d['meeting']}"
    return "url:" + canon(d["url"])
ACCESS = {"public","repo","local"}; PROV = {"release","host","capture","sent","observation","leak"}
HOLDS = {"third-party-consent","gj-only","do-not-link"}
GEN_OWNED = {"id","key","kind","ref","url","path","access","provenance","retrieved","status"}
HAND_NODE_OK = {"sha256","note","hold","aid","variant","derived_from","method","findings_n","access","also","duration"}
roots, nodes, holds = {}, {}, {}
for p in sorted((root/"sources"/"gen").glob("*.toml")):
    d = tomllib.loads(p.read_text()); roots[d["id"]] = p.name
    if "schema_version" not in d: fail(f"{p.name}: no schema_version")
    if U(seed(d)) != d["id"]: fail(f"{p.name}: id does not derive from seed {seed(d)!r}")
    for m in d.get("messages", []):
        if U(f"{seed(d)}/msg/{m['n']}") != m["id"]: fail(f"{p.name}: message {m['n']} id does not derive")
    if d.get("provenance") not in PROV: fail(f"{p.name}: provenance {d.get('provenance')!r}")
    if d.get("access") not in ACCESS: fail(f"{p.name}: access {d.get('access')!r}")
    if "hold" in d: fail(f"{p.name}: [hold] on the generated side")
    keys = {}
    def walk(fs, n=0):
        for f in fs:
            n += 1
            if f.get("access") not in (None, *ACCESS): fail(f"{p.name}: node {f['name']} access {f.get('access')!r}")
            if f.get("id"): nodes[f["id"]] = (p.name, f["name"])
            else: print("WARN", p.name, "node without id:", f["name"])
            if f.get("sha256"):
                keys[f["sha256"]] = f["name"]
                if U("sha256:" + f["sha256"]) != f.get("id"): fail(f"{p.name}: node {f['name']} id does not derive from sha256")
            else: print("WARN", p.name, "node without sha256:", f["name"])
            n = walk(f.get("files", []), n)
        return n
    n = walk(d.get("files", []))
    hp = root/"sources"/(p.stem + ".hand.toml"); hold = ""
    if hp.exists():
        h = tomllib.loads(hp.read_text())
        if "schema_version" not in h: fail(f"{hp.name}: no schema_version")
        for k in h:
            if k in GEN_OWNED: fail(f"{hp.name}: sets generator-owned key {k}")
        if "hold" in h:
            holds[d["id"]] = h["hold"]["reason"]; hold = "HOLD:" + h["hold"]["reason"]
            if h["hold"]["reason"] not in HOLDS: fail(f"{hp.name}: hold reason {h['hold']['reason']!r} not in {sorted(HOLDS)}")
        ns = {m["n"] for m in d.get("messages", [])}
        for e in h.get("messages", []):
            if set(e) - {"n","note","hold"}: fail(f"{hp.name}: hand message entry sets {sorted(set(e)-{'n','note','hold'})}")
            if e.get("n") not in ns: fail(f"{hp.name}: hand message n={e.get('n')} matches nothing")
        for e in h.get("files", []):
            if set(e) - HAND_NODE_OK: fail(f"{hp.name}: hand node entry sets {sorted(set(e)-HAND_NODE_OK)}")
            k = e.get("sha256")
            if not k: fail(f"{hp.name}: hand entry without sha256"); continue
            if k not in keys: fail(f"{hp.name}: hand entry {k[:16]}… matches no generated node")
    print("root ok", p.name, f"{n} nodes", hold, "(+hand)" if hp.exists() else "")
for hp in sorted((root/"sources").glob("*.hand.toml")):
    if not (root/"sources"/"gen"/hp.name.replace(".hand.toml", ".toml")).exists(): fail(f"{hp.name}: hand file without a generated side")
parties = tomllib.loads((root/"parties.toml").read_text())
pids = {x["id"] for k in ("vendor","jurisdiction","person") for x in parties.get(k, [])}
for x in parties.get("person", []):
    if "/" in x["slug"]: fail(f"parties: person slug carries an agency: {x['slug']}")
    if U("person:" + x["slug"]) != x["id"]: fail(f"parties: person {x['slug']} id does not derive")
V = tomllib.loads((root/"vocab.toml").read_text()); vocab = set(V["topics"]["topic"]) | set(V["topics"]["vendor"])
for p0 in list((root/"sources").rglob("*.toml")) + list((root/"facts").rglob("*.toml")):
    for i, l in enumerate(p0.read_text().splitlines(), 1):
        if re.match(r"^\s*\w+\s*=\s*\"\"\s*(#.*)?$", l): fail(f"{p0.name}:{i}: empty string as unknown (use an absent key)")
        if re.match(r"^\s*(path|proof)\s*=\s*\"(?!repo:|private:|nas:|s3:|ia:|pra:|file:)", l): fail(f"{p0.name}:{i}: bare path without a scheme")
for p in sorted((root/"facts").rglob("*.toml")):
    d = tomllib.loads(p.read_text()); fid = d["id"]
    if fid != str(p.relative_to(root/"facts")).removesuffix(".toml"): fail(f"{p.name}: id != path")
    if "schema_version" not in d: fail(f"{fid}: no schema_version")
    h = p.read_text(); first = next((i for i, l in enumerate(h.splitlines()) if re.match(r"^\[\[?[A-Za-z_][A-Za-z0-9_.-]*\]\]?\s*(#.*)?$", l)), 10**9)
    for key in ("does_not_show", "related", "clear_plates", "speaker_basis"):
        for i, l in enumerate(h.splitlines()):
            if l.startswith(key + " ") and i > first: fail(f"{fid}: top-level key {key} after the first table (ordering trap)")
    srcs = d.get("sources", {}); used = set(d.get("in", []))
    for r in d.get("rows", []): used |= set(r.get("in", []))
    used |= set(re.findall(r"\[([a-z0-9-]+)\]", d.get("fact","")))
    for k, v in d.items():
        if k.endswith("_basis") and isinstance(v, str): used |= set(re.findall(r"\[([a-z0-9-]+)\]", v))
    for k in d:
        if k.endswith("_in"): used |= set(d[k])
    for a in used:
        if a not in srcs: fail(f"{fid}: alias [{a}] has no [sources.{a}]")
    for a, s in srcs.items():
        if a not in used: print("WARN", fid, f"alias {a} defined but unused")
        if not s.get("source"): fail(f"{fid}: alias {a}: empty source")
        elif s["source"] not in nodes: fail(f"{fid}: alias {a} source {s['source'][:8]}… not a registered node")
        if not s.get("root"): fail(f"{fid}: alias {a} names no root")
        elif s["root"] not in roots: fail(f"{fid}: alias {a} root {s['root'][:8]}… not a registered root")
        if s.get("root") in holds: print("INFO", fid, f"alias {a} inherits source hold {holds[s['root']]}")
    for t in d.get("topics", []):
        if t not in vocab: print("WARN", fid, "topic not in README vocabulary:", t)
    for pt in d.get("parties", []):
        if not re.fullmatch(r"[0-9a-f-]{36}", pt["id"]): fail(f"{fid}: party id not a UUID: {pt['id']}")
    if d.get("speaker"):
        if d["speaker"] not in pids: fail(f"{fid}: speaker not in parties.toml")
        if not d.get("speaker_basis"): fail(f"{fid}: speaker without speaker_basis")
    how = d.get("verification", {}).get("how")
    if d["status"] == "verified":
        if not isinstance(how, dict): fail(f"{fid}: verified but how is not per-alias")
        else:
            for a, v in how.items():
                pre = v.split(":")[0]
                if pre == "query" and not d.get("verification", {}).get("audit_db"): fail(f"{fid}: query-verified without audit_db")
                elif pre not in ("rendered","native","audio","query"): fail(f"{fid}: verified on a finding step: {a}={v[:30]}")
    print("fact ok", fid, d["status"], "aliases:", ",".join(srcs))
print("roots", len(roots), "nodes", len(nodes), "parties", len(pids), "holds", len(holds))
sys.exit(1 if bad else 0)
