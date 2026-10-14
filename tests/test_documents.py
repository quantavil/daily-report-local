from datetime import date

import pymupdf
import pytest

from daily_report.documents import extract_layout, parse_document, render_markdown
from daily_report.models import DocumentLayout, LayoutPage, LayoutSpan
from daily_report.rules import parse_filename


def span(text, x=20, y=20, w=180):
    return LayoutSpan(text=text, bbox=(x, y, x + w, y + 10))


def meta(code="SB21"):
    return parse_filename(f"754640407102026INMBD6{code}081020261432.pdf")


def identity():
    return [
        span("SB No", 20, 20, 50),
        span("7546404", 80, 20, 70),
        span("SB Date", 200, 20, 60),
        span("07/10/2026", 270, 20, 90),
        span("Port Code", 390, 20, 70),
        span("INMBD6", 470, 20, 65),
    ]


def layout(*pages):
    return DocumentLayout(pages=[LayoutPage(number=i + 1, width=600, height=800, spans=p) for i, p in enumerate(pages)])


def test_exporter_primary_invoice_anchored_cells():
    p = [
        *identity(),
        span("1. EXPORTER'S NAME & ADDRESS", 20, 80, 250),
        span("SYNTHETIC EXPORTS LTD", 20, 100, 230),
        span("123 ADDRESS ROAD", 20, 115),
        span("4. CHA NAME", 320, 80),
        span("WRONG CHA LTD", 320, 100),
        span("2. INVOICE No. & Dt.", 20, 200),
        span("INV-42 07/10/2026 USD 1000", 20, 220, 260),
    ]
    result = parse_document(layout(p), meta(), "hash")
    assert result.fields["shipper_name"].value == "SYNTHETIC EXPORTS LTD"
    assert result.fields["inv_no"].value == "INV-42"
    assert result.fields["inv_no"].evidence[0].page_number == 1


def test_leo_source_and_page_only():
    p = [
        *identity(),
        span("Order Date", 20, 50),
        span("08/10/2026", 20, 65),
        span("6. LEO Date", 20, 100),
        span("09/10/2026", 20, 120),
    ]
    r = parse_document(layout(p, [span("6. LEO Date"), span("10/10/2026", 20, 40)]), meta("SB22"), "hash")
    assert r.fields["leo_date"].value == date(2026, 10, 9)
    assert set(r.fields) == {"leo_date"}
    r = parse_document(
        layout([*identity(), span("Order Date", 20, 50), span("08/10/2026", 20, 65)]), meta("SB22"), "hash"
    )
    assert r.fields["leo_date"].state == "rejected"


def test_missing_identity_and_duplicate_labels_reject():
    p = [
        *identity(),
        span("6. LEO Date", 20, 100),
        span("09/10/2026", 20, 120),
        span("6. LEO Date", 300, 100),
        span("10/10/2026", 300, 120),
    ]
    assert parse_document(layout(p), meta("SB22"), "hash").fields["leo_date"].state == "rejected"
    assert (
        parse_document(layout([span("6. LEO Date"), span("09/10/2026", 20, 40)]), meta("SB22"), "hash")
        .fields["leo_date"]
        .state
        == "rejected"
    )
    p[1] = span("1111111", 80, 20, 70)
    assert parse_document(layout(p), meta("SB22"), "hash").fields["leo_date"].state == "rejected"


def test_movement_rows_keep_blank_positions_and_continuation():
    headers = [span("11. Container", 20, 100, 150), span("12. Seal", 200, 100, 120), span("14. Size", 400, 100, 80)]
    p = (
        identity()
        + headers
        + [
            span("ABCU1234567", 20, 130, 120),
            span("SEAL1", 200, 130, 90),
            span("40", 400, 130, 40),
            span("DEFU7654321", 20, 150, 120),
            span("20", 400, 150, 40),
        ]
    )
    p2 = [span("GHIU1234567", 20, 40, 120), span("SEAL3", 200, 40, 90), span("40", 400, 40, 40)]
    r = parse_document(layout(p, p2), meta("SB35"), "hash")
    assert [(c.number, c.size, c.seal) for c in r.containers] == [
        ("ABCU1234567", "40", "SEAL1"),
        ("DEFU7654321", "20", None),
        ("GHIU1234567", "40", "SEAL3"),
    ]
    assert r.fields["customs_seal"].state == "accepted"
    assert r.fields["customs_seal"].value is None
    assert r.fields["customs_seal"].value_type == "records"


