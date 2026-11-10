"""Export a reproducible random sample of relevant posts for manual (human/second-coder) validation
of the rule-based labels. The coder fills columns m_<label> with 1/0 WITHOUT looking at rule columns
(rule labels are written to a separate file).

Usage: python src/analysis/validation_sample.py <snapshot> [--n 60]
Out:   reports/<snap>/validation/sample_to_code.csv, rule_labels.csv
"""
import argparse
import csv
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from narratives import CODEBOOK, COUNTER_DEBUNK, DEBUNK, MEDIC_CLAIM, tag  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
LABELS = list(CODEBOOK) + ["medic_claim", "debunk", "counter_debunk"]

ap = argparse.ArgumentParser()
ap.add_argument("snapshot")
ap.add_argument("--n", type=int, default=60)
ap.add_argument("--seed", type=int, default=20261105)
ap.add_argument("--exclude", default=None, help="csv with url column to exclude (e.g. dev sample)")
ap.add_argument("--tag", default="")
a = ap.parse_args()
posts = [json.loads(l) for l in (ROOT / "data/processed" / a.snapshot / "posts_clean.jsonl").open()]
rel = [p for p in posts if p["relevant"] and len(p["text_clean"]) >= 40]
rnd = random.Random(a.seed)
if a.exclude:
    ex = {r["url"] for r in csv.DictReader(open(a.exclude))}
    rel = [p for p in rel if p["url"] not in ex]
sample = rnd.sample(rel, min(a.n, len(rel)))
out = ROOT / "reports" / a.snapshot / "validation"
out.mkdir(parents=True, exist_ok=True)
with (out / f"sample_to_code{a.tag}.csv").open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["i", "url", "text"] + [f"m_{k}" for k in LABELS])
    for i, p in enumerate(sample):
        w.writerow([i, p["url"], p["text_clean"][:1200]] + [""] * len(LABELS))
with (out / f"rule_labels{a.tag}.csv").open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["i", "url"] + LABELS)
    for i, p in enumerate(sample):
        t = p["text_clean"]
        cd = bool(COUNTER_DEBUNK.search(t))
        db = bool(DEBUNK.search(t)) and not cd
        row = {k: int(k in tag(t)) for k in CODEBOOK} | {"counter_debunk": int(cd), "debunk": int(db),
                                                          "medic_claim": int(bool(MEDIC_CLAIM.search(t)) and not db)}
        w.writerow([i, p["url"]] + [row[k] for k in LABELS])
print(f"{len(sample)} posts -> {out}")
