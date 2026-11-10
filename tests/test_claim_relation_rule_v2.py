"""Regression tests for relation-rule v2: numeric anchoring, polarity and named-person guards."""
from src.analysis.claim_verification import infer_claim_passage_relation as infer


def kv(n, key="count"):
    return {key: {"value": n, "operator": "eq"}}


def is_support(claim, text):
    r = infer(claim, {"text": text})
    return r["relation"] == "supports" and r["decision"] == "accepted"


OCC = {"statement": "У будівлі перебували 26 військовослужбовців", "key_values": kv(26)}


def test_number_with_unrelated_predicate_is_not_support():
    assert not is_support(OCC, "26 дерев ростуть у саду.")


def test_number_with_matching_predicate_is_support():
    assert is_support(OCC, "У будівлі перебували 26 військовослужбовців, повідомило МВС.")


def test_number_anchor_must_be_in_the_same_sentence():
    claim = {"statement": "Серед осіб у приміщенні 4 військових самовільно залишили частину", "key_values": kv(4)}
    assert not is_support(claim, "У приміщенні перебували особи. Військових самовільно ніхто не бачив. У саду росте 4 дерева.")


def test_article_number_with_abbreviation_periods_stays_one_sentence():
    claim = {"statement": "ДБР відкрило кримінальне провадження за ч. 5 ст. 426-1 КК України за фактом подій",
             "key_values": {"article_number": {"value": 426, "operator": "eq"}, "part_number": {"value": 5, "operator": "eq"}, "article_subnumber": {"value": 1, "operator": "eq"}}}
    assert is_support(claim, "ДБР відкрило кримінальне провадження за ч. 5 ст. 426-1 КК України.")


def test_plain_negation_is_not_support():
    claim = {"statement": "Поліція прибула на місце події", "key_values": {}}
    assert not is_support(claim, "Поліція не прибула на місце події.")


def test_negation_through_copula_is_not_support():
    claim = {"statement": "Кримінальне провадження відкрили за фактом подій", "key_values": {}}
    assert not is_support(claim, "Кримінальне провадження не було відкрили за фактом подій.")


def test_negation_of_unrelated_word_does_not_block_support():
    claim = {"statement": "Поліція прибула на місце події", "key_values": {}}
    assert is_support(claim, "Поліція прибула на місце події, але журналістів не пустили.")


def test_claim_that_is_itself_negated_can_match_negated_passage():
    claim = {"statement": "Поліція не прибула на місце події", "key_values": {}}
    assert is_support(claim, "Поліція не прибула на місце події.")


def test_different_person_is_not_support():
    claim = {"statement": "Суд обрав Олені Коваль запобіжний захід у вигляді тримання під вартою без застави", "key_values": {}}
    assert not is_support(claim, "Суд обрав Ірині Бондар запобіжний захід у вигляді тримання під вартою без застави.")


def test_inflected_surname_still_matches():
    claim = {"statement": "Суд застосував до Галині Шевчук тримання під вартою на 59 днів", "key_values": kv(59)}
    assert is_support(claim, "Суд застосував до Шевчук тримання під вартою на 59 днів.")


def test_anchor_tolerates_inflection_of_predicate_words():
    claim = {"statement": "Галині Шевчук повідомлено про підозру за ч. 1 ст. 114-1 КК України",
             "key_values": {"article_number": {"value": 114, "operator": "eq"}, "part_number": {"value": 1, "operator": "eq"}}}
    assert is_support(claim, "Галину Шевчук підозрюють у перешкоджанні — ч. 1 ст. 114-1 Кримінального кодексу України.")
