import pytest

from leo_validation import resolve_leo_date
from models import LeoDateEvidence


def _evidence(doc_type, label, raw_date, quote, page_number=1):
    return LeoDateEvidence(
        doc_type=doc_type,
        page_number=page_number,
        label=label,
        raw_date=raw_date,
        quote=quote,
    )


def test_submission_date_is_rejected_even_when_quote_also_contains_leo_date():
    context = (
        "**5.Submission** 06-JUN-26 15:32 **6.LEO Date.** 08-JUN-26\n"
        "**9.LEO** 08-JUN-26 11:47"
    )
    evidence = _evidence(
        "FINAL_LEO",
        "5.Submission",
        "06-JUN-26",
        "**5.Submission** 06-JUN-26 15:32 **6.LEO Date.** 08-JUN-26",
    )

    result = resolve_leo_date([evidence], {"finalLeo": {1: context}})

    assert result.leo_date is None
    assert result.warning
    assert result.accepted_evidence == []


def test_quote_must_include_the_cited_leo_label_before_the_date_token():
    context = "5.Submission 06-JUN-26 6.LEO Date 08-JUN-26"
    evidence = _evidence("FINAL_LEO", "6.LEO Date", "08-JUN-26", "08-JUN-26")

    result = resolve_leo_date([evidence], {"finalLeo": {1: context}})

    assert result.leo_date is None
    assert result.warning


def test_sb22_point_six_and_event_nine_evidence_agree():
    point6 = "**5.Submission** 06-JUN-26 15:32 **6.LEO Date.** 08-JUN-26"
    event9 = "**9.LEO** 08-JUN-26 11:47 **12.Vessel Name**"
    context = f"{point6}\n{event9}"

    result = resolve_leo_date(
        [
            _evidence("FINAL_LEO", "6.LEO Date", "08-JUN-26", point6),
            _evidence("FINAL_LEO", "9.LEO", "08-JUN-26", event9),
        ],
        {"finalLeo": {1: context}},
    )

    assert result.leo_date == "08-Jun-2026"
    assert [item.label for item in result.accepted_evidence] == ["6.LEO Date", "9.LEO"]


def test_gatepass_date_has_priority_when_sb22_agrees():
    gate_quote = "1.Order No / date 1/101 08-JUN-26"
    sb22_quote = "5.Submission 06-JUN-26 6.LEO Date 08-JUN-26"

    result = resolve_leo_date(
        [
            _evidence("FINAL_LEO", "6.LEO Date", "08-JUN-26", sb22_quote),
            _evidence("EGATEPASS", "1.Order No / date", "08-JUN-26", gate_quote),
        ],
        {
            "eGatepass": {1: gate_quote},
            "finalLeo": {1: sb22_quote},
        },
    )

    assert result.leo_date == "08-Jun-2026"
    assert result.accepted_evidence[0].doc_type == "EGATEPASS"


def test_conflicting_gatepass_and_sb22_dates_are_refused():
    gate_quote = "1.Order No / date 1/101 08-JUN-26"
    sb22_quote = "5.Submission 06-JUN-26 6.LEO Date 09-JUN-26"

    result = resolve_leo_date(
        [
            _evidence("EGATEPASS", "1.Order No / date", "08-JUN-26", gate_quote),
            _evidence("FINAL_LEO", "6.LEO Date", "09-JUN-26", sb22_quote),
        ],
        {
            "eGatepass": {1: gate_quote},
            "finalLeo": {1: sb22_quote},
        },
    )

    assert result.leo_date is None
    assert "conflict" in result.warning.lower()
    assert result.accepted_evidence == []


@pytest.mark.parametrize(
    ("evidence", "contexts"),
    [
        (
            _evidence("FINAL_LEO", "6.LEO Date", "08-JUN-26", "invented quote 08-JUN-26"),
            {"finalLeo": {1: "6.LEO Date 08-JUN-26"}},
        ),
        (
            _evidence("FINAL_LEO", "6.LEO Date", "09-JUN-26", "6.LEO Date 08-JUN-26"),
            {"finalLeo": {1: "6.LEO Date 08-JUN-26"}},
        ),
        (
            _evidence("FINAL_LEO", "6.LEO Date", "08-JUN-26", "6.LEO Date 08-JUN-26", page_number=2),
            {"finalLeo": {1: "6.LEO Date 08-JUN-26", 2: "6.LEO Date 08-JUN-26"}},
        ),
        (
            _evidence("EGATEPASS", "1.Order No / date", "08-JUN-26", "1.Order No / date 08-JUN-26"),
            {"finalLeo": {1: "1.Order No / date 08-JUN-26"}},
        ),
    ],
)
def test_fake_quote_date_page_or_source_is_rejected(evidence, contexts):
    result = resolve_leo_date([evidence], contexts)

    assert result.leo_date is None
    assert result.warning