def test_split_heading_and_verified_blank():
    p = [*identity(), span("6.", 20, 100, 15), span("LEO Date", 40, 100, 80), span("7. NEXT FIELD", 20, 150)]
    assert parse_document(layout(p), meta("SB22"), "hash").fields["leo_date"].state == "blank"


def test_native_extraction_original_page_numbers_and_markdown():
    d = pymupdf.open()
    for text in ["page one", "page two", "page three"]:
        p = d.new_page()
        p.insert_text((20, 30), text)
    raw = d.tobytes()
    d.close()
    native = extract_layout(raw, meta())
    assert [p.number for p in native.pages] == [1, 2, 3]
    assert native.pages[0].spans[0].text == "page one"
    assert "SOURCE PAGE 2" in render_markdown(native, meta())
    with pytest.raises(RuntimeError):
        extract_layout(b"not pdf", meta())


def test_real_geometry_overlapping_header_lines_and_far_right_leo():
    p = [
        span("Port Code", 310, 30, 38),
        span("INMBD6", 322, 39, 31),
        span("SB No", 395, 30, 24),
        span("7546404", 391, 39, 31),
        span("SB Date", 459, 30, 31),
        span("07-Oct-26", 453, 39, 40),
        span("6.LEO Date.", 315, 543, 45),
        span("09-Oct-26", 473, 543, 40),
    ]
    assert parse_document(layout(p), meta("SB22"), "hash").fields["leo_date"].value == date(2026, 10, 9)


def test_main_exporter_does_not_use_repeated_invoice_party_heading():
    p = [
        *identity(),
        span("1.EXPORTER'S NAME & ADDRESS", 63, 192, 133),
        span("SYNTHETIC EXPORTS", 63, 201, 130),
        span("123 ADDRESS ROAD", 63, 210, 130),
    ]
    p2 = [span("1.EXPORTER'S NAME & ADDRESS", 122, 160, 133), span("WRONG PARTY", 122, 180)]
    assert parse_document(layout(p, p2), meta(), "hash").fields["shipper_name"].value == "SYNTHETIC EXPORTS"


def test_stacked_movement_records_on_original_page_four():
    p = identity()
    p4 = [
        span("11,Container", 144, 183, 46),
        span("ABCU1234567", 263, 183, 54),
        span("12.Seal", 72, 197, 27),
        span("SEAL1", 120, 197, 56),
        span("13.Seal Type", 184, 197, 48),
        span("BOLT", 266, 197, 20),
        span("14.Size", 315, 197, 27),
        span("40", 366, 197, 9),
    ]
    r = parse_document(layout(p, [], [], p4), meta("SB35"), "hash")
    assert [(c.number, c.size, c.seal) for c in r.containers] == [("ABCU1234567", "40", "SEAL1")]
    assert r.fields["container_no"].state == "accepted"
    assert r.fields["container_no"].value is None
    assert r.containers[0].evidence[0].page_number == 4


def test_partially_blank_seals_are_writable_aligned_sequence():
    headers = [span("11. Container", 20, 100, 150), span("12. Seal", 200, 100, 120), span("14. Size", 400, 100, 80)]
    p = (
        identity()
        + headers
        + [
            span("ABCU1234567", 20, 130, 120),
            span("SEAL1", 200, 130, 90),
            span("40", 400, 130, 40),
            span("DEFU7654321", 20, 150, 120),
            span("20", 400, 150, 40),
        ]
    )
    result = parse_document(layout(p), meta("SB35"), "hash")
    assert result.fields["customs_seal"].state == "accepted"
    assert result.fields["customs_seal"].value is None
    assert [record.seal for record in result.containers] == ["SEAL1", None]


