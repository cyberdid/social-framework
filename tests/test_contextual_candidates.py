"""Regression checks for candidate boundaries; no truth classification."""
import unittest
import json
import tempfile
from pathlib import Path
from src.analysis.contextual_candidates import detect, scan


class ContextualCandidatesTests(unittest.TestCase):
    def test_concealment_reversed_order_and_mixed_inflections(self):
        for text in ['Сліди заметають', 'Замели всі сліди', 'Следы уже замели',
                     'Знищення доказів у підвалі', 'Улики хотят скрыть',
                     'Не замітайте сліди, це інша подія',
                     'у них было время всё зачистить все следы котуванняприбрати',
                     'може вимивали свої злочинні сліди']:
            with self.subTest(text=text):
                self.assertIn('evidence_concealment', {h['rule'] for h in detect(
                    text, {'context_text': 'Інцидент у Петропавлівській Борщагівці'})})

    def test_parent_text_enables_candidate_but_never_assigns_geography(self):
        context = {'context_text': 'Справа про підвал і медиків на Борщагівці'}
        flags = {h['rule'] for h in detect('вона не лікарка', context)}
        self.assertIn('medical_identity_dispute', flags)
        self.assertNotIn('geographic_context_conflict', flags)
        self.assertNotIn('medical_identity_dispute', {h['rule'] for h in detect(
            'Знову не працює інтернет', context)})

    def test_unrelated_parent_text_does_not_anchor_concealment(self):
        self.assertEqual(detect('Замели сліди', {'context_text': 'Погода на завтра'}), [])
        self.assertEqual(detect('У підвалі помітні сліди ремонту; заметіль триває.'), [])

    def test_analyst_review_scope_enables_review_without_geographic_claim(self):
        flags = {h['rule'] for h in detect('Сліди замели', {'review_scope': 'user_selected_case_batch'})}
        self.assertIn('evidence_concealment', flags)
        self.assertNotIn('geographic_context_conflict', flags)

    def test_implicit_cleanup_requires_review_context_and_is_a_separate_rule(self):
        context = {'review_scope': 'user_selected_case_batch'}
        text = 'Вони все ретельно прибрали поки ви туди приїхали'
        self.assertEqual(detect(text), [])
        flags = {h['rule'] for h in detect(text, context)}
        self.assertIn('implicit_cleanup_allusion', flags)
        self.assertNotIn('evidence_concealment', flags)

    def test_place_free_concealment_and_conditional_fire(self):
        flags = {x['rule'] for x in detect('Катівня у підвалі: прибирають сліди катувань. Не здивуюсь, якщо будівля спалахне, щоб знищити докази.')}
        self.assertIn('evidence_concealment', flags)
        self.assertIn('conditional_fire', flags)

    def test_word_substrings_do_not_become_evidence_claims(self):
        self.assertEqual(detect('Знищили ворожу техніку. Слідство триває. Наступні відео будуть завтра.'), [])

    def test_old_media_boundary(self):
        self.assertNotIn('old_or_recaptained_media', {x['rule'] for x in detect('Катівня: старомодне відео про політику.')})
        self.assertIn('old_or_recaptained_media', {x['rule'] for x in detect('Це старе відео, не з Борщагівки.')})

    def test_sofiivska_caption_is_context_not_conflict(self):
        flags = {x['rule'] for x in detect('Софіївська Борщагівка 05.11.26. Пожежа на складі.')}
        self.assertIn('sofiivska_context', flags)
        self.assertNotIn('geographic_context_conflict', flags)

    def test_geographic_correction_and_concealment_comment(self):
        flags = {x['rule'] for x in detect('Це Софіївська, а не Петропавлівська Борщагівка.')}
        self.assertIn('geographic_correction', flags)
        flags = {x['rule'] for x in detect('Замітають сліди катівні…', {'context_place': 'Софіївська Борщагівка'})}
        self.assertIn('geographic_context_conflict', flags)

    def test_medical_identity_requires_context(self):
        self.assertEqual(detect('Медики нашої лікарні отримали ліцензію на новий кабінет.'), [])
        self.assertIn('medical_identity_dispute', {x['rule'] for x in detect('Медик із підвалу: швидка не працює з 2023 року.')})

    def test_visible_bruises_are_not_concealment(self):
        self.assertNotIn("evidence_concealment", {x["rule"] for x in detect("У підвалі: на его лице заметны следы побоев.")})

    def test_tram_and_wounded_people_are_not_medical_identity(self):
        self.assertEqual(detect("Борщагівська лінія швидкісного трамвая не працює."), [])
        self.assertEqual(detect("Медик з підвалу виводить неіснуючих скалічених людей."), [])

    def test_explicit_event_dates_not_publication_dates(self):
        self.assertNotIn("explicit_event_date_discrepancy", {x["rule"] for x in detect("Пожежа", {"date":"2026-11-05"})})
        self.assertIn("explicit_event_date_discrepancy", {x["rule"] for x in detect("Пожежа", {"reference_event_date":"2026-11-02", "context_event_date":"2026-11-05"})})

    def test_jsonl_scan_is_deterministic_and_preserves_context(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'input.jsonl'
            rows = [dict(platform='tiktok', id='comment1', url='https://example.test/comments/1',
                         text='Замітають сліди катівні…', context_place='Софіївська Борщагівка'),
                    dict(platform='telegram', id='post2', url='https://example.test/posts/2',
                         text='У підвалі прибирають сліди катувань. Це довший текст для перевірки повторів.'),
                    dict(platform='telegram', id='post3', url='https://example.test/posts/3',
                         text='У підвалі прибирають сліди катувань. Це довший текст для перевірки повторів.')]
            source.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows))
            prefix = Path(temp) / 'result'
            summary = scan([source], prefix)
            first = prefix.with_suffix('.jsonl').read_text()
            scan([source], prefix)
            self.assertEqual(first, prefix.with_suffix('.jsonl').read_text())
            self.assertEqual(summary['repeated_groups'], 1)
            self.assertEqual(summary['rule_counts']['geographic_context_conflict'], 1)

    def test_matches_retain_actual_spans(self):
        text = 'У катівні замітають сліди.'
        for hit in detect(text):
            self.assertEqual(text[hit['start']:hit['end']], hit['match'])


if __name__ == '__main__':
    unittest.main()
