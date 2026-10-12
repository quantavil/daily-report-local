# Selective AI pipeline — approved design
User approved the in-chat plan and requested GPT-6 Luna xhigh multi-agent execution.
Goal: eliminate redundant SB21 processing, implement genuine field-specific extraction,
persist safe caches and resume work without repeating completed AI requests.
Keep pure AI on meta/muse-spark-1.3-contributor; native AnyDoc OCR reject;
PyMuPDF unsorted page selection; strict schema; existing workbook/date/conflict safeguards.
No local parser, model change, universal OCR, destructive deduplication, or Gmail writes.

## Behaviour
Discover metadata for every matching attachment; choose newest internalDate per document
type deterministically, paginate if necessary. Download only slots needed by requested
workbook fields. SB22 supersedes SB21; use SB21 when SB22 absent, unreadable, identity-
invalid or missing invoice/exporter. Never SB21 for LEO or containers.
All available Gatepass/SB22 page-one observations participate in LEO conflict checks.
LEO-only selects page one, preserves full source rows and asks only for leo_evidence.
Normal sync requests missing fields; force requests selected fields, including filled.
Refresh re-extracts rather than reusing cached AI. Read-only verification always fresh AI.
Unchanged attachment IDs reuse PDF bytes; changed bytes invalidate conversion and extraction.
Cache identity includes content hash, SB, document type, requested fields, model, prompt,
schema and converter/validator version. Cached raw AI observations are revalidated against
current source text; never trust cached derived date alone. Invalid or corrupt cache is a miss.
Do not persist secrets or real documents to Git; local cache directories/files 0700/0600.
Conversion errors, ambiguous dates and missing evidence never erase existing workbook dates.

## Scope decisions
Gmail discovery remains a fresh per-SB metadata search so revisions are not hidden by TTLs.
No persistent Gmail history cursor in this phase: content-addressed reuse and requested-
field selection deliver incremental processing within the current CLI without a daemon.
A combined request remains one call for newly needed fields; exceptional SB21 fallback
may require one additional request restricted to missing invoice/exporter fields.
Keep complete page-one LEO context initially; no aggressive cell cropping until benchmarked.
Expose cache hits and stage timings so later latency tuning is evidence-based.
