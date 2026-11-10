"""Build a URL audit from immutable HTTP receipts; performs no network requests."""
import argparse
import hashlib
import json
from pathlib import Path

from bs4 import BeautifulSoup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt_dir", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    rows = []
    for collection in ("http-audit.json", "http-replacements.json", "http-updates.json"):
        for receipt in json.loads((args.receipt_dir / collection).read_text()):
            row = dict(receipt)
            row["receipt_collection"] = str(args.receipt_dir / collection)
            if receipt.get("body_file"):
                body = Path(receipt["body_file"]).read_bytes()
                row["sha256_verified"] = hashlib.sha256(body).hexdigest() == receipt["sha256"]
                soup = BeautifulSoup(body, "html.parser")
                row["page_title"] = soup.title.get_text(" ", strip=True) if soup.title else None
                row["http_success"] = receipt["status"] == 200
            rows.append(row)
    previous = [json.loads(line) for line in Path("raw/web/index.jsonl").read_text().splitlines()]
    targets = {row["url"] for row in rows}
    result = {
        "scope": "HTTP availability and page titles, not adjudication of factual claims",
        "receipt_dir": str(args.receipt_dir),
        "script": str(Path(__file__)),
        "rows": rows,
        "old_raw_web_direct_matches": [row for row in previous if row["url"] in targets],
        "limits": [
            "HTTP 200 does not validate claims or establish final procedural status.",
            "Indexed official DBR text is preserved in web-search2.json; direct TLS validation failed.",
            "A failed request does not establish that a document or decision does not exist.",
        ],
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
