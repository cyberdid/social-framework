"""Reproducible caption profile and candidate contexts; NOT automatic stance coding.
Run with a raw subtitle snapshot. Manual decisions are a separate reviewed CSV.
"""
import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from youtube_subtitles_collect import parse_vtt, sha

TOPICS = {
    'tcc_mobilization': r'тцк|мобіліз|мобилиз|военком',
    'detention_basement': r'підвал|подвал|заручник|заложник|утримува|удержива|полон|плен',
    'torture_abuse': r'катув|\bпыт(?:к|оч|ок|а(?:л(?!с)|ют|ть))|тортур|знуща|издева|побит|\bбили\b|\bбить\b',
    'concentration_camp': r'концтаб|концлаг',
    'military_unit_official': r'нацгвар|військовослужб|военнослуж|військової частини|азов|офіційн|официальн',
    'medical_witness_40': r'медик|медичн|медицин|швидк|скор(?:ая|ой|ую)|сорок|\b40\b',
    'disinformation_provocation': r'фейк|дезінформ|дезинформ|іпсо|ипсо|провокац|російськ\w*.{0,30}(?:операц|пропаганд)',
    'investigation_accountability': r'розсліду|расслед|слідств|следств|перевір|проверк|дбр|кримінальн|уголовн',
}

def csv_write(path, rows, fields):
    with path.open('w') as f:
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(rows)

