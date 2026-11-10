# Social

Social is a modular Python research system for evidence-aware analysis of social conflicts and information incidents from digital traces. It combines platform-specific collection, versioned snapshots, text-similarity and timing analysis, an append-only evidence ledger, bounded retrieval and rule-based claim assessment, typed event organization, local media processing, and report quality gates.

This repository is the public code release that accompanies the preprint *Social: An Evidence-Grounded Framework for Automated Social Conflict and Information Incident Analysis*.

## What this release contains

- `src/`: collectors, snapshot preparation, analysis modules, the evidence ledger, media pipeline and workflow runners.
- `schemas/`: JSON Schemas for the typed records.
- `tests/`: regression and contract tests that run without private data.

## What it does not contain

The research corpora, comments, media, reports, curated case builders and channel classifications are not included, because they contain personal data or case-specific material about identifiable people. Tests that need those artifacts are removed or skipped with a stated reason. Results reported in the preprint therefore cannot be fully replayed from this repository alone.

## Status and limits

Social is research software, not a validated autonomous investigator. Passing tests establish software behavior, not factual accuracy. Semantic accuracy of claim assessment has not been measured against independently adjudicated data. The relation rule is a deterministic heuristic and its confidence values are not calibrated probabilities. Statistical departures from a null model do not establish coordination, inauthenticity or attribution. Some modules still contain incident-specific keywords and paths from the original case and need adaptation before reuse.

Use public data only, respect platform terms and applicable law, and do not use this software to profile or identify private individuals.

## Running the tests

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-contracts.txt pytest numpy pandas scipy matplotlib telethon requests beautifulsoup4 datasketch networkx rapidfuzz pymorphy3 pymorphy3-dicts-uk pymorphy3-dicts-ru reportlab qrcode curl_cffi yt-dlp pyyaml trafilatura instaloader pillow av==18.1.0
pytest -q
```

## License

Source code is licensed under the Apache License 2.0. See `LICENSE`.