@pytest.mark.parametrize("code, expected_pages", [("SB21", [1, 2]), ("SB22", [1]), ("SB35", [1, 2, 4])])
def test_markdown_uses_native_cells_and_original_eligible_pages(code, expected_pages):
    native = DocumentLayout(pages=[
        LayoutPage(number=number, width=600, height=800, spans=[
            span(f"Page {number} label", 20, 20, 80),
            span("fragment", 104, 20, 50),
            span(f"Page {number} value", 300, 20, 100),
        ])
        for number in [1, 2, 4]
    ])
    markdown = render_markdown(native, meta(code))
    for number in [1, 2, 4]:
        if number in expected_pages:
            assert f"SOURCE PAGE {number}" in markdown
            assert f"Page {number} label fragment" in markdown
            assert f"Page {number} value" in markdown
        else:
            assert f"SOURCE PAGE {number}" not in markdown
            assert f"Page {number} label" not in markdown


def test_native_layout_carries_document_profile():
    d = pymupdf.open()
    p = d.new_page()
    p.insert_text((20, 30), "native")
    raw = d.tobytes()
    d.close()
    assert extract_layout(raw, meta("SB22")).document_code == "SB22"


def test_invalid_seal_does_not_block_valid_container_cells():
    p = [
        *identity(),
        span("11. Container", 20, 100, 150),
        span("12. Seal", 200, 100, 120),
        span("14. Size", 400, 100, 80),
        span("ABCU1234567", 20, 130, 120),
        span("BAD SEAL!", 200, 130, 100),
        span("40", 400, 130, 40),
    ]
    result = parse_document(layout(p), meta("SB35"), "hash")
    assert result.fields["customs_seal"].state == "rejected"
    assert result.fields["container_no"].state == "accepted"
    assert result.fields["container_size"].state == "accepted"
    assert [(record.number, record.size) for record in result.containers] == [("ABCU1234567", "40")]


def test_split_neighbor_heading_bounds_exporter_cell():
    p = [
        *identity(),
        span("1. EXPORTER'S NAME & ADDRESS", 20, 80, 250),
        span("4.", 320, 80, 15),
        span("CHA NAME", 340, 80, 100),
        span("SYNTHETIC EXPORTS LTD", 20, 100, 230),
        span("WRONG CHA LTD", 320, 100),
    ]
    assert parse_document(layout(p), meta(), "hash").fields["shipper_name"].value == "SYNTHETIC EXPORTS LTD"


@pytest.mark.parametrize(
    "text", ["07/10/2026", "07-10-2026", "07-Oct-2026", "2026-10-07", "07.10.2026", "07102026", "07-Oct-26"]
)
def test_invoice_date_without_invoice_is_rejected(text):
    p = [*identity(), span("2. INVOICE No. & Dt.", 20, 100), span(text, 20, 120)]
    assert parse_document(layout(p), meta(), "hash").fields["inv_no"].state == "rejected"


def test_exporter_address_without_name_is_rejected():
    p = [*identity(), span("1. EXPORTER'S NAME & ADDRESS", 20, 80, 250), span("123 ADDRESS ROAD", 20, 100)]
    assert parse_document(layout(p), meta(), "hash").fields["shipper_name"].state == "rejected"


def test_movement_continuation_below_numbered_identity_header():
    p = [
        *identity(),
        span("11. Container", 20, 100, 150),
        span("12. Seal", 200, 100, 120),
        span("14. Size", 400, 100, 80),
        span("ABCU1234567", 20, 130, 120),
        span("SEAL1", 200, 130, 90),
        span("40", 400, 130, 40),
    ]
    p2 = identity()
    p2[0] = span("1. SB No", 20, 20, 55)
    p2 += [span("DEFU7654321", 20, 100, 120), span("SEAL2", 200, 100, 90), span("20", 400, 100, 40)]
    r = parse_document(layout(p, p2), meta("SB35"), "hash")
    assert r.fields["container_no"].state == "accepted"
    assert [record.number for record in r.containers] == ["ABCU1234567", "DEFU7654321"]


