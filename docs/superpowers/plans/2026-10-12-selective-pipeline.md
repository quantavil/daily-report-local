# Selective Pipeline Implementation Plan
> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development. User explicitly chose GPT-6 Luna xhigh multi-agent execution.

**Goal:** Avoid redundant documents and AI work while preserving source-validated LEO writes.
**Architecture:** Fresh metadata discovery selects source revisions; a private content cache
reuses attachment bytes, native conversion and raw AI outputs. Requested-field profiles
control selected documents, original pages, strict schema and sequential workbook writes.
**Tech Stack:** Existing Python 3.14, uv, AnyDoc, PyMuPDF, OpenRouter, openpyxl; no new dependency.
**Spec:** docs/superpowers/specs/2026-10-12-selective-pipeline.md

## Global Constraints
- AI model meta/muse-spark-1.3-contributor; no local/regex date extraction fallback.
- AnyDoc native OCR reject; PyMuPDF unsorted text; close every document.
- Preserve workbook guards, cancellation, merged cells, scoped writes and backups.
- No agent live API/Gmail calls, secrets access or production workbook writes.
- All code in the explicitly shared isolated worktree; disjoint ownership below.
- User authorized tests, review, fixes and commits; preserve secrets Git ignore.

## Review Focus
- Same filename with new attachment bytes must invalidate results.
- Invalid SB22 must not suppress valid SB21 invoice/exporter fallback.
- Cached LEO evidence must be validated against current full source contexts.
- Blank/conflicting LEO and read-only audit must retain existing safety behaviour.
- Revised Gatepass must invalidate LEO even when SB22 is unchanged.

### Task 1: Private cache primitives
Files: src/pipeline_cache.py, src/config.py, pyproject.toml, .gitignore,
tests/test_pipeline_cache.py.
Interfaces: PipelineCache(root: Path); get_bytes(namespace: str,key: str)->bytes|None;
put_bytes(namespace,key,value:bytes)->None; get_json(namespace,key)->dict|None;
put_json(namespace,key,value:dict)->None; cache_key(*parts: object)->str.
Config: CACHE_DIR default BASE_DIR/.cache/daily-report; environment DAILY_REPORT_CACHE_DIR.
- [ ] Tests: valid roundtrip, corrupt misses, content/key distinctions, private permissions,
atomic concurrent writes, invalid paths, unwritable storage safely disables caching.
- [ ] Implement minimal cache with versioned envelopes and atomic replacement; no TTL reuse
of Gmail discovery. Keep secrets out of keys/data and logs.
- [ ] Run targeted pytest/Ruff; report red/green evidence and changed files; no commits yet.

### Task 2: Selective Gmail and AI profiles
Files: src/gmail_client.py, src/extractor.py, tests/test_extractor.py,
tests/test_gmail_client.py.
Interfaces: GmailClient(...,cache: PipelineCache|None=None);
fetch_documents(sb_no,*,fields:set[str]|None=None,include_first_copy:bool=False)->dict.
extract_with_ai(...,fields:set[str]|None=None); get_strict_json_schema(fields=None);
_build_prompt(doc_type="COMBINED",fields=None).
fields use workbook names inv_no,shipper_name,leo_date,container_no,container_size,customs_seal.
- [ ] Tests metadata pagination/newest selection, zero SB21 downloads when SB22 present,
firstCopy fallback/LEO exclusion, cached attachment reuse, changed attachment invalidation.
- [ ] Strict schema contains only requested output properties; LEO-only solely leo_evidence,
all nested required/additionalProperties constraints remain. Prompt retains complete source
row requirement; irrelevant guidelines omitted; unrequested response data cleared.
- [ ] Run targeted pytest/Ruff and report. No commits.

### Task 3: Profile routing, caching and CLI integration
Files: src/sync.py, src/pdf_processor.py, tests/test_pdf_processor.py,
tests/test_sync_rows.py, tests/test_leo_safety.py, tests/test_leo_repair.py,
tests/test_pipeline_efficiency.py.
Interfaces: trim_pdf(...,*,leo_only:bool=False); process_documents(...,
fields:set[str]|None=None,cache:PipelineCache|None=None,refresh:bool=False).
- [ ] LEO-only page one/full-row context, excludes SB21 without conversion; identity and
conflict guards unchanged. Scope schema/prompt through fields argument.
- [ ] Cache conversion by source bytes/type/profile/converter version; cache raw AI fields,
including runtime warnings, by exact source/profile/model/prompt/schema/version. Re-resolve
LEO every hit; never cache errors or malformed AI responses as successful output.
- [ ] Worker passes missing/requested fields, production cache, refresh; SB21 fallback only
for missing invoice/exporter with safe merge preserving validated LEO/other values.
- [ ] --refresh bypasses AI cache; --no-cache disables cache; audits always fresh AI.
- [ ] Measure stages and cache hit counts; expose concise CLI timings. Rejects/statuses keep
selection and no-overwrite behaviour. Compatible default process_documents calls.
- [ ] Test cold/warm identical sources (warm zero AI/conversion calls), revisions, profile
changes, cache tampering, scoped fallback, no invoice re-extraction in LEO-only and fresh audit.
- [ ] Run tests/Ruff; report no commits.

### Task 4: Review, docs and evidence
- [ ] Controller reviews each task diff; GPT-6 Luna xhigh independent reviewer audits integration.
- [ ] Fix findings, run full pytest/Ruff, lock/build checks as relevant.
- [ ] Update concise README/AGENTS/environment example; synthetic reproducible efficiency report.
- [ ] Replay private sample PDFs read-only with approved API use only by controller if needed.
- [ ] Commit reviewed changes, integrate into original checkout; retain workbook/secrets ignored.
- [ ] Resume incomplete repair through safe recorded results after efficiency verification;
compare workbook against original snapshot and accepted evidence.
