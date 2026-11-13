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
| `coverage [--producer <LIKE>]` | which agency's logs are held for which months, from the audit DB itself: per agency, audit kind and month, the releases holding rows, what they observe (first and last instant, rows, distinct IDs, duplicate rows) and what they claim by file or sheet name; marks full / partial / claimed-empty / hole and overlapping releases. `query` prints its window's coverage from this, so a count reads as a floor with a denominator | `_build/coverage/coverage.md`, `coverage.csv`, `coverage.releases.csv` |
| `releases twins --producer <LIKE>` | finds the same released table loaded through two of our own paths (same request, workbook and sheet under different release-id prefixes) and compares the two row sets cell by cell: raw differences, differences left after trimming whitespace and treating an empty cell as missing, rows one side lacks; verdict per pair. The report is the basis a removal judgment cites; nothing is removed | `_build/releases/twins.md`, `twins.json` |
| `sharing <agency> [--tz <zone>]` | an agency's Flock transparency portal sharing lists, snapshot by snapshot, as one span per partner and direction: a run of captures that list the partner, bracketed by the captures that did not (`>d1, <=d2` … `>=d3, <d4`), open at the latest observation otherwise; a capture that did not show the section observes nothing. Partner names resolve through the registry; an unknown name gets a derived id. Observed spans only: the generated span facts of "Sharing at scale" are built from this | `_build/by-agency/<slug>.sharing.md`, `.sharing.json` |
| `query <name> [--since] [--param k=v]… [--summary]` | a named query (`queries/<name>.toml`, store first, then this repo) read-only against the audit DB (truth, and derived when it is built beside it) with `facts.duckdb` attached. Every `sql_*` key is a further block; `--param` overrides a `[params]` value for the run and the output stem carries it. The summary carries the pin (loader code, state, the derived linking build when read, the query's and runner's commits, the parameters), the facts joined (with content hashes) and the releases the count rests on, with truth's flag for a disagreeing second row count | `_build/queries/<stem>.csv`, one CSV per `sql_*` block, `.summary.md` |

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
