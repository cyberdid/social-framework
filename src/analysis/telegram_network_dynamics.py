"""Describe observed Telegram forwards and six-hour lexical-label shares; no causal tests.

Run: python src/analysis/telegram_network_dynamics.py
Outputs: reports/telegram_network_dynamics_20261106/{CSV,JSON,PNG}.
"""
from pathlib import Path
import hashlib
import json
import platform

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter
import networkx as nx
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
START = '2026-11-02T00:00:00+02:00'
END = '2026-11-05T12:10:13+02:00'
LABELS = ['N0_official', 'N1_hostages', 'N2_death_torture', 'N3_tcc_camp', 'N4_cover_up']
LABEL_NAMES = ['N0: офіційне пояснення', 'N1: заручники / підвал', 'N2: смерть / катування', 'N3: табір / в’язниця', 'N4: приховування / «Хартія»']
GROUPS = ['A', 'B', 'C', 'D', 'E', 'U']
BLUE = '#25638B'
ORANGE = '#C86B25'


def compute_graph(edges):
    """from=visible attributed source, to=reposter; n is volume, never distance."""
    assert edges[['from', 'to', 'from_group', 'to_group']].notna().all().all()
    assert (edges.n > 0).all() and (edges.n == edges.n.astype(int)).all()
    assert not edges.duplicated(['from', 'to']).any(), 'Input must contain aggregated unique edges'
    groups = {}
    graph = nx.DiGraph()
    for source, target, n, source_group, target_group in edges.itertuples(index=False, name=None):
        for channel, group in [(source, source_group), (target, target_group)]:
            assert groups.get(channel, group) == group, f'Conflicting groups: {channel}'
            groups[channel] = group
        graph.add_edge(source, target, volume=int(n))
    betweenness = nx.betweenness_centrality(graph, normalized=True, weight=None, endpoints=False)
    raw_betweenness = nx.betweenness_centrality(graph, normalized=False, weight=None, endpoints=False)
    rows = []
    for channel in sorted(graph):
        ins = list(graph.in_edges(channel, data=True))
        outs = list(graph.out_edges(channel, data=True))
        rows.append(dict(channel=channel, group=groups[channel],
                         betweenness_directed_unweighted=betweenness[channel],
                         shortest_path_credit=raw_betweenness[channel],
                         in_degree=graph.in_degree(channel), out_degree=graph.out_degree(channel),
                         in_volume=sum(d['volume'] for _, _, d in ins),
                         out_volume=sum(d['volume'] for _, _, d in outs),
                         cross_group_in_volume=sum(d['volume'] for u, _, d in ins if groups[u] != groups[channel]),
                         cross_group_out_volume=sum(d['volume'] for _, v, d in outs if groups[v] != groups[channel])))
    nodes = pd.DataFrame(rows).sort_values(['betweenness_directed_unweighted', 'out_volume', 'channel'], ascending=[False, False, True])
    transitions = edges.groupby(['from_group', 'to_group']).agg(edges=('n', 'size'), volume=('n', 'sum')).reindex(pd.MultiIndex.from_product([GROUPS, GROUPS], names=['from_group', 'to_group']), fill_value=0).reset_index()
    two_steps = [(u, v, w) for u, v in graph.edges for w in graph.successors(v) if u != w]
    closed_two_steps = sum(graph.has_edge(u, w) for u, _, w in two_steps)
    stats = dict(nodes=graph.number_of_nodes(), edges=graph.number_of_edges(), volume=int(edges.n.sum()),
                 weak_components=nx.number_weakly_connected_components(graph),
                 strong_components=nx.number_strongly_connected_components(graph),
                 self_loops=nx.number_of_selfloops(graph),
                 two_step_paths=len(two_steps), closed_two_step_paths=closed_two_steps,
                 two_step_closure_rate=closed_two_steps/len(two_steps) if two_steps else 0.0,
                 cross_group_edges=int(edges.from_group.ne(edges.to_group).sum()),
                 cross_group_volume=int(edges.loc[edges.from_group.ne(edges.to_group), 'n'].sum()))
    assert nodes.in_volume.sum() == nodes.out_volume.sum() == stats['volume']
    assert nodes.in_degree.sum() == nodes.out_degree.sum() == stats['edges']
    assert transitions.volume.sum() == stats['volume']
    assert transitions.edges.sum() == stats['edges']
    assert nodes.betweenness_directed_unweighted.between(0, 1).all()
    return nodes, transitions, stats


