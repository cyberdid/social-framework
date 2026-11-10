from pathlib import Path
import importlib.util
import pandas as pd
spec=importlib.util.spec_from_file_location('dynamics',Path('src/analysis/telegram_network_dynamics.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
e=pd.DataFrame([['a','b',1,'A','B'],['b','c',1,'B','C'],['a','c',9,'A','C']],columns=['from','to','n','from_group','to_group'])
nodes, transitions, stats=m.compute_graph(e)
# A high-volume direct a->c edge is still one step: treating n as distance would wrongly credit b.
assert nodes.loc[nodes.channel.eq('b'),'betweenness_directed_unweighted'].iat[0]==0
assert nodes.in_volume.sum()==11 and nodes.out_volume.sum()==11
assert stats['two_step_paths']==1 and stats['closed_two_step_paths']==1 and stats['two_step_closure_rate']==1
chain=e.iloc[:2];nodes,_,_=m.compute_graph(chain)
assert nodes.loc[nodes.channel.eq('b'),'betweenness_directed_unweighted'].iat[0]==0.5
p=pd.DataFrame({'t_kyiv':['2026-11-02T06:00:00+02:00','2026-11-02T06:01:00+02:00'],'narr':['N1_hostages,N2_death_torture','']})
b=m.narrative_bins(p,'2026-11-02T00:00:00+02:00','2026-11-02T12:10:13+02:00')
assert b.groupby('bin_start').messages.first().tolist()==[0,2,0]
assert b.loc[b.messages.eq(0),'share'].eq(0).all()
assert b.loc[(b.label=='N1_hostages') & b.messages.eq(2),'share'].iat[0]==0.5
assert b.partial_bin.sum()==5
print('PASS: shortcut versus chain; volume reconciliation; boundary bin; zero-denominator convention; multi-label denominator; partial bin.')
