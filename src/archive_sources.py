"""Archive primary/secondary sources about the incident: raw HTML (gzip) + sha256 + fetch time."""
import gzip, hashlib, json, sys, time
from datetime import datetime, timezone
from pathlib import Path
import requests
ROOT = Path(__file__).resolve().parents[1]
out = ROOT / "raw/web"; out.mkdir(parents=True, exist_ok=True)
urls = [l.strip() for l in (ROOT / "config/borshchahivka_sources.txt").read_text().splitlines() if l.strip() and not l.startswith("#")]
idx_p = out / "index.jsonl"
done = {json.loads(l)["url"] for l in idx_p.open()} if idx_p.exists() else set()
with idx_p.open("a") as idx:
    for u in urls:
        if u in done: continue
        try:
            r = requests.get(u, timeout=40, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) research-archive"})
            b = r.content; h = hashlib.sha256(b).hexdigest()
            (out / f"{h[:16]}.html.gz").write_bytes(gzip.compress(b))
            rec = {"url": u, "status": r.status_code, "final_url": r.url, "sha256": h, "file": f"{h[:16]}.html.gz", "bytes": len(b), "fetched_at": datetime.now(timezone.utc).isoformat()}
        except Exception as e:
            rec = {"url": u, "error": f"{type(e).__name__}: {e}", "fetched_at": datetime.now(timezone.utc).isoformat()}
        idx.write(json.dumps(rec, ensure_ascii=False) + "\n"); print(rec.get("status", rec.get("error")), u); time.sleep(1)
