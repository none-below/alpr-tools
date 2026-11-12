"""Identity: every id in the store derives from a seed, so the lint can re-derive and a hand-typed id cannot drift.

NS = uuid5(NAMESPACE_DNS, "nuggets.sm-alpr"). A file node's id is uuid5(NS, "sha256:<hex>"). A root's id is
uuid5(NS, <seed>) with the seed fixed per kind (the store's README, Ids). Message ids hang off the root seed.
"""

from __future__ import annotations

import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

NS = uuid.uuid5(uuid.NAMESPACE_DNS, "nuggets.sm-alpr")

# Query parameters that never identify a document; dropped by canonical_url.
TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid",
                   "mc_cid", "mc_eid", "ref", "igshid"}

KINDS_BY_SEED = {
    "pra": "ref", "muckrock": "ref", "snapshot": "ref", "recording": "ref", "notes": "ref", "survey": "ref",
    "sent": "ref", "email": "ref", "packet": "ref", "page": "url", "statute": "url", "article": "url",
}


def u5(seed: str) -> str:
    return str(uuid.uuid5(NS, seed))


def node_id(sha256_hex: str) -> str:
    return u5("sha256:" + sha256_hex.lower())


def canonical_url(url: str) -> str:
    """Lowercase scheme and host, drop the fragment and tracking parameters, keep query order, leave the path alone."""
    p = urlsplit(url.strip())
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, urlencode(q, doseq=True), ""))


def root_seed(root: dict) -> str:
    """The seed string for a generated root, from its kind and the keys the per-kind table names."""
    k = root["kind"]
    if k == "pra":
        return f"ref:pra:{root['agency']}/{root['ref']}"
    if k == "muckrock":
        n = root.get("ref") or str(root["key"]).removeprefix("mr-")
        return f"ref:muckrock:{n}"
    if k == "snapshot":
        return f"ref:portal:{root['agency']}@{root['retrieved']}"
    if k in ("recording", "notes", "survey", "sent"):
        d = root.get("meeting") or root.get("received") or root.get("visited")
        n = root.get("n")
        return f"ref:{k}:{root['agency']}@{d}" + (f"/{n}" if n else "")
    if k == "email":
        return f"ref:email:{root['message_id']}"
    if k == "packet":
        return f"ref:packet:{root['agency']}@{root['meeting']}/{root.get('doc', 'packet')}"
    # page, statute, article
    return "url:" + canonical_url(root["url"])


def root_id(root: dict) -> str:
    return u5(root_seed(root))


def message_id(root: dict, n: int) -> str:
    return u5(f"{root_seed(root)}/msg/{n}")


def party_id(kind: str, slug: str) -> str:
    return u5(f"{kind}:{slug}")
