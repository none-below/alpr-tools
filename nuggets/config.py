"""Where things are: the store directory and the roots that scheme-prefixed paths resolve through.

The store is a directory holding `facts/`, `sources/gen/`, `sources/*.hand.toml`, `parties.toml`, `vocab.toml`
and `nuggets.toml`. `nuggets.toml` maps the schemes used in `path`/`also` values to machine roots, so moving a
checkout is an edit there, never in the registry.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SCHEMES = ("repo", "private", "nas", "s3", "ia", "pra", "file")


@dataclass
class Config:
    store: Path
    site_repo: Path | None = None
    private_root: Path | None = None
    nas_root: Path | None = None
    derived_root: Path | None = None
    audit_db: Path | None = None
    tools_repo: Path | None = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def load(cls, store: str | os.PathLike | None = None) -> "Config":
        root = Path(store or os.environ.get("NUGGETS_STORE") or ".").resolve()
        cfg = cls(store=root)
        f = root / "nuggets.toml"
        if f.exists():
            d = tomllib.loads(f.read_text())
            cfg.raw = d
            for k in ("site_repo", "private_root", "nas_root", "derived_root", "audit_db", "tools_repo"):
                if d.get(k):
                    setattr(cfg, k, Path(d[k]).expanduser())
        return cfg

    def resolve(self, value: str) -> Path | None:
        """Turn a scheme-prefixed path into a local Path, or None when the scheme has no local root here."""
        scheme, _, rest = value.partition(":")
        if scheme not in SCHEMES:
            raise ValueError(f"bare path without a scheme: {value!r}")
        if scheme == "repo" and self.site_repo:
            return self.site_repo / rest
        if scheme == "private" and self.private_root:
            return self.private_root / rest
        if scheme == "nas" and self.nas_root:
            return self.nas_root / rest
        if scheme == "file":
            return Path(rest)
        return None  # s3:, ia:, pra: are not local

    @property
    def build_dir(self) -> Path:
        return self.store / "_build"