def narrative_bins(posts, start=START, end=END):
    """Left-closed, right-open Kyiv bins; 0 denominator -> share 0 + explicit count."""
    times = pd.to_datetime(posts.t_kyiv, utc=True).dt.tz_convert('Europe/Kyiv')
    start, end = pd.Timestamp(start).tz_convert('Europe/Kyiv'), pd.Timestamp(end).tz_convert('Europe/Kyiv')
    assert times.between(start, end, inclusive='left').all()
    labels = posts.narr.fillna('').apply(lambda text: set(filter(None, str(text).split(','))))
    assert set().union(*labels).issubset(LABELS)
    starts = pd.date_range(start=start, end=end, freq='6h', inclusive='left')
    rows = []
    for left in starts:
        right = min(left + pd.Timedelta(hours=6), end)
        mask = times.ge(left) & times.lt(right)
        count = int(mask.sum())
        selected = labels.loc[mask]
        for label in LABELS:
            numerator = sum(label in lab for lab in selected)
            rows.append(dict(bin_start=left.isoformat(), bin_end=right.isoformat(),
                             bin_hours=(right-left).total_seconds()/3600,
                             partial_bin=right-left < pd.Timedelta(hours=6), label=label,
                             label_posts=int(numerator), messages=count,
                             share=numerator/count if count else 0.0,
                             empty_bin=count == 0,
                             unlabelled_messages=int(selected.apply(len).eq(0).sum())))
    result = pd.DataFrame(rows)
    assert result.groupby('bin_start', sort=False).messages.first().sum() == len(posts)
    assert result.label_posts.le(result.messages).all() and result.share.between(0, 1).all()
    for label in LABELS:
        assert result.loc[result.label.eq(label), 'label_posts'].sum() == sum(label in lab for lab in labels)
    return result


def save_plots(nodes, transitions, bins, out):
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 11, 'axes.spines.top': False, 'axes.spines.right': False, 'savefig.facecolor': 'white'})
    selected = nodes.head(10).iloc[::-1]
    fig, axes = plt.subplots(1, 2, figsize=(10.5, 5.5), gridspec_kw={'width_ratios': [1.45, 1]}, layout='constrained')
    axes[0].barh(selected.channel, selected.betweenness_directed_unweighted, color=BLUE)
    axes[0].set_xlabel('Нормована спрямована betweenness')
    axes[0].xaxis.set_major_formatter(PercentFormatter(1, decimals=1))
    axes[0].set_xlim(left=0)
    y=np.arange(len(selected))
    axes[1].barh(y-0.18, selected.in_volume, height=.34, color=BLUE, label='вхідні')
    axes[1].barh(y+0.18, selected.out_volume, height=.34, color=ORANGE, label='вихідні')
    axes[1].set_yticks(y, ['']*len(y)); axes[1].set_xlabel('Кількість видимих форвардів (n)')
    axes[1].legend(loc='lower right', frameon=False)
    fig.suptitle('Проміжні вузли спостереженого графа форвардів', fontsize=14)
    fig.savefig(out/'forward_bridges.png', dpi=220); plt.close(fig)

    matrix=transitions.pivot(index='from_group', columns='to_group', values='volume').reindex(index=GROUPS, columns=GROUPS)
    fig,ax=plt.subplots(figsize=(7.5,5.8), layout='constrained')
    im=ax.imshow(matrix.to_numpy(), cmap='Blues', vmin=0)
    for i in range(6):
        for j in range(6):
            val=int(matrix.iloc[i,j]);ax.text(j,i,str(val),ha='center',va='center',color='white' if val>matrix.to_numpy().max()*.6 else '#172B3C')
    ax.set_xticks(range(6), GROUPS);ax.set_yticks(range(6),GROUPS)
    ax.set_xlabel('Група репостера (to)');ax.set_ylabel('Група джерела (from)')
    ax.set_title('Видимі форварди між групами: обсяг n')
    fig.colorbar(im,ax=ax,label='Кількість форвардів',shrink=.8)
    fig.savefig(out/'group_transitions.png',dpi=220);plt.close(fig)

    base=bins.drop_duplicates('bin_start'); xs=np.arange(len(base))
    fig,axes=plt.subplots(6,1,figsize=(10,9),sharex=True,layout='constrained',gridspec_kw={'height_ratios':[1.1,1,1,1,1,1]})
    axes[0].bar(xs,base.messages,color=BLUE,width=.85);axes[0].set_ylabel('Дописи, n')
    for pos,count in zip(xs,base.messages):
        axes[0].text(pos,count+7,str(count),ha='center',va='bottom',fontsize=9)
    axes[0].set_ylim(0,base.messages.max()*1.2)
    axes[0].set_title('Шестигодинні біни: обсяг дописів і частки лексичних міток',fontsize=14)
    for i,(label,title) in enumerate(zip(LABELS,LABEL_NAMES),start=1):
        sub=bins[bins.label.eq(label)]
        axes[i].bar(xs,sub.share,color=ORANGE if label in LABELS[1:3] else BLUE,width=.85)
        axes[i].set_ylim(0,1.05);axes[i].set_yticks([0,.5,1]);axes[i].yaxis.set_major_formatter(PercentFormatter(1,decimals=0))
        axes[i].set_ylabel(title,rotation=0,ha='right',va='center',fontsize=10,labelpad=12)
    for ax in axes:
        ax.axvspan(len(xs)-1.5,len(xs)-.5,color='#F1E0C9',alpha=.5,zorder=-2)
        ax.grid(axis='y',alpha=.2);ax.set_axisbelow(True)
    tickpos=list(range(0,len(xs),2));labels=[pd.Timestamp(base.iloc[i].bin_start).strftime('%d.%m\n%H:%M') for i in tickpos]
    if len(xs)-1 not in tickpos:tickpos.append(len(xs)-1);labels.append(pd.Timestamp(base.iloc[-1].bin_start).strftime('%d.%m\n%H:%M'))
    axes[-1].set_xticks(tickpos,labels)
    axes[-1].set_xlabel('Час Києва; останній бін 05.11 12:00–12:10:13, неповний\nЧастки можуть перекриватися; мітка теми не визначає позицію автора')
    fig.savefig(out/'narrative_shares_6h.png',dpi=220);plt.close(fig)