def test_gatepass_order_value_can_be_on_the_direct_next_line():
    context = "1.Order No / date\n1/101 08-JUN-26\nA LEO 2.Name of Officer"
    evidence = _evidence(
        "EGATEPASS",
        "1.Order No / date",
        "08-JUN-26",
        "1.Order No / date\n1/101 08-JUN-26",
    )

    result = resolve_leo_date([evidence], {"eGatepass": {1: context}})

    assert result.leo_date == "08-Jun-2026"


@pytest.mark.parametrize("order_id", ["1/112", "1 / 112"])
def test_gatepass_merged_order_officer_header_uses_unique_date_cell(order_id):
    row = (
        "|A LEO DTLS||1.Order No / date 2.Name of Officer|"
        f"{order_id}|Nagendra Singh||01-APR-26|||||||"
    )
    evidence = _evidence(
        "EGATEPASS",
        "1.Order No / date",
        "01-APR-26",
        row,
    )

    result = resolve_leo_date([evidence], {"eGatepass": {1: row}})

    assert result.leo_date == "01-Apr-2026"
    assert result.accepted_evidence == [evidence]


def test_gatepass_merged_order_officer_header_rejects_multiple_dates():
    row = (
        "|A LEO DTLS||1.Order No / date 2.Name of Officer|"
        "1/112|Nagendra Singh||01-APR-26|02-APR-26|||||"
    )
    evidence = _evidence("EGATEPASS", "1.Order No / date", "01-APR-26", row)

    result = resolve_leo_date([evidence], {"eGatepass": {1: row}})

    assert result.leo_date is None
    assert result.warning


def test_gatepass_merged_order_officer_header_rejects_multiple_order_ids():
    row = (
        "|1/777|A LEO DTLS||1.Order No / date 2.Name of Officer|"
        "1/112|Nagendra Singh||01-APR-26|||||||"
    )
    evidence = _evidence("EGATEPASS", "1.Order No / date", "01-APR-26", row)

    result = resolve_leo_date([evidence], {"eGatepass": {1: row}})

    assert result.leo_date is None
    assert result.warning


@pytest.mark.parametrize("unexpected_cell", ["7.Assessment", "BRC Date", "3.Other Field"])
def test_gatepass_merged_order_officer_header_rejects_unrecognized_cells(unexpected_cell):
    row = (
        "|A LEO DTLS||1.Order No / date 2.Name of Officer|"
        f"1/112|Nagendra Singh|{unexpected_cell}|01-APR-26|||||||"
    )
    evidence = _evidence("EGATEPASS", "1.Order No / date", "01-APR-26", row)

    result = resolve_leo_date([evidence], {"eGatepass": {1: row}})

    assert result.leo_date is None
    assert result.warning


def test_gatepass_merged_blank_date_rejects_unrecognized_cells_before_sb22_fallback():
    gatepass_row = (
        "|A LEO DTLS||1.Order No / date 2.Name of Officer|"
        "1/112|Nagendra Singh|7.Assessment|||||||"
    )
    sb22_row = "6.LEO Date 08-JUN-26"
    evidence = [
        _evidence("EGATEPASS", "1.Order No / date", None, gatepass_row),
        _evidence("FINAL_LEO", "6.LEO Date", "08-JUN-26", sb22_row),
    ]

    result = resolve_leo_date(
        evidence,
        {"eGatepass": {1: gatepass_row}, "finalLeo": {1: sb22_row}},
    )

    assert result.leo_date is None
    assert result.warning


@pytest.mark.parametrize("other_date_label", ["SB Date", "13. CIN DT", "5.Submission"])
def test_gatepass_merged_order_officer_header_rejects_mixed_date_labels(other_date_label):
    row = (
        "|A LEO DTLS||1.Order No / date 2.Name of Officer|"
        f"1/112|Nagendra Singh|{other_date_label}|01-APR-26|||||"
    )
    evidence = _evidence("EGATEPASS", "1.Order No / date", "01-APR-26", row)

    result = resolve_leo_date([evidence], {"eGatepass": {1: row}})

    assert result.leo_date is None
    assert result.warning


def test_gatepass_merged_order_officer_quote_must_preserve_order_id():
    source = (
        "|A LEO DTLS||1.Order No / date 2.Name of Officer|"
        "1/112|Nagendra Singh||01-APR-26|||||||"
    )
    quote = "1.Order No / date 2.Name of Officer Nagendra Singh 01-APR-26"
    evidence = _evidence("EGATEPASS", "1.Order No / date", "01-APR-26", quote)

    result = resolve_leo_date([evidence], {"eGatepass": {1: source}})

    assert result.leo_date is None
    assert result.warning


def test_first_copy_is_never_an_eligible_leo_source():
    result = resolve_leo_date([], {"firstCopy": {1: "9.LEO 08-JUN-26"}})

    assert result.leo_date is None
    assert result.warning is None
