"""nuggets — tools for the ALPR citable-fact store.

The store (facts, source registry, parties, vocabulary) lives in a separate private repo and is read by path;
nothing in this package carries data. See README.md and the store's own spec (its README) for the rules the
lint enforces. Stdlib only; DuckDB and PDF tools are optional and imported where used.
"""

__version__ = "0.1.0"
