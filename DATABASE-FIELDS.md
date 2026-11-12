# Database fields and where they come from

Fields in **bold** are exported to `report.xlsx`. Where a field has several sources, the first
non-empty value wins and later sources only fill blanks. Examples are made up, in the same
format as real portal values; never put real client data in this file.

A bill is `Success` when `iec`, `exporter`, an invoice, `leo_date`, a container, its size and its seal are all filled. A gap in any gatepass field (container, size, seal, iec, exporter) triggers a gatepass fetch.

## `bills` (one row per bill; key is financial year + SB number)

| Field | Data source(s) | Example 1 | Example 2 |
| --- | --- | --- | --- |
| fy | derived from SB date | 2026-27 | 2025-26 |
| **sb_no** | Job Status report, gatepass month search | 7100001 | 6900002 |
| **sb_date** | Job Status report, gatepass month search | 2026-10-05 | 2026-02-14 |
| **port_code** | Job Status report, gatepass month search | INMBD6 | INDER6 |
| **iec** | Job Status report, gatepass `IEC/Br` | 0399000001 | 0599000002 |
| job_status | Job Status report | LEO | LEO |
| **job_no** | Job Status report, public SB enquiry | 2700001 | 2600002 |
| **job_date** | Job Status report, public SB enquiry | 2026-10-05 | 2026-02-14 |
| control_number | Job Status report | (blank) | (blank) |
| **exporter** | IEC name map (My IEC list, `iec-set`), gatepass `3.Exporter` | SAMPLE EXPORTS PVT LTD | DEMO TRADERS LLP |
| cha_no | public SB enquiry | AAAAA0000ACH001 | AAAAA0000ACH001 |
| port_of_discharge | public SB enquiry | ZACPT | AEJEA |
| total_package | public SB enquiry | 1326 | 480 |
| gross_weight | public SB enquiry | 5300.19 | 21450.00 |
| fob | public SB enquiry | 3242420.52 | 1875000.00 |
| total_cess | public SB enquiry | 0 | 0 |
| drawback | public SB enquiry | 38909 | 12500 |
| str | public SB enquiry | 0 | 0 |
| dbk_str_total | public SB enquiry | 38909 | 12500 |
| cin_no | public SB enquiry | 26PCEG10264032747800 | 26PCEG10255012345600 |
| cin_date | public SB enquiry | 2026-10-05 | 2026-02-14 |
| reward_flag | public SB enquiry | Y | N |
| curr_queue | public SB enquiry | HIST | EXAM |
| **curr_status** | public SB enquiry | (blank when finished) | EXAM OVER |
| **leo_date** | public SB enquiry | 2026-10-07 | 2026-02-16 |
| ep_copy | public SB enquiry | Y | N |
| scroll_no | public SB enquiry | 192713 | (blank) |
| scroll_date | public SB enquiry | 2026-10-30 | (blank) |
| mark_date | public SB enquiry | 2026-10-05 | 2026-02-14 |
| exam_date | public SB enquiry | 2026-10-06 | 2026-02-15 |
| appraisal_date | public SB enquiry | (blank) | (blank) |
| ready_to_pr | public SB enquiry | P | P |
| sample_drawn | public SB enquiry | (blank) | (blank) |
| test_report | public SB enquiry | (blank) | (blank) |
| ware_name | public SB enquiry | shed | shed |
| rosl_availed | public SB enquiry | N | N |
| rosl_amount | public SB enquiry | 0 | 0 |
| rosl_scroll_no | public SB enquiry | (blank) | (blank) |
| rosl_scroll_date | public SB enquiry | (blank) | (blank) |
| awb_no | public SB enquiry | (blank) | (blank) |
| gateway_port | public SB enquiry | (blank) | (blank) |
| gateway_site_id | public SB enquiry | (blank) | (blank) |
| fetched_job, fetched_enquiry, fetched_gatepass | set by the run when each source answers | (blank), 2026-10-30T15:10:30Z, 2026-10-30T15:10:37Z | 2026-10-30T15:10:12Z, 2026-10-30T15:10:30Z, (blank) |

## `invoices` (one row per invoice)

| Field | Data source(s) | Example 1 | Example 2 |
| --- | --- | --- | --- |
| **invoice_no** | public SB enquiry | BM/26-27/98 | 9230023771 |
| **invoice_date** | public SB enquiry | 2026-10-30 | 2026-10-29 |
| igst_value | public SB enquiry | (blank) | 18450.00 |

## `containers` (one row per container, in order)

| Field | Data source(s) | Example 1 | Example 2 |
| --- | --- | --- | --- |
| **container_no** | public SB enquiry (EGM list), gatepass | NYKU3827049 | FANU3161005 |
| **size** | gatepass | 20 | 40 |
| **seal** | public SB enquiry (EGM list), gatepass | BOLT52404787 | BOLT52390468 |
| **egm_no** | public SB enquiry (EGM list) | CMR135672 | J26H10351 |
| **egm_date** | public SB enquiry (EGM list) | 2026-10-30 | 2026-10-30 |

## `iec_names` (IEC to exporter name)

| Field | Data source(s) | Example 1 | Example 2 |
| --- | --- | --- | --- |
| iec | My IEC list, `iec-set` | 0399000001 | 0599000002 |
| name (the exporter) | My IEC list, `iec-set` | SAMPLE EXPORTS PVT LTD | DEMO TRADERS LLP |
| source | `my-iec` or `user` | my-iec | user |

## `manual` (bills closed by hand)

| Field | Data source(s) | Example 1 | Example 2 |
| --- | --- | --- | --- |
| fy, sb_no | `mark-manual` | 2024-25, 1000001 | 2025-26, 2000002 |

 in the database

Both are computed at export time.

| Excel column | How it is computed | Example 1 | Example 2 |
| --- | --- | --- | --- |
| **Type Shipment (derived)** | `LCL` when a container is shared by bills of different non-empty exporters, else blank | (blank) | LCL |
| **Status** | `Success`, `Partial`, `Cancelled` or `Not found`, from what the database holds | Success | Partial |

## Candidate additions to the report

Already in the database; adding one only means editing `export-map.json`.

| Field | Why | Example 1 | Example 2 |
| --- | --- | --- | --- |
| scroll_no, scroll_date | whether drawback is credited | 192713, 2026-10-30 | (blank) |
| port_of_discharge | destination port | ZACPT | AEJEA |
| fob, gross_weight | useful if the report is also used for billing | 3242420.52, 5300.19 | 1875000.00, 21450.00 |
