"""Inspectable sensitivity audit: publication order, exact-similarity edges, coverage.

All estimates describe this collected corpus, not all Telegram or causal transfer.
"""
import csv
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median

from datasketch import MinHashLSH
from analyze import load, minhash, shingles, KYIV
from narratives import tag

ROOT = Path(__file__).resolve().parents[2]


def exact_edges(posts, minimum=0.6):
    candidates = [p for p in posts if len(p['text_clean']) >= 80]
    ix = MinHashLSH(threshold=0.5, num_perm=128)
    hashes, sets = {}, {}
    for i, p in enumerate(candidates):
        hashes[i] = minhash(p['text_clean'])
        sets[i] = shingles(p['text_clean'])
        ix.insert(i, hashes[i])
    edges = []
    for i, h in hashes.items():
        for j in ix.query(h):
            if j <= i or candidates[i]['channel'] == candidates[j]['channel']:
                continue
            score = len(sets[i] & sets[j]) / max(1, len(sets[i] | sets[j]))
            if score >= minimum:
                edges.append((i, j, score))
    return candidates, edges


def audit(name):
    _, posts, ch, groups = load(name)
    rel = sorted((p for p in posts if p['relevant']), key=lambda p: p['dt'])
    out = ROOT / 'reports' / name
    out.mkdir(parents=True, exist_ok=True)
    for p in rel:
        p['labels'] = tag(p['text_clean'])
    result = {'dataset': name, 'scope': 'observed collected publications; not a probability sample',
              'n_publications': len(posts), 'n_relevant': len(rel),
              'unknown_view_counters': sum(p.get('views') is None for p in rel),
              'sum_snapshot_view_counters': sum(p.get('views') or 0 for p in rel)}
    result['large_channel_sensitivity'] = []
    lo = datetime(2026, 11, 2, 13, 49, tzinfo=KYIV)
    hi = datetime(2026, 11, 2, 16, 14, tzinfo=KYIV)
    for threshold in [100000, 500000, 1000000]:
        big = [p for p in rel if p.get('subs') is not None and not math.isnan(p['subs']) and p['subs'] >= threshold]
        first = big[0] if big else None
        result['large_channel_sensitivity'].append({'threshold_subscribers': threshold,
            'first': None if first is None else {k: first[k] for k in ['url','channel','t_kyiv','subs']},
            'lull_counterexamples': [{k:p[k] for k in ['url','channel','t_kyiv','subs']} for p in big if lo <= p['dt'] < hi]})
    manual = list(csv.DictReader(l for l in (ROOT/'config/channel_groups.csv').open() if not l.startswith('#')))
    uncertain = {r['channel'].lower() for r in manual if re.search(r'attrib|contest',r['basis'],re.I)}
    auto = ROOT/'config/channel_groups_auto.csv'
    if auto.exists():
        uncertain.update(r['channel'].lower() for r in csv.DictReader(l for l in auto.open() if not l.startswith('#')))
    result['narrative_mention_sensitivity'] = {}
    for variant, ps in [('all_labels',rel),('without_contested_or_auto',[p for p in rel if p['channel'] not in uncertain])]:
        rows = []
        for label in ['N1_hostages','N2_death_torture','N3_tcc_camp','N4_cover_up','N0_official']:
            selected=[p for p in ps if label in p['labels']]
            views=Counter()
            for p in selected:views[p['group']] += p.get('views') or 0
            den=sum(views.values())
            rows.append({'label':label,'posts':len(selected),'sum_snapshot_views':den,
                         'view_shares_by_group':{g:v/den for g,v in views.items()} if den else {}})
        result['narrative_mention_sensitivity'][variant]=rows
    candidates, edges = exact_edges(rel)
    rows=[]
    for i,j,score in edges:
        a,b=sorted([candidates[i],candidates[j]],key=lambda p:p['dt'])
        rows.append({'earlier_url':a['url'],'later_url':b['url'],'earlier_channel':a['channel'],
                     'later_channel':b['channel'],'earlier_group':a['group'],'later_group':b['group'],
                     'earlier_time':a['t_kyiv'],'later_time':b['t_kyiv'],
                     'lag_minutes':(b['dt']-a['dt']).total_seconds()/60,'jaccard':score,
                     'earlier_text':a['text_clean'],'later_text':b['text_clean']})
    if rows:
        with (out/'verified_similarity_edges.csv').open('w') as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    sensitivity=[]
    c2lo=datetime(2026,11,2,18,30,tzinfo=KYIV)
    c2hi=datetime(2026,11,2,19,30,tzinfo=KYIV)
    headline=re.compile(r'концлагер[ья]\s+на\s+борщаговке|бунт\s+украинцев',re.I)
    result['c2_headline_matches']=[{k:p[k] for k in ['url','channel','group','t_kyiv','text_clean']}
                                   for p in rel if c2lo <= p['dt'] < c2hi and headline.search(p['text_clean'])]
    for threshold in [0.6,0.75,0.9]:
        adjacency=defaultdict(set)
        for i,j,score in edges:
            if score>=threshold:
                adjacency[i].add(j);adjacency[j].add(i)
        seen=set();components=[]
        for i in adjacency:
            if i in seen:continue
            stack=[i];seen.add(i);comp=[]
            while stack:
                a=stack.pop();comp.append(a)
                for b in adjacency[a]-seen:seen.add(b);stack.append(b)
            components.append(comp)
        families=[];firsts=Counter();c2=[]
        for comp in components:
            ps=sorted([candidates[i] for i in comp],key=lambda p:p['dt'])
            at_first={p['group'] for p in ps if p['dt']==ps[0]['dt']}
            firsts[next(iter(at_first)) if len(at_first)==1 else 'tie']+=1
            b=next((p for p in ps if p['group']=='B'),None)
            c=next((p for p in ps if p['group']=='C'),None)
            if b and c:
                sb,sc=shingles(b['text_clean']),shingles(c['text_clean'])
                score=len(sb&sc)/max(1,len(sb|sc))
                if score>=threshold:
                    families.append({'B_url':b['url'],'C_url':c['url'],'delta_B_minus_C_minutes':(b['dt']-c['dt']).total_seconds()/60,
                                     'direct_jaccard':score,'any_earlier_A_D':any(p['group'] in 'AD' and p['dt']<min(b['dt'],c['dt']) for p in ps)})
            cc=[p for p in ps if p['group']=='C' and c2lo<=p['dt']<c2hi]
            if cc:c2.append({'channels':len({p['channel'] for p in cc}),'urls':[p['url'] for p in cc],
                             'limitation':'connected component; all channel pairs need not exceed threshold'})
        plus=[f['delta_B_minus_C_minutes'] for f in families if f['delta_B_minus_C_minutes']>0]
        minus=[-f['delta_B_minus_C_minutes'] for f in families if f['delta_B_minus_C_minutes']<0]
        sensitivity.append({'threshold':threshold,'verified_pairs':sum(s>=threshold for _,_,s in edges),
                            'connected_text_components':len(components),'first_group':dict(firsts),
                            'directly_similar_first_B_C_families':families,
                            'B_later_n':len(plus),'B_later_median_min':median(plus) if plus else None,
                            'C_later_n':len(minus),'C_later_median_min':median(minus) if minus else None,
                            'c2_largest_components':sorted(c2,key=lambda r:r['channels'],reverse=True)[:3]})
    result['copy_sensitivity']=sensitivity
    result['copy_limitations']='LSH at0.5 may miss pairs; exact similarity validates retained edges only. Publication order is not causal transfer. Families are connected components; lag uses only directly similar earliest B/C pair.'
    (out/'audit_results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str))
    print(json.dumps({k:result[k] for k in ['dataset','n_publications','n_relevant','unknown_view_counters','sum_snapshot_view_counters']},ensure_ascii=False))
    print('copy threshold summaries',[(s['threshold'],s['verified_pairs'],s['B_later_n'],s['C_later_n']) for s in sensitivity])


if __name__=='__main__':
    audit(sys.argv[1] if len(sys.argv)>1 else 'continuation_20261105')
