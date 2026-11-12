# alpr-tools

Public code for the ALPR fact store ("nuggets"): the lint, builder, index and query runner, and the audit-log
loader once it moves here. The data — facts, the source registry, held material, the review trail — lives in a
separate private repo and is read by path; nothing here contains it.

Rules this repo keeps:

- **Synthetic fixtures only.** Tests run on invented stores under `tests/fixtures/`; no real production, request
  number, agency identifier or discrepancy appears here. The data repo's examples never come across.
- **No leads in commits.** Commit messages, issues and PRs describe mechanisms, never findings.
- **Pins name this repo.** A published number cites `alpr-tools@<commit>` and `query:alpr-tools/<name>@<commit>`.

Today (2026-11-12) it holds the `nuggets` package with `lint` (every rule the spec names, as code: ids re-derived
from their seeds, the generated/hand split, the locator grammar, alias and root rules, the vocabulary, the
verification gate) and the stubs of `sources add`, `verify`, `grab`, `build` and `query` that slice 1 fills in.
The schema doc for outsiders, with synthetic examples, is the next file to write; the working spec stays in
the data repo.

```
python -m pytest -q
python -m nuggets.cli --store <store dir> lint --warnings
```

Python 3.11+ (`tomllib`). License: AGPL-3.0, as the site repo.