def test_unsupported_continuation_does_not_silently_truncate():
    p = [
        *identity(),
        span("11. Container", 20, 100, 150),
        span("12. Seal", 200, 100, 120),
        span("14. Size", 400, 100, 80),
        span("ABCU1234567", 20, 130, 120),
        span("SEAL1", 200, 130, 90),
        span("40", 400, 130, 40),
    ]
    p2 = [
        span("1. UNKNOWN TABLE", 20, 20),
        span("DEFU7654321", 20, 100, 120),
        span("SEAL2", 200, 100, 90),
        span("20", 400, 100, 40),
    ]
    assert parse_document(layout(p, p2), meta("SB35"), "hash").fields["container_no"].state == "rejected"


def test_non_owned_page_identity_conflict_rejects_leo():
    p = [*identity(), span("6. LEO Date", 20, 100), span("09/10/2026", 20, 120)]
    p2 = identity()
    p2[1] = span("1111111", 80, 20, 70)
    assert parse_document(layout(p, p2), meta("SB22"), "hash").fields["leo_date"].state == "rejected"


def test_unrelated_port_table_does_not_conflict_with_bill_header():
    p = [
        *identity(),
        span("6. LEO Date", 20, 100),
        span("09/10/2026", 20, 120),
        span("3.PORT CODE", 180, 278, 100),
        span("OTHER TABLE DETAIL", 250, 300, 150),
    ]
    assert parse_document(layout(p), meta("SB22"), "hash").fields["leo_date"].value == date(2026, 10, 9)


def test_conflicting_secondary_bill_header_still_rejected():
    p = [*identity(), span("6. LEO Date", 20, 100), span("09/10/2026", 20, 120)]
    second = [
        s.model_copy(update={"bbox": (s.bbox[0], s.bbox[1] + 200, s.bbox[2], s.bbox[3] + 200)}) for s in identity()
    ]
    second[1] = span("1111111", 80, 220, 70)
    assert parse_document(layout([*p, *second]), meta("SB22"), "hash").fields["leo_date"].state == "rejected"


def test_gatepass_secondary_stacked_bill_identity_and_movement():
    p = [
        *identity(),
        span("1.SB NO", 72, 107, 32),
        span("7546404", 124, 107, 31),
        span("2.BL NO", 166, 107, 33),
        span("3.BL DT", 315, 107, 30),
        span("4.SB DT", 72, 120.5, 30),
        span("07-Oct-26", 117, 120.5, 40),
        span("5.HBL No", 166, 120.5, 34),
        span("11,Container", 144, 183, 46),
        span("ABCU1234567", 263, 183, 54),
        span("12.Seal", 72, 197, 27),
        span("SEAL1", 120, 197, 56),
        span("13.Seal Type", 184, 197, 48),
        span("BOLT", 266, 197, 20),
        span("14.Size", 315, 197, 27),
        span("40", 366, 197, 9),
    ]
    result = parse_document(layout(p), meta("SB35"), "hash")
    assert result.fields["container_no"].state == "accepted"
    assert [record.number for record in result.containers] == ["ABCU1234567"]
    p[11] = span("08-Oct-26", 117, 120.5, 40)
    assert parse_document(layout(p), meta("SB35"), "hash").fields["container_no"].state == "rejected"


def test_headerless_stacked_continuation_is_unresolved_not_truncated():
    p = [
        *identity(),
        span("11,Container", 144, 183, 46),
        span("ABCU1234567", 263, 183, 54),
        span("12.Seal", 72, 197, 27),
        span("SEAL1", 120, 197, 56),
        span("13.Seal Type", 184, 197, 48),
        span("BOLT", 266, 197, 20),
        span("14.Size", 315, 197, 27),
        span("40", 366, 197, 9),
    ]
    p2 = [span("DEFU7654321", 263, 40, 54), span("SEAL2", 120, 54, 56), span("20", 366, 54, 9)]
    assert parse_document(layout(p, p2), meta("SB35"), "hash").fields["container_no"].state == "rejected"
