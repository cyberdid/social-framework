"""Service-role career of public officials from public NAZK declarations (Ukraine), declarant only.

For each official in the config, list declarations by name, keep those whose workplace matches the role
regex, group by patronymic (to separate namesakes), and extract only what describes the public role:
year, declaration type, workplace, position, and the declarant's own declared income by type.
Monetary assets, property, vehicles, interests and anything about family members are NOT extracted.
Raw documents are cached locally (gzip + sha256) for audit.

Config (JSON): {"officials": [{"key": "...", "query": "Прізвище Ім'я", "workplace_regex": "...",
                               "official_role": "...", "role_source": "URL or document"}]}
Usage: python -m social.collectors.declarations_ua --config officials.json --raw data/raw/nazk --out data/processed/declarations.json
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

API = "https://public-api.nazk.gov.ua/v2/documents"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) research-archive"}
TYPES = {0: "щорічна(стара схема)/інша", 1: "щорічна", 2: "перед звільненням", 3: "після звільнення", 4: "кандидата"}


def get(url, **params):
    last = None
    for i in range(6):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=60)
            if r.status_code == 200:
                time.sleep(3)
                return r
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = f"{type(e).__name__}"
        time.sleep(30 * (i + 1))
    raise RuntimeError(f"NAZK request failed: {url} ({last})")


class Cached:
    def __init__(self, raw):
        self.content = raw

    def json(self):
        return json.loads(self.content)


def own_income(step):
    out = defaultdict(int)
    for r in (step or {}).get("data") or []:
        owners = [p.get("person") for p in r.get("person_who_care") or []]
        if r.get("person") is not None:  # schema used in 2015-2018 declarations
            owners.append(r.get("person"))
        if "1" in owners:
            try:
                out[r.get("objectType") or "?"] += int(float(str(r.get("sizeIncome") or 0).replace(",", ".")))
            except ValueError:
                out["unparsed"] += 1
    return dict(out)



def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--raw", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    a.raw.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(a.config.read_text())
    report = {"script": "social.collectors.declarations_ua", "generated_at": datetime.now(timezone.utc).isoformat(),
              "scope": "service role only: workplace, position, declarant own income; no assets, property, interests or family", "officials": []}
    for o in cfg["officials"]:
        rx = re.compile(o["workplace_regex"], re.I)
        lst = get(API + "/list", query=o["query"]).json()
        matches = []
        for it in lst.get("data", []):
            s1 = it.get("data", {}).get("step_1", {}).get("data", {})
            if rx.search(s1.get("workPlace") or ""):
                matches.append(it)
        by_mid = defaultdict(list)
        for it in matches:
            cache = a.raw / f"{it['id']}.json.gz"
            if cache.exists():
                doc = Cached(gzip.decompress(cache.read_bytes()))
            else:
                doc = get(f"{API}/{it['id']}")
                cache.write_bytes(gzip.compress(doc.content))
            raw = doc.content
            d = doc.json()
            data = d.get("data", {})
            s1 = data.get("step_1", {}).get("data", {})
            by_mid[(s1.get("middlename") or "").upper()].append({
                "id": d["id"], "url": f"https://public.nazk.gov.ua/documents/{d['id']}",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "year": d.get("declaration_year"), "type": TYPES.get(d.get("declaration_type"), d.get("declaration_type")),
                "submitted": d.get("date"), "workPlace": s1.get("workPlace"), "workPost": s1.get("workPost"),
                "responsiblePosition": s1.get("responsiblePosition"),
                "own_income_by_type_uah": own_income(data.get("step_11")),
            })
        persons = [{"patronymic": mid, "declarations": sorted(v, key=lambda x: (x["year"] or 0, x["submitted"] or ""))}
                   for mid, v in by_mid.items()]
        report["officials"].append({
            **{k: o.get(k) for k in ["key", "official_role", "role_source", "name_source"]},
            "registry_hits": lst.get("count"), "workplace_matches": len(matches),
            "distinct_patronymics": len(persons),
            "identity_status": ("no_public_declaration_identified" if not persons else
                                "single_candidate" if len(persons) == 1 else "ambiguous_multiple_candidates"),
            "candidates": persons})
        print(o["key"], lst.get("count"), len(matches), [p["patronymic"] for p in persons], flush=True)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
