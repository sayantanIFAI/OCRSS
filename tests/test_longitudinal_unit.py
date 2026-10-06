"""E5-S1 (unit): which orders are the same medication, what changed, which supersedes which -
and, for the problem list, which mention continues which."""

from __future__ import annotations

from datetime import date

from cdi_adapter.longitudinal import (
    change_kind,
    desired_links,
    desired_problem_links,
    match_key,
    problem_key,
    same_problem,
)


def _o(i, d, code=None, drug="Tab Amlodipine 5 mg", **m):
    return {"id": i, "encounter_date": d, "code_system": "http://snomed.info/sct" if code else None,
            "code": code, "drug_text": drug, "local_text": drug, **m}  # fmt: skip


def test_the_same_medication_is_the_same_code_else_the_same_name():
    assert match_key(_o(1, None, code="386864001")) == "code:http://snomed.info/sct|386864001"
    assert match_key(_o(1, None, drug="Tab Amlodipine 5 mg")) == match_key(
        _o(2, None, drug="Amlodipine 10mg Tablet")
    )  # form words and strength are not the medicine's name
    assert match_key(_o(1, None, drug="Tab Amlodipine 5 mg")) != match_key(
        _o(2, None, drug="Tab Atorvastatin 10 mg")
    )
    assert match_key(_o(1, None, drug="")) is None


def test_what_changed_between_two_orders():
    old = {"strength_num": 5, "dose_num": 1, "frequency_code": "OD", "duration_days": 30}
    assert change_kind(dict(old), old) == "reordered"
    assert change_kind({**old, "strength_num": 10}, old) == "strength_changed"
    assert change_kind({**old, "frequency_code": "BD"}, old) == "frequency_changed"
    assert change_kind({**old, "strength_num": 10, "frequency_code": "BD"}, old) == "changed"
    assert change_kind({**old, "duration_days": None}, old) == "reordered"  # not written: no claim


def test_each_date_supersedes_the_latest_earlier_date_only():
    d1, d2, d3 = date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1)
    orders = [_o("a", d1, code="X"), _o("b", d2, code="X", strength_num=10), _o("c", d3, code="X"),
              _o("z", d1, code="Y")]  # fmt: skip
    links = desired_links(orders)
    assert set(links) == {("b", "a"), ("c", "b")}  # a chain, never c -> a; Y stays alone
    assert links[("b", "a")]["superseded_date"] == d1 and links[("c", "b")]["encounter_date"] == d3


def test_orders_of_one_date_never_supersede_each_other():
    d = date(2026, 8, 1)
    assert desired_links([_o("a", d, code="X"), _o("b", d, code="X")]) == {}


# ---------------------------------------------------------------- coded vs uncoded (the gap)


def test_the_same_medicine_matches_whether_or_not_it_was_coded():
    """Terminology binding is probabilistic: amlodipine is coded at one encounter and not at the
    next. Without the name-key fallback neither order would supersede the other and both would
    sit in the active list as duplicates."""
    d1, d2 = date(2026, 8, 1), date(2026, 9, 1)
    links = desired_links([_o("coded", d1, code="X"), _o("plain", d2, code=None)])
    assert set(links) == {("plain", "coded")}
    assert links[("plain", "coded")]["match_key"] == "code:http://snomed.info/sct|X"


def test_two_codes_that_disagree_are_two_medicines():
    """A binding is the stronger signal: it is never overruled by look-alike text."""
    d1, d2 = date(2026, 8, 1), date(2026, 9, 1)
    assert desired_links([_o("a", d1, code="X"), _o("b", d2, code="Y")]) == {}


def test_an_uncoded_order_joins_a_group_through_another_order():
    """Grouping is transitive, so a coded order, an uncoded one and a later coded one are one
    medication and only the newest stays active."""
    d1, d2, d3 = date(2026, 8, 1), date(2026, 9, 1), date(2026, 10, 1)
    links = desired_links([_o("a", d1, code="X"), _o("b", d2, code=None), _o("c", d3, code="X")])
    assert set(links) == {("b", "a"), ("c", "b")}


# ---------------------------------------------------------------- the problem list


def _p(i, d, code=None, textv="Hypertension", **kw):
    return {"id": i, "encounter_date": d, "code_system": "http://snomed.info/sct" if code else None,
            "code": code, "code_display": textv, "local_text": textv, "onset": None,
            "clinical_status": "active", "verification": "confirmed", **kw}  # fmt: skip


def test_the_same_problem_is_the_same_code_else_the_same_text():
    assert problem_key(_p(1, None, code="38341003")) == "code:http://snomed.info/sct|38341003"
    assert problem_key(_p(1, None, textv="Hypertension")) == problem_key(
        _p(2, None, textv="  hypertension. ")
    )  # case and punctuation are not the problem
    assert problem_key(_p(1, None, textv="")) is None
    assert same_problem(_p(1, None, code="X"), _p(2, None, code=None))  # coded, then not
    assert not same_problem(_p(1, None, code="X"), _p(2, None, code="Y"))


def test_a_later_mention_continues_the_latest_earlier_one():
    d1, d2, d3 = date(2026, 1, 5), date(2026, 4, 5), date(2026, 7, 5)
    facts = [_p("a", d1, code="X"), _p("b", d2, code="X"),
             _p("c", d3, code="X", clinical_status="resolved"),
             _p("z", d1, textv="Asthma")]  # fmt: skip
    links = desired_problem_links(facts)
    assert set(links) == {("b", "a"), ("c", "b")}  # a chain, never c -> a; asthma stays alone
    assert links[("b", "a")]["status_change"] == "unchanged"
    assert links[("c", "b")]["status_change"] == "status_changed"  # active -> resolved
    assert {v["chain_head"] for v in links.values()} == {"a"}  # the problem is dated from its start


def test_mentions_of_one_date_never_continue_each_other():
    d = date(2026, 1, 5)
    assert desired_problem_links([_p("a", d, code="X"), _p("b", d, code="X")]) == {}


def test_a_problem_with_neither_code_nor_text_is_left_alone():
    d1, d2 = date(2026, 1, 5), date(2026, 2, 5)
    assert desired_problem_links([_p("a", d1, textv=""), _p("b", d2, textv="")]) == {}
