"""CPU-only, explainable candidate discovery; never assigns factual truth.

Default: python -m src.analysis.contextual_candidates
Other platforms: --input comments.jsonl --known-cases cases.json --output-prefix data/processed/comment_candidates
JSONL accepts platform, id/comment_id, text, date, url and optional context_place,
context_event_date, reference_event_date. Dates are compared only when both event
fields are supplied; publication time alone is never treated as filming time.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / 'data/processed/continuation_20261105/posts_clean.jsonl'
VERSION = '1.1'

def rx(value):
    return re.compile(value, re.I | re.S)

PLACE = rx(r'\b(?:борщаг[іи]в\w*|борщагов\w*|петропавл[іи]вськ\w*|петропавловск\w*)')
SOF = rx(r'\b(?:соф[іи][їе]вськ\w*|софиевск\w*)\b(?:\s+борщаг\w*)?')
PETRO = rx(r'\b(?:петропавл[іи]вськ\w*|петропавловск\w*)\b')
ANCHOR = rx(r'\b(?:борщаг\w*|петропавл\w*|катівн\w*|пыточн\w*|підвал\w*|подвал\w*|заручник\w*|заложник\w*|буданов\w*|півненк\w*|пивненк\w*|азов\w*|тцк)\b|мед[ -]престиж|на\s+кол[іе]н\w*')
CONCEAL_ACTION = r'\b(?:знищ\w*|уничтож\w*|прибир\w*|прибрат\w*|прибрал\w*|зачист\w*|вимива\w*|вымыва\w*|зам[іи]та\w*|замітал\w*|зам[еі]сти|замел[аио]?|замет[аеёуи]\w*|прихов\w*|скры\w*)\b'
CONCEAL_OBJECT = r'\b(?:доказ\w*|улик\w*|слід(?:и|ів|ами|ах)|след(?:ы|ов|ами|ах))\b'
CONCEAL = rx(CONCEAL_ACTION + r'[^.!?\n]{0,100}?' + CONCEAL_OBJECT + '|' +
             CONCEAL_OBJECT + r'[^.!?\n]{0,100}?' + CONCEAL_ACTION)
CLEANUP = rx(r'\b(?:прибрал\w*|прибрат\w*|прибир\w*|почист\w*|зачист\w*)\b')
CLEANUP_CONTEXT = rx(r'\b(?:поки|доки|пока|все|всё|всі|стільки\s+часу|з\s*ранку|за\s+\d+\s*(?:год|час))\b')
FIRE = rx(r'\b(?:якщо|если|можуть|могут|може|может)\b.{0,110}?\b(?:спалах\w*|згор\w*|сгор\w*|підпал\w*|подож\w*|зажг\w*)\b')
OLD = r'\b(?:старе|старі|старий|старих|старого|старое|старые|старый|старая|старую|старой|старых|торішн\w*|прошлогодн\w*|архівн\w*|архивн\w*|підроблен\w*|поддельн\w*)\b'
MEDIA = r'\b(?:відео|видео|кадр\w*|фото)\b'
OLD_MEDIA = rx(OLD+r'.{0,35}?'+MEDIA+'|'+MEDIA+r'.{0,35}?'+OLD+r'|\b(?:перепідпис\w*|переподпис\w*|вирван\w*\s+з\s+контекст\w*|вырван\w*\s+из\s+контекст\w*)\b')
MEDIC = rx(r'\b(?:медик\w*|псевдомедик\w*|лікар\w*|врач\w*|швидк(?:а|ої|у|ою|ій)|скорая|скорой|скорую)\b')
MEDICAL_ISSUE = rx(r'\b(?:псевдомедик\w*|не\s+медик\w*|не\s+лікар\w*|не\s+врач\w*|ліценз\w*|лиценз\w*|не\s+працю\w*|не\s+работ\w*|(?:неіснуюч\w*|несуществующ\w*).{0,25}(?:швидк\w*|скор\w*|медслужб\w*|клин\w*|служб\w*)|справжн\w*\s+медик\w*|настоящ\w*\s+(?:медик\w*|врач\w*))\b')
CORRECTION = rx(r'\b(?:це|это)\s+не\b.{0,45}?борщаг\w*|\b(?:а\s+не|а\s+ни|не\s+з|не\s+из)\b.{0,45}?(?:петропавл\w*|соф[іи]\w*|борщаг\w*)|\b(?:соф[іи]\w*|петропавл\w*)\b.{0,45}?\b(?:а\s+не|а\s+ни)\b')


def detect(text, context=None):
    """Return exact text spans and explicit context evidence, with no stance inference."""
    text = text or ''
    context = context or {}
    hits = []
    local_anchor = bool(ANCHOR.search(text))
    parent_anchor = bool(ANCHOR.search(context.get('context_text') or ''))
    # Analyst-selected review scope is a retrieval aid, never a verified location.
    anchored = local_anchor or parent_anchor or bool(context.get('review_scope'))
    def add(rule, match, **extra):
        hits.append(dict(rule=rule, start=match.start(), end=match.end(), match=match.group(), **extra))
    for m in CONCEAL.finditer(text):
        if anchored or context.get('context_place'):
            add('evidence_concealment', m)
    if anchored and not CONCEAL.search(text) and CLEANUP_CONTEXT.search(text):
        for m in CLEANUP.finditer(text):
            add('implicit_cleanup_allusion', m,
                limitation='Cleanup allusion only; object, purpose and factual truth are not established.')
    for m in FIRE.finditer(text):
        if anchored:
            add('conditional_fire', m)
    for m in OLD_MEDIA.finditer(text):
        if anchored:
            add('old_or_recaptained_media', m)
    if anchored and (MEDIC.search(text) or (parent_anchor and MEDIC.search(context.get('context_text') or ''))):
        for m in MEDICAL_ISSUE.finditer(text):
            generic_nonwork = bool(rx(r'^не\s+(?:працю\w*|работ\w*)$').fullmatch(m.group()))
            if (parent_anchor and not generic_nonwork) or any(abs(m.start()-a.start()) <= 180 for a in MEDIC.finditer(text)):
                add('medical_identity_dispute', m)
    for m in SOF.finditer(text):
        add('sofiivska_context', m)
    if PLACE.search(text) or SOF.search(text) or PETRO.search(text):
        for m in CORRECTION.finditer(text):
            add('geographic_correction', m)
    context_place = context.get('context_place') or ''
    if SOF.search(context_place):
        reference = rx(r'\b(?:сліди|слідів|следы|следов|катівн\w*|пыточн\w*|пытачн\w*|підвал\w*|подвал\w*|катуван\w*|замітання|зам[іи]та\w*|заме(?:та|ли|сти)\w*|буданов\w*|лжемедик\w*)\b|концы\s+в\s+огонь|не\s+там|інше\s+місце|другая\s+борщаг\w*')
        for m in reference.finditer(text):
            add('incident_context_reference', m, evidence_fields={'context_place': context_place}, limitation='May be quotation, question, rebuttal, sarcasm or another incident; manual stance required.')
    if SOF.search(context_place) and (PETRO.search(text) or (CONCEAL.search(text))):
        m = PETRO.search(text) or CONCEAL.search(text)
        add('geographic_context_conflict', m, evidence_fields={'context_place': context_place},
            limitation='Review requested: concealment allusion does not explicitly name Petropavlivska.')
    a, b = context.get('reference_event_date'), context.get('context_event_date')
    if a and b and re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(a)) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(b)) and a != b:
        hits.append(dict(rule='explicit_event_date_discrepancy', start=None, end=None, match=None,
                         evidence_fields={'reference_event_date': a, 'context_event_date': b}))
    return hits


def normalized_text(text):
    return re.sub(r'\W+', ' ', (text or '').casefold()).strip()


def scan(input_paths, output_prefix, known_cases=None):
    known_cases = known_cases or {}
    records, input_receipts, seen = [], [], set()
    for path in map(Path, input_paths):
        blob = path.read_bytes()
        input_receipts.append({'path': str(path), 'sha256': hashlib.sha256(blob).hexdigest(), 'bytes': len(blob)})
        for lineno, line in enumerate(blob.decode('utf-8').splitlines(), 1):
            if not line.strip():
                continue
            p = json.loads(line)
            text = p.get('text_clean') if p.get('text_clean') is not None else (p.get('text') or '')
            platform = p.get('platform') or 'telegram'
            native_id = p.get('id') or p.get('comment_id') or p.get('post_key') or f"{p.get('channel','')}/{p.get('msg_id','')}"
            identity = p.get('url') or f'{platform}:{native_id}'
            stable = hashlib.sha256(f'{platform}\0{identity}\0{text}'.encode()).hexdigest()[:24]
            if stable in seen:
                continue
            seen.add(stable)
            context = dict(known_cases.get(identity) or known_cases.get(p.get('video_url')) or {})
            context.update({key: p[key] for key in ('context_place','context_text','review_scope','context_event_date','reference_event_date') if p.get(key)})
            records.append(dict(candidate_id=stable, platform=platform, native_id=native_id,
                                url=p.get('url'), video_url=p.get('video_url'), parent_id=p.get('parent_id'),
                                date=p.get('date') or p.get('created_at_utc'), t_kyiv=p.get('t_kyiv'),
                                channel=p.get('channel'), text=text, text_raw=p.get('text'),
                                rules=detect(text, context), original_relevant=p.get('relevant'),
                                place_mentioned=bool(PLACE.search(text)),
                                context_only=not bool(p.get('relevant')),
                                reference_flags={'reply_to_url':p.get('reply_to_url'), 'forward_url':p.get('fwd_from_url'),
                                                 'linked_urls':p.get('links') or p.get('urls') or [],
                                                 'known_case':context or None},
                                source_file=str(path), source_line=lineno,
                                sources=p.get('sources') or [], manual_status='unreviewed'))
    # Exact normalized repeated text, only among otherwise selected candidates.
    groups = defaultdict(list)
    for p in records:
        if p['rules'] and len(normalized_text(p['text'])) >= 50:
            groups[normalized_text(p['text'])].append(p)
    repeated_groups = 0
    for normalized, members in groups.items():
        if len({p['url'] or p['native_id'] for p in members}) > 1:
            repeated_groups += 1
            gid = hashlib.sha256(normalized.encode()).hexdigest()[:16]
            for p in members:
                p['rules'].append(dict(rule='repeated_candidate_text', start=0, end=len(p['text']), match=p['text'],
                                       repeat_group_id=gid, repeat_group_size=len(members),
                                       limitation='Text repetition is not evidence of coordination or falsity.'))
    candidates = sorted((p for p in records if p['rules']), key=lambda p:(p.get('date') or '',p['candidate_id']))
    prefix = Path(output_prefix)
    prefix.parent.mkdir(parents=True,exist_ok=True)
    jsonl = prefix.with_suffix('.jsonl')
    with jsonl.open('w',encoding='utf-8') as f:
        for p in candidates:
            f.write(json.dumps(p,ensure_ascii=False)+'\n')
    fields = ['candidate_id','platform','native_id','url','date','t_kyiv','channel','original_relevant','place_mentioned','context_only','rules','reference_flags','text','source_file','source_line']
    with prefix.with_suffix('.csv').open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for p in candidates:
            w.writerow({k:json.dumps(p[k],ensure_ascii=False) if k in ('rules','reference_flags') else p.get(k) for k in fields})
    counts=Counter(rule for p in candidates for rule in {h['rule'] for h in p['rules']})
    summary=dict(version=VERSION,created_at=datetime.now(timezone.utc).isoformat(),inputs=input_receipts,
                 unique_input_records=len(records),candidates=len(candidates),
                 previously_relevant=sum(p['original_relevant'] is True for p in candidates),
                 previously_not_relevant=sum(p['original_relevant'] is False for p in candidates),
                 rule_counts=dict(sorted(counts.items())),repeated_groups=repeated_groups,
                 counts_note='Rule counts overlap; not counts of misinformation or unique viewers.',
                 known_cases_supplied=bool(known_cases))
    prefix.with_suffix('.summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',action='append',type=Path,help='Repeat for multiple JSONL files')
    parser.add_argument('--output-prefix',type=Path,default=ROOT/'data/processed/contextual_candidates')
    parser.add_argument('--known-cases',type=Path,help='JSON object keyed by record URL with context_place/event dates/case URL')
    args=parser.parse_args()
    cases=json.loads(args.known_cases.read_text()) if args.known_cases else None
    print(json.dumps(scan(args.input or [DEFAULT_INPUT],args.output_prefix,cases),ensure_ascii=False,indent=2))

if __name__=='__main__':
    main()
