from datetime import date

import pytest

from daily_report.models import BillIdentity
from daily_report.rules import document_codes, filename_prefix, matches_identity, parse_filename

NAME = "754640407102026INMBD6SB22081020261432.pdf"


def test_filename_fixed_segments_and_optional_document_date():
    m = parse_filename(NAME)
    assert m.identity == BillIdentity(
        sb_no="7546404", sb_date=date(2026, 10, 7), port_code="INMBD6", document_date=date(2026, 10, 8)
    )
    assert m.document_at.hour == 14
    assert filename_prefix(BillIdentity(sb_no="7546404"), "SB22") == "7546404"
    assert filename_prefix(m.identity, "SB22") == NAME[:-8]


@pytest.mark.parametrize(
    "name",
    [
        NAME.replace("07102026", "32102026"),
        NAME.replace("1432", "2460"),
        NAME.replace("SB22", "SB23"),
        NAME.replace(".pdf", " (1).pdf"),
        "../" + NAME,
    ],
)
def test_malformed_filename_rejected(name):
    with pytest.raises(ValueError):
        parse_filename(name)


def test_identity_matches_every_supplied_component():
    actual = parse_filename(NAME).identity
    assert matches_identity(BillIdentity(sb_no="7546404"), actual)
    assert not matches_identity(actual.model_copy(update={"document_date": date(2026, 10, 9)}), actual)
    assert document_codes({"leo_date", "container_no"}) == {"SB22", "SB35"}
def test_batch_name_is_the_common_identity_prefix():
    from daily_report.rules import batch_name, parse_filename
    metadata = parse_filename('754640407102026INMBD6SB22081020261717.pdf')
    assert batch_name(metadata.identity) == '754640407102026INMBD6'
    assert batch_name(metadata.identity.model_copy(update={'sb_date': None, 'port_code': None})) == '7546404unknownunknown'


def test_update_window_age_boundary_and_unknown_date():
    from daily_report.rules import gmail_window_open
    identity = BillIdentity(sb_no="7546404", sb_date=date(2026, 10, 7))
    assert gmail_window_open(identity, date(2026, 11, 5))
    assert not gmail_window_open(identity, date(2026, 11, 6))
    assert not gmail_window_open(identity, date(2026, 12, 1))
    assert gmail_window_open(identity.model_copy(update={"sb_date": None}), date(2026, 12, 1))


def test_storage_year_uses_sb_date_not_document_timestamp():
    from daily_report.rules import storage_year
    m = parse_filename('754640431122026INMBD6SB22010120271717.pdf')
    assert storage_year(m.identity) == '2026'
    assert storage_year(BillIdentity(sb_no='7546404')) == 'unknown'


def test_old_incomplete_or_failed_results_still_require_gmail():
    from daily_report.models import BillResult
    from daily_report.rules import gmail_checks_needed
    identity = BillIdentity(sb_no='7546404', sb_date=date(2026, 10, 7))
    assert gmail_checks_needed(identity, date(2026, 11, 6), None)
    assert gmail_checks_needed(identity, date(2026, 11, 6), BillResult(identity=identity))
