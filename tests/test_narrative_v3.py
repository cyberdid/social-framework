import unittest

from src.analysis.narrative_v3 import classify


class NarrativeV3Tests(unittest.TestCase):
    def test_direct_ukrainian_assertion_has_mentions_and_candidate_claim(self):
        result = classify("ТЦК утримує людей у підвалі.")

        self.assertEqual(result.mentions, ("ТЦК", "людей", "підвалі"))
        self.assertEqual(result.claim, "ТЦК утримує людей у підвалі.")
        self.assertIsNone(result.speaker)
        self.assertEqual(result.commitment, "direct_assertion")
        self.assertEqual(result.stance_to_claim, "support")
        self.assertEqual(result.truth_state, "unverified")
        self.assertEqual(result.classifier_status, "candidate_only")

    def test_topic_mention_without_a_proposition_does_not_become_a_claim(self):
        result = classify("ТЦК, підвал, медики.")

        self.assertEqual(result.mentions, ("ТЦК", "підвал", "медики"))
        self.assertIsNone(result.claim)
        self.assertEqual(result.commitment, "none")
        self.assertEqual(result.stance_to_claim, "not_applicable")

    def test_russian_reported_claim_extracts_speaker_without_endorsing_it(self):
        result = classify("По словам Ивановой, в подвале держат людей.")

        self.assertEqual(result.mentions, ("подвале", "людей"))
        self.assertEqual(result.claim, "в подвале держат людей.")
        self.assertEqual(result.speaker, "Ивановой")
        self.assertEqual(result.commitment, "attributed")
        self.assertEqual(result.stance_to_claim, "unclear")

    def test_reporting_verb_is_not_mislabeled_as_direct_support(self):
        result = classify("Сообщают, что ТЦК удерживает людей в подвале.")

        self.assertEqual(result.claim, "ТЦК удерживает людей в подвале.")
        self.assertIsNone(result.speaker)
        self.assertEqual(result.commitment, "reports")
        self.assertEqual(result.stance_to_claim, "reports_no_stance")

    def test_ukrainian_reporting_wrapper_is_not_labeled_as_support(self):
        result = classify("Повідомили, що ТЦК утримує людей у підвалі.")

        self.assertEqual(result.claim, "ТЦК утримує людей у підвалі.")
        self.assertEqual(result.commitment, "reports")
        self.assertEqual(result.stance_to_claim, "reports_no_stance")

    def test_quoted_claim_is_not_labeled_as_support(self):
        result = classify("«В подвале держат людей», — пишут в сети.")

        self.assertEqual(result.claim, "В подвале держат людей")
        self.assertEqual(result.commitment, "quoted")
        self.assertEqual(result.stance_to_claim, "unclear")

    def test_scare_quotes_do_not_replace_the_whole_candidate_claim_span(self):
        result = classify("У «ТЦК» удерживают людей в подвале.")

        self.assertEqual(result.claim, "У «ТЦК» удерживают людей в подвале.")
        self.assertEqual(result.commitment, "quoted")
        self.assertEqual(result.stance_to_claim, "unclear")

    def test_explicit_negation_opposes_the_positive_candidate_claim(self):
        result = classify("В подвале людей не держат.")

        self.assertEqual(result.claim, "В подвале людей держат.")
        self.assertEqual(result.commitment, "denial")
        self.assertEqual(result.stance_to_claim, "oppose")

    def test_coordinated_claims_abstain_without_partial_negation_normalization(self):
        text = "Людей не держат в подвале, а пытают."
        result = classify(text)

        self.assertEqual(result.claim, text)
        self.assertEqual(result.commitment, "unclear")
        self.assertEqual(result.stance_to_claim, "unclear")

    def test_explicit_debunk_opposes_the_candidate_claim(self):
        result = classify("Сообщение, что людей держат в подвале, — фейк.")

        self.assertEqual(result.claim, "людей держат в подвале")
        self.assertEqual(result.commitment, "debunk")
        self.assertEqual(result.stance_to_claim, "oppose")

    def test_negated_debunk_does_not_become_support(self):
        result = classify("Это не фейк: в подвале держат людей.")

        self.assertEqual(result.commitment, "unclear")
        self.assertEqual(result.stance_to_claim, "unclear")

    def test_hedged_claim_abstains_on_stance(self):
        result = classify("Возможно, людей удерживают в подвале.")

        self.assertEqual(result.commitment, "unclear")
        self.assertEqual(result.stance_to_claim, "unclear")

    def test_attempt_to_leave_is_not_misread_as_torture(self):
        result = classify("Медики пытаются уйти из подвала.")

        self.assertIsNone(result.claim)
        self.assertEqual(result.commitment, "none")
        self.assertEqual(result.stance_to_claim, "not_applicable")

    def test_constrained_torture_predicate_still_recognizes_torture(self):
        result = classify("Медиков пытают в подвале.")

        self.assertEqual(result.claim, "Медиков пытают в подвале.")
        self.assertEqual(result.stance_to_claim, "support")

    def test_question_is_not_labeled_as_support_or_opposition(self):
        result = classify("ТЦК утримує людей у підвалі?")

        self.assertEqual(result.commitment, "unclear")
        self.assertEqual(result.stance_to_claim, "unclear")

    def test_compound_with_conflicting_assertion_and_debunk_abstains(self):
        result = classify("ТЦК утримує людей у підвалі. Це неправда.")

        self.assertEqual(result.commitment, "unclear")
        self.assertEqual(result.stance_to_claim, "unclear")

    def test_unrecognized_text_abstains_and_empty_input_is_safe(self):
        for text in ("Завтра матч начнётся в восемь.", "", None):
            with self.subTest(text=text):
                result = classify(text)
                self.assertEqual(result.mentions, ())
                self.assertIsNone(result.claim)
                self.assertEqual(result.stance_to_claim, "not_applicable")
                self.assertEqual(result.truth_state, "unverified")

    def test_classification_is_deterministic(self):
        text = "За словами Марії, медиків не пускають у підвал."

        self.assertEqual(classify(text), classify(text))

    def test_annotation_preserves_raw_evidence_offsets_and_limitations(self):
        text = "  В подвале людей не держат.  "
        result = classify(text)

        self.assertEqual(result.claim, "В подвале людей держат.")
        self.assertEqual(result.source_text, text)
        self.assertEqual(result.evidence_text, text)
        self.assertEqual(result.evidence_start, 0)
        self.assertEqual(result.evidence_end, len(text))
        self.assertEqual(
            text[result.evidence_start:result.evidence_end], result.evidence_text
        )
        self.assertEqual(
            result.limitations,
            (
                "deterministic_rules_only",
                "narrow_uk_ru_vocabulary",
                "candidate_only",
                "no_independent_gold_set_validation",
                "truth_unverified",
            ),
        )


if __name__ == "__main__":
    unittest.main()
