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