def analyze(snapshot, output):
    output.mkdir(parents=True,exist_ok=True)
    selection=list(csv.DictReader((snapshot/'selection.csv').open()))
    metadata={r['video_id']:r for r in selection}
    receipts=[json.loads(l) for l in (snapshot/'receipts.jsonl').open()]
    transcripts,contexts=[],[]
    for receipt in receipts:
        if receipt['status']!='ok':continue
        source=snapshot/receipt['file']
        assert sha(source)==receipt['sha256'], source
        cues=parse_vtt(source.read_text())
        vid=receipt['video_id']; text=' '.join(c['text'] for c in cues)
        for path,payload in [(output/f'{vid}.transcript.json',json.dumps(cues,ensure_ascii=False,indent=2)), (output/f'{vid}.transcript.txt',text+'\n')]:
            if path.exists():
                assert path.read_text()==payload, f'Existing transcript would change: {path}'
            else:
                path.write_text(payload)
        row={k:metadata[vid][k] for k in ['video_id','views_rank','upload_date','format_group','selection_reason','url','title']}
        row.update(kind=receipt['kind'],language=receipt['language'],cues=len(cues),characters=len(text),
                   duration_covered=max([c['end'] for c in cues],default=0),
                   incident_name_mentioned=bool(re.search(r'борща[гхк]|борщ[ао]',text.lower())))
        for topic,pattern in TOPICS.items():
            hits=[i for i,c in enumerate(cues) if re.search(pattern,c['text'].lower())]
            row[topic]=len(hits)
            # Candidate windows retain negations, attribution and adjacent phrases.
            for i in hits:
                a,b=max(0,i-2),min(len(cues),i+3)
                contexts.append({'video_id':vid,'topic':topic,'start':cues[a]['start'],'end':cues[b-1]['end'],
                                 'url':metadata[vid]['url']+f'&t={int(cues[a]["start"])}s',
                                 'context':' '.join(c['text'] for c in cues[a:b]),'stance':'unreviewed'})
        transcripts.append(row)
    fields=list(transcripts[0]) if transcripts else ['video_id']
    csv_write(output/'caption_profile.csv',transcripts,fields)
    csv_write(output/'candidate_contexts.csv',contexts,['video_id','topic','start','end','url','context','stance'])
    (output/'codebook.json').write_text(json.dumps({'topic_regex':TOPICS,'automatic_output':'candidate mentions, not stance',
       'stance_values':{'asserts':'speaker directly advances a claim; not verified truth',
                       'attributes_claim':'speaker attributes or quotes another source',
                       'rejects_or_corrects':'excerpt rejects or corrects an earlier claim',
                       'mixed':'excerpt combines incompatible claims or qualifications',
                       'unclear':'insufficient context or interrogative/conditional form'},
       'manual_additional_topics':{'incident_discussion':'explicit discussion of the event without narrower theme',
                                   'scene_footage':'speech from the scene without a stable factual claim'},
       'coding_unit':'one contextually inspected passage; never equate speaker with publisher',
       'relevance_values':{'incident_context_supported':'local text supports a connection to this incident; does not establish the truth of claims',
                           'available_text_no_verified_incident_excerpt':'readable text lacks sufficient self-contained incident context; title is not content proof'},
       'limitations':'ASR cannot establish truth, speaker identity, irony, quotations or exact wording; confirm against original before publication.'},ensure_ascii=False,indent=2))
    summary={'snapshot':str(snapshot),'planned':len(selection),'attempted':len(receipts),
      'status_counts':dict(Counter(r['status'] for r in receipts)),'caption_count':len(transcripts),
      'nonempty_caption_texts':sum(r['characters']>0 for r in transcripts),
      'empty_caption_video_ids':[r['video_id'] for r in transcripts if not r['characters']],
      'kind_counts':dict(Counter(r['kind'] for r in transcripts)),
      'language_counts':dict(Counter(r['language'] for r in transcripts)),
      'selection_day_counts':dict(Counter(r['upload_date'] for r in selection)),
      'caption_day_counts':dict(Counter(r['upload_date'] for r in transcripts)),
      'selected_format_counts':dict(Counter(r['format_group'] for r in selection)),
      'caption_format_counts':dict(Counter(r['format_group'] for r in transcripts)),
      'topic_candidate_video_counts':{t:sum(r[t]>0 for r in transcripts) for t in TOPICS},
      'content_coding_scope':'candidate mentions only until manual review; no population extrapolation',
      'analysis_script_sha256':sha(Path(__file__)),
      'receipts_sha256':sha(snapshot/'receipts.jsonl'),
      'selection_sha256':sha(snapshot/'selection.csv')}
    if (output/'contextual_review.csv').exists():
        reviewed=list(csv.DictReader((output/'contextual_review.csv').open()))
        observed={r['video_id'] for r in transcripts}
        assert all(r['video_id'] in observed for r in reviewed), 'review missing source caption'
        def normalized(value): return re.sub(r'[^\w]+', ' ', value.lower()).strip()
        for r in reviewed:
            source_text=(output/f"{r['video_id']}.transcript.txt").read_text()
            assert normalized(r['short_excerpt']) in normalized(source_text), (r['video_id'],r['short_excerpt'])
        quote_words=Counter()
        for r in reviewed: quote_words[r['video_id']]+=len(r['short_excerpt'].split())
        assert max(quote_words.values(),default=0)<=25, 'More than 25 quoted words per source video'
        nonempty={r['video_id'] for r in transcripts if r['characters']>0}
        assert {r['video_id'] for r in reviewed}==nonempty, 'Review must cover all and only nonempty texts'
        review_status={}
        for r in reviewed:
            status=r['relevance_assessment']
            assert status in {'incident_context_supported','available_text_no_verified_incident_excerpt'}
            assert review_status.setdefault(r['video_id'],status)==status, 'Conflicting video relevance assessments'
        summary['review_relevance_video_counts']=dict(Counter(review_status.values()))
        summary['review_relevance_passage_counts']=dict(Counter(r['relevance_assessment'] for r in reviewed))
        summary['review_quote_words_max_per_video']=max(quote_words.values(),default=0)
        zero_candidate=[r['video_id'] for r in transcripts if r['characters']>0 and not any(r[t] for t in TOPICS)]
        summary['nonempty_zero_lexical_candidates']=zero_candidate
        review_profile=[]
        for r in transcripts:
            entries=[entry for entry in reviewed if entry['video_id']==r['video_id']]
            review_profile.append({'video_id':r['video_id'],'characters':r['characters'],
              'lexical_candidate_topics':sum(r[t]>0 for t in TOPICS),
              'reviewed_passages':len(entries),
              'review_status':review_status.get(r['video_id'],'empty_text_not_reviewable'),
              'reviewed_topics':';'.join(sorted({entry['topic'] for entry in entries})),
              'url':r['url']})
        csv_write(output/'video_context_review.csv',review_profile,list(review_profile[0]))
        summary['contextual_review_sha256']=sha(output/'contextual_review.csv')
        summary['contextually_reviewed_videos']=len({r['video_id'] for r in reviewed})
        summary['contextually_reviewed_passages']=len(reviewed)
        summary['reviewed_excerpt_stances']=dict(Counter(r['excerpt_stance'] for r in reviewed))
    (output/'analysis_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print(json.dumps(summary,ensure_ascii=False))

def write_report(snapshot, output, target):
    """Markdown report only after final manifest exists, with explicit denominators."""
    summary=json.loads((output/'analysis_summary.json').read_text())
    manifest=json.loads((snapshot/'manifest.json').read_text())
    config=json.loads((snapshot/'config.json').read_text())
    root=Path(__file__).resolve().parents[2]
    videos=[json.loads(l) for l in (root/config['videos']).open()]
    eligible={r['video_id'] for r in csv.DictReader((root/config['metadata_selection']).open()) if r['selected']=='True'}
    videos=[r for r in videos if r['id'] in eligible]
    selection=list(csv.DictReader((snapshot/'selection.csv').open()))
    profile=list(csv.DictReader((output/'caption_profile.csv').open()))
    receipts=[json.loads(l) for l in (snapshot/'receipts.jsonl').open()]
    review=list(csv.DictReader((output/'contextual_review.csv').open()))
    byid={r['id']:r for r in videos}
    daily=[]
    for day in sorted({r.get('upload_date') for r in videos}):
        daily.append({'upload_date':day,'metadata_population':sum(r.get('upload_date')==day for r in videos),
                      'selected':sum(r['upload_date']==day for r in selection),
                      'attempted':sum(byid[r['video_id']].get('upload_date')==day for r in receipts),
                      'captions':sum(r['upload_date']==day for r in profile)})
    csv_write(output/'daily_coverage.csv',daily,list(daily[0]))
    coverage={r['video_id']:r['status'] for r in receipts}
    csv_write(output/'collection_status.csv',[
        r|{'collection_status':coverage.get(r['video_id'],'not_attempted')} for r in selection],
        list(selection[0])+['collection_status'])
    topics={'tcc_mobilization':'ТЦК / мобілізація','detention_basement':'Утримання / підвал / заручники',
      'torture_abuse':'Катування / побиття','concentration_camp':'Концтабір',
      'military_unit_official':'Військова частина / офіційні структури','medical_witness_40':'Медична допомога / 40',
      'disinformation_provocation':'Фейки / провокації','investigation_accountability':'Перевірка / відповідальність'}
    lines=[
      '# YouTube: доступні субтитри та межі змістового аналізу — 2026-11-06', '',
      f"Отримано **{summary['caption_count']} текстових доріжок із {summary['attempted']} спроб** у цільовому відборі {summary['planned']} відео. Початкова рамка — {config['lexical_population']} відео {config['channels']} каналів, відібраних за метаданими; це не змістово підтверджена генеральна сукупність. Відео та аудіо не завантажувалися; логін, платні API і обхід захисту не використовувалися.", '',
      f"Для **всіх {summary.get('contextually_reviewed_videos',0)} непорожніх текстів** контекстно перечитано {summary.get('contextually_reviewed_passages',0)} цільових фрагментів. У {summary['review_relevance_video_counts'].get('incident_context_supported',0)} відео знайдено контекстно підтриманий зв’язок фрагмента з інцидентом; у {summary['review_relevance_video_counts'].get('available_text_no_verified_incident_excerpt',0)} коротких записах текст доступний, але такого зв’язку за самим текстом не перевірено. Перевірку виконав один LLM-кодувальник за текстом субтитрів, без прослуховування оригіналу й незалежного другого кодувальника. Це розвідувальний аналіз фрагментів, а не верифікація подій або повне кодування всіх відео.", '',
      'Кожному непорожньому тексту приділено 1–2 цільові фрагменти. Для довгих ефірів читалися локальні вікна навколо згадок місця чи події; це не суцільне прочитання всього ефіру. Рішення про контекст не дорівнює встановленню істинності реплік. Числа нижче описують цей цільовий відбір і не переносяться на всі 444 відео.', '',
      '## Що встановлює прочитаний контекст', '',
      '- У прочитаних фрагментах співіснують звинувачення в насильстві, переказ чуток, офіційні заперечення й вимоги розслідування. Сам факт згадки катування чи заручників не означає підтримки цього твердження каналом.',
      '- У [новині про заяву Азову](https://www.youtube.com/watch?v=BroCVmARlGA&t=31s) ведучий прямо зачитує позицію джерела, а [згодом](https://www.youtube.com/watch?v=BroCVmARlGA&t=688s) ставить питання щодо потреби розслідування. Змішати ці голоси в єдину підтримку або заперечення було б помилкою.',
      '- У [випуску Є ПИТАННЯ](https://www.youtube.com/watch?v=QJEVzbcoxoU&t=1075s) згадка 40 заручників входить до критики поширення історії без подальших спростувань. У [новині про сутички](https://www.youtube.com/watch?v=qsna0ucg1Q8&t=7s) початкову версію жителів зіставлено із запереченням поліції.',
      '- У [довгому прямому ефірі](https://www.youtube.com/watch?v=NHouk6Z9698&t=4138s) наприкінці подано уточнення про компанію швидкої допомоги. Ранні й пізні репліки не можна звести до незмінної позиції всього ефіру.',
      '- [Багатотемний випуск](https://www.youtube.com/watch?v=OVx0tLlgNDU&t=802s) має окремий блок про підозру жінці; ранні слова про полон стосуються іншої історії. Пошук по всьому тексту без локального контексту завищує тематичні збіги.', '',
      'Ці кейси показують різні способи подання інформації. Вони не встановлюють, який наратив переважав у 444 відео, хто найбільше вплинув на аудиторію чи чиї твердження істинні. Субтитри ASR не є доказом насильства, медичного статусу, провокації чи вини.', '',
      '## Покриття та відбір', '',
      f"Відбір: 50 відео з найбільшим `view_count` старого знімка плюс {len(selection)-50} доповнення — найпопулярніше відео в кожній ще не покритій даті, комбінації дата × формат або групі лексичних маркерів заголовка. Доповнення добираються послідовно й можуть закривати кілька груп. Це цільовий відбір, не випадкова стратифікована вибірка; ваги репрезентативності відсутні.", '',
      'Формати: запис live за полями платформи; інакше коротке ≤180 с або довге >180 с. Це операційні групи тривалості, а не встановлений платформний тип Shorts. Лексичні групи заголовків — наближення для різноманітності відбору, не вже встановлені наративи.', '',
      '| Дата завантаження | Рамка метаданих | Відібрано | Спроб | Отримано доріжок |',
      '|---|---:|---:|---:|---:|']
    lines += [f"| {r['upload_date']} | {r['metadata_population']} | {r['selected']} | {r['attempted']} | {r['captions']} |" for r in daily]
    lines += ['', f"Непорожніх текстових витягів: **{summary['nonempty_caption_texts']}**; порожні або непридатні до текстового парсингу доріжки: `{', '.join(summary['empty_caption_video_ids']) or 'немає'}`. Завантаження файлу не дорівнює змістовому покриттю.", '', 'Мітки дат узято з `upload_date` YouTube; дата 06.11 є неповною. Розподіл не описує часову динаміку всього інформаційного простору.', '',
              'Статуси спроб: '+ '; '.join(f'`{k}` — {v}' for k,v in summary['status_counts'].items())+'.',
              'Типи отриманих доріжок за позначкою сервісу: '+ '; '.join(f'`{k}` — {v}' for k,v in summary['kind_counts'].items())+'.',
              'Мовні коди доріжок: '+ '; '.join(f'`{k}` — {v}' for k,v in summary['language_counts'].items())+'.',
              f"Причина завершення: `{manifest.get('stop_reason') or 'selection_exhausted'}`.", '',
              '`no_original_caption_track` означає, що extractor не надав придатної VTT-доріжки без параметра машинного перекладу `tlang` у цій спробі. Це не доказ, що жодних субтитрів не існує. `manual` — мітка платформи для завантаженої доріжки, не гарантія її авторства або точності. Мова за кодом платформи може не збігатися з мовою кожної репліки.', '',
              '## Тематичні кандидати та позиція', '',
              'Автоматичний пошук позначає лише кандидатні згадки в повному тексті. Таблиця нижче не є розподілом підтриманих наративів: категорії перетинаються, можуть належати іншим сюжетам і включають заперечення та цитати. Рідкісна або нульова кількість збігів не означає відсутності відповідного смислу.', '',
              '| Лексична тема | Відео зі збігом серед отриманих доріжок |','|---|---:|']
    lines += [f"| {topics[k]} | {v} |" for k,v in summary['topic_candidate_video_counts'].items()]
    lines += ['', '`contextual_review.csv` зберігає статус зв’язку з інцидентом, тему, короткий нормалізований ASR-фрагмент, час, пояснення та позицію конкретного фрагмента: `asserts`, `attributes_claim`, `rejects_or_corrects`, `mixed`, `unclear`. В усіх рядках позиція видавця `not_assigned`: промовець у записі чи цитоване джерело не тотожні редакційній позиції. Маркер медик / 40 не підтверджує число 40. Кількість переглядів належить старим метаданим і не встановлює прочитання саме цих фрагментів.', '',
              f"Відео без автоматичних лексичних кандидатів серед непорожніх текстів: {len(summary['nonempty_zero_lexical_candidates'])} (`{', '.join(summary['nonempty_zero_lexical_candidates'])}`). Вони також прочитані: нуль regex-збігів не є доказом відсутності релевантного смислу. Статуси усіх отриманих текстів збережено у `video_context_review.csv`; порожній витяг окремо має `empty_text_not_reviewable`.", '',
      f"Статус `available_text_no_verified_incident_excerpt` мають {summary['review_relevance_video_counts'].get('available_text_no_verified_incident_excerpt',0)} відео: `"+'`, `'.join(sorted({r['video_id'] for r in review if r['relevance_assessment']=='available_text_no_verified_incident_excerpt'}))+"`. Їхні рядки — аудит доступного тексту з невизначеним зв’язком, а не підтверджені релевантні кейси. Малі підгрупи та цільовий добір не дозволяють порівнювати поширеність позицій.", '',
      '## Джерела, відтворення та обмеження', '',
              f"- Сирий незмінний знімок: `{snapshot.relative_to(root) if snapshot.is_absolute() else snapshot}`; `manifest.json` містить SHA-256 файлів, версію yt-dlp та походження відбору; `receipts.jsonl` — окремий статус і діагностику кожної спроби.",
              f"- Результати: `{output.relative_to(root) if output.is_absolute() else output}` — `selection.csv`, `collection_status.csv`, `daily_coverage.csv`, `analysis_summary.json`, `caption_profile.csv`, `candidate_contexts.csv`, `contextual_review.csv`, `video_context_review.csv`, `codebook.json`, текстові похідні. Профіль і контексти мають один рядок на відео та на кандидатний фрагмент відповідно.",
              '- Збір: `src/youtube_subtitles_collect.py`; парсинг, агрегати та звіт: `src/analysis/youtube_content_analyze.py`. Змістові фрагменти збережено окремою таблицею рішень; код перевіряє наявність нормалізованого уривка в отриманому тексті.',
              f"- Відтворити агрегати та звіт: `.venv/bin/python src/analysis/youtube_content_analyze.py {snapshot} {output} --report {target}`.",
              '- Новий збір завжди створює новий raw-знімок. `--previous <snapshot>` читає попередні квитанції й пропускає успішні та завершені без доріжки спроби; попередній знімок не змінює. Помилки залишаються придатними для повторної спроби. Два послідовні 429 або вимога логіну/перевірки бота зупиняють поточний збір.',
              '- Повного відтворення відповідей YouTube не гарантовано: платформа та доріжки можуть змінитися. Відтворювані саме розрахунки на зафіксованому знімку. Резервні метадані для всіх 444 лишаються лексичною рамкою й не підмінюють відсутній зміст.',
              '- Рівень A стосується кількості спроб, файлів і зафіксованих метаданих; змістова інтерпретація текстових фрагментів — рівень C. Істинність взаємовиключних заяв і поширеність позицій у всіх 444 цими даними не встановлюються.', '',
              'Протокол продовжує правило `wiki/precommit/2026-11-05-continuation-protocol.md`: regex означає згадку, доки контекст не встановив позицію. Цей додатковий розвідувальний зріз не є новою сліпою попередньою реєстрацією.']
    target.write_text('\n'.join(lines)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('snapshot',type=Path);p.add_argument('output',type=Path)
    p.add_argument('--report',type=Path)
    a=p.parse_args();analyze(a.snapshot,a.output)
    if a.report: write_report(a.snapshot,a.output,a.report)
