"""Reproducible literature search (OpenAlex + arXiv), records every query, date and hit count.
Free/open APIs, no keys. Output: raw/literature/<UTC>/{queries.jsonl,records.jsonl}
"""
import json, time, urllib.parse, urllib.request, xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UA = {"User-Agent": "Social-research-lit-search/0.1 (open-source scoping review)"}
THEMES = {
 "T1_telegram_waves": ["Telegram channels propaganda narrative analysis", "Telegram disinformation Ukraine war channels",
                       "information cascade diffusion Telegram forwarding network"],
 "T2_cib": ["coordinated inauthentic behavior detection", "coordinated behavior detection social media network synchronization",
            "copypasta near-duplicate text coordination detection"],
 "T3_bots": ["social bot detection features evaluation", "Botometer limitations false positives bot detection",
             "bot detection comments YouTube TikTok", "troll accounts detection state-sponsored"],
 "T4_out_of_context": ["out-of-context misinformation image caption detection", "cheapfakes miscaptioned images verification",
                       "video misattribution location verification misinformation"],
 "T5_content_analysis": ["intercoder reliability computational content analysis", "validating dictionary methods automated text analysis",
                         "LLM annotation reliability content analysis political text"],
 "T6_correction_exposure": ["misinformation correction debunking effectiveness meta-analysis", "exposure to misinformation measurement views reach",
                            "continued influence effect misinformation retraction"],
}
ARXIV = {"T2_cib": "coordinated inauthentic behavior", "T3_bots": "social bot detection", "T4_out_of_context": "out-of-context misinformation",
         "T1_telegram_waves": "Telegram propaganda", "T5_content_analysis": "LLM annotation content analysis reliability"}


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return r.read()


def abstract(inv):
    if not inv:
        return ""
    pos = sorted((i, w) for w, ii in inv.items() for i in ii)
    return " ".join(w for _, w in pos)


def main():
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "raw/literature" / now
    out.mkdir(parents=True)
    fq, fr = (out / "queries.jsonl").open("w"), (out / "records.jsonl").open("w")
    seen = set()
    for theme, qs in THEMES.items():
        for q in qs:
            url = ("https://api.openalex.org/works?" + urllib.parse.urlencode({
                "search": q, "per-page": 25, "filter": "from_publication_date:2015-01-01",
                "select": "id,doi,title,publication_year,cited_by_count,type,primary_location,abstract_inverted_index,authorships"}))
            d = json.loads(get(url))
            fq.write(json.dumps({"source": "openalex", "theme": theme, "query": q, "url": url, "count_total": d["meta"]["count"],
                                 "returned": len(d["results"]), "at": datetime.now(timezone.utc).isoformat()}) + "\n")
            for w in d["results"]:
                key = (w.get("doi") or w["id"]).lower()
                rec = {"source": "openalex", "theme": theme, "query": q, "key": key, "doi": w.get("doi"), "title": w.get("title"),
                       "year": w.get("publication_year"), "cited_by": w.get("cited_by_count"), "type": w.get("type"),
                       "venue": ((w.get("primary_location") or {}).get("source") or {}).get("display_name"),
                       "authors": [a["author"]["display_name"] for a in (w.get("authorships") or [])[:6]],
                       "abstract": abstract(w.get("abstract_inverted_index"))[:2500], "dup": key in seen}
                seen.add(key)
                fr.write(json.dumps(rec, ensure_ascii=False) + "\n")
            time.sleep(1)
    for theme, q in ARXIV.items():
        url = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode({"search_query": f'all:"{q}"', "max_results": 25, "sortBy": "relevance"})
        root = ET.fromstring(get(url))
        ns = {"a": "http://www.w3.org/2005/Atom"}
        ents = root.findall("a:entry", ns)
        fq.write(json.dumps({"source": "arxiv", "theme": theme, "query": q, "url": url, "returned": len(ents),
                             "at": datetime.now(timezone.utc).isoformat()}) + "\n")
        for e in ents:
            aid = e.find("a:id", ns).text.rsplit("/", 1)[-1].split("v")[0]
            key = f"arxiv:{aid}"
            fr.write(json.dumps({"source": "arxiv", "theme": theme, "query": q, "key": key, "doi": f"https://doi.org/10.48550/arXiv.{aid}",
                                 "title": " ".join(e.find("a:title", ns).text.split()), "year": int(e.find("a:published", ns).text[:4]),
                                 "authors": [x.find("a:name", ns).text for x in e.findall("a:author", ns)][:6],
                                 "abstract": " ".join(e.find("a:summary", ns).text.split())[:2500], "dup": key in seen}, ensure_ascii=False) + "\n")
            seen.add(key)
        time.sleep(3)
    fq.close(); fr.close()
    print(out)


if __name__ == "__main__":
    main()