def main():
    source=ROOT/'reports/continuation_20261105'
    out=ROOT/'reports/telegram_network_dynamics_20261106';out.mkdir(exist_ok=True)
    edges=pd.read_csv(source/'forwards_edges.csv')
    posts=pd.read_csv(source/'relevant_posts.csv')
    assert len(posts)==1408 and not posts.url.duplicated().any()
    nodes,transitions,stats=compute_graph(edges)
    bins=narrative_bins(posts)
    nodes.to_csv(out/'node_metrics.csv',index=False)
    transitions.to_csv(out/'group_transitions.csv',index=False)
    bins.to_csv(out/'narrative_shares_6h.csv',index=False)
    totals=bins.groupby('label').label_posts.sum().reindex(LABELS)
    total_posts=bins.drop_duplicates('bin_start').messages
    validation=ROOT/'reports/20261105T101013Z/validation/agreement_test_v2.csv'
    stats.update(relevant_posts=len(posts),narrative_bins=int(bins.bin_start.nunique()),
                 empty_bins=int((total_posts==0).sum()),unlabelled_posts=int(posts.narr.isna().sum()),
                 multilabel_posts=int(posts.narr.fillna('').str.contains(',').sum()),
                 label_counts={k:int(v) for k,v in totals.items()},
                 label_assignment_sum=int(totals.sum()),
                 window_start=START,window_end_exclusive=END,timezone='Europe/Kyiv',
                 final_partial_bin_hours=float(bins.iloc[-1].bin_hours),
                 graph_scope='Only endpoints of visible forwarded edges; excludes isolated channels.',
                 distance='Unweighted directed shortest paths; n used only as observed volume.',
                 labels_scope='Existing regex thematic labels, not stance; overlap permitted.',
                 granger={'run':False,'interpretation':'exploratory only; no p-values',
                          'reasons':['15 six-hour bins, with first empty bins and final partial bin',
                                     'single shared event, nonstationarity and common shocks',
                                     'weak validation of N1/N2 and other lexical labels',
                                     'panel coverage, copied text and aggregate graph do not identify operators or causal influence']},
                 validation_table=str(validation.relative_to(ROOT)),
                 input_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [source/'forwards_edges.csv',source/'relevant_posts.csv',validation]},
                 software={'python':platform.python_version(),'networkx':nx.__version__,'pandas':pd.__version__,'matplotlib':matplotlib.__version__},
                 checks={'unique_post_urls':True,'positive_integer_edge_volumes':True,'consistent_node_groups':True,
                         'in_out_degree_and_volume_reconcile':True,'group_matrix_reconciles':True,
                         'bin_denominators_reconcile':True,'label_totals_reconcile':True,
                         'empty_bin_zero_share_explicit':True,
                         'causality_or_operator_attribution_tested':False})
    (out/'results.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2)+'\n')
    save_plots(nodes,transitions,bins,out)
    print(json.dumps(stats,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
