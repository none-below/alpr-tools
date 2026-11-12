# alpr-tools

Public code for the ALPR fact store ("nuggets"): the lint, the registrar, the verifier, the builder and the query
runner, and the audit-log loader once it moves here. The data — facts, the source registry, held material, the
review trail — lives in a separate private repo and is read by path; nothing here contains it.

Rules this repo keeps:

- **Synthetic fixtures only.** Tests run on invented stores under `tests/fixtures/`; no real production, request
  number, agency identifier or discrepancy appears here. The data repo's examples never come across.
- **No leads in commits.** Commit messages, issues and PRs describe mechanisms, never findings.
- **Pins name this repo.** A published number cites `alpr-tools@<commit>` and `query:<repo>/<name>@<commit>`.

## Commands (`python -m nuggets.cli --store <store dir> …`)

| command | what it does | writes |
|---|---|---|
| `lint [--warnings]` | every rule the spec names, as code: ids re-derived from their seeds, the generated/hand split, the locator grammar, alias and root rules, the vocabulary, the verification gate | nothing |
| `sources add <folder> --agency <alias> [--kind pra] [--dry-run]` | a GovQA request folder → the root's generated side: every file hashed and minted a node id, the Message_History export parsed into messages with page ranges and kinds, status edges | `sources/gen/<key>.toml` (rewritten; ids and `n` stable across runs) |
| `verify <fact> [--no-render]` | resolves each alias to its bytes, checks the hash, records structure (pages and PDF metadata, sheet row counts, media duration), renders every cited page or row, lists siblings produced under the same message | `_build/verify/<id>.toml` + PNG/CSV artifacts; never the fact |
| `grab <fact> [--for gj\|public]` | the citation block: full hashes, byte counts, root label, provenance phrase, locator (a `message(n)` resolves to the export's pages), a commit-pinned blob link, status edge, "also in", proof tier; two blocks by default, one audience with `--for`, a one-line refusal when nothing is citable | stdout |
| `build [--for gj\|public]` | the store as tables with a build stamp (data, site and tools commits; the audit DB's `code` and `state` pin); audience trees with holds and access applied and dropped pointers listed; prints what is newly public | `_build/facts.json`, `sources.json`, `stamp.json`, `facts.duckdb` (`facts`, `fact_parties`, `fact_sources`, `fact_values`, `contracts`), `for-<audience>/` |
| `query <name> [--since] [--summary]` | a named query (`queries/<name>.toml`, store first, then this repo) read-only against the audit DB with `facts.duckdb` attached; the summary carries the pin, the facts joined (with content hashes) and the releases the count rests on | `_build/queries/<name>.csv`, `.periods.csv`, `.summary.md` |

The store directory holds `nuggets.toml` (machine roots for the `repo:`, `private:`, `nas:` … path schemes),
`vocab.toml`, `parties.toml`, `facts/`, `sources/gen/` and `sources/*.hand.toml`; the spec is its README.

```
uv run --python 3.12 --with pytest --with duckdb python -m pytest -q
uv run --python 3.12 python -m nuggets.cli --store <store dir> lint --warnings
uv run --python 3.12 --with duckdb==1.5.5 python -m nuggets.cli --store <store dir> build
```

`build` and `query` need DuckDB at the audit DB's own minor version (the `db` extra pins it). `verify` and
`sources add` call poppler's `pdfinfo`, `pdftotext` and `pdftoppm`, and `ffprobe` for media. Python 3.11+
(`tomllib`). License: AGPL-3.0, as the site repo.
