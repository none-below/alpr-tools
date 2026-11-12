# alpr-tools

Public code for the ALPR fact store ("nuggets"): the lint, builder, index and query runner, and the audit-log
loader once it moves here. The data — facts, the source registry, held material, the review trail — lives in a
separate private repo and is read by path; nothing here contains it.

Rules this repo keeps:

- **Synthetic fixtures only.** Tests run on invented stores under `tests/fixtures/`; no real production, request
  number, agency identifier or discrepancy appears here. The data repo's examples never come across.
- **No leads in commits.** Commit messages, issues and PRs describe mechanisms, never findings.
- **Pins name this repo.** A published number cites `alpr-tools@<commit>` and `query:alpr-tools/<name>@<commit>`.

Today (2026-11-12) it holds the lint prototype: two checkers that validate a store's TOML, re-derive every id from
its seed, enforce the generated/hand split, the alias rules and the vocabulary. The schema doc for outsiders, with
synthetic examples, is the next file to write; the working spec stays in the data repo.

```
python -I tests/check_toml.py tests/fixtures/store/README.md tests/fixtures/store
python -I tests/check_store.py tests/fixtures/store
```

Python 3.11+ (`tomllib`). License: AGPL-3.0, as the site repo.
