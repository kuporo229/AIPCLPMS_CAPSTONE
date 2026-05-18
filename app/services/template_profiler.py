"""
AI-powered DOCX template profiler.

Reads a departmental CLP template, extracts its structural fingerprint
(tables, headers, cell labels, merged regions), and sends it to Gemini
to produce a ``template_profile`` JSON that describes:

  - which sections exist and where they are (locators)
  - what alignment columns the template expects
  - which metadata fields appear on the front page

Public API
----------
extract_document_structure(doc)
    Pure-Python structural extraction from a python-docx Document.

profile_template(file_bytes, department, *, user_id=None, plan_id=None)
    End-to-end: extract → AI analysis → validated profile dict.

profile_template_offline(file_bytes, department)
    Same as above but without AI — returns only the structural extract
    so the caller can build or edit the profile manually.
"""

import hashlib
import json
import logging
import re
from io import BytesIO
from typing import Any, Dict, List, Optional

from docx import Document

from app.services.template_profile import (
    CURRENT_SCHEMA_VERSION,
    validate_profile,
)
from app.services.template_context_extractor import extract_template_context, context_counts
from app.services.template_generation_spec import (
    extract_generation_spec,
    merge_spec_into_profile,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structural extraction (no AI)
# ---------------------------------------------------------------------------

def _cell_text(cell) -> str:
    """Concatenate all paragraph texts in a cell, stripped."""
    return '\n'.join(p.text for p in cell.paragraphs).strip()


def _run_font_summary(run) -> Dict[str, Any]:
    """Extract a small font fingerprint from a run."""
    font = run.font
    return {
        'name': font.name,
        'size': str(font.size) if font.size else None,
        'bold': run.bold,
        'italic': run.italic,
    }


def _para_summary(para, include_runs: bool = False) -> Dict[str, Any]:
    """Summarize a paragraph."""
    info: Dict[str, Any] = {
        'text': para.text.strip(),
        'style': para.style.name if para.style else None,
    }
    if para.alignment is not None:
        info['alignment'] = str(para.alignment)
    if include_runs and para.runs:
        info['font'] = _run_font_summary(para.runs[0])
    return info


def _table_summary(table, table_index: int) -> Dict[str, Any]:
    """Build a structural summary of a single table."""
    rows_info: List[Dict[str, Any]] = []
    for ri, row in enumerate(table.rows):
        cells_info = []
        for ci, cell in enumerate(row.cells):
            text = _cell_text(cell)
            cell_info: Dict[str, Any] = {
                'col': ci,
                'text': text[:200] if text else '',
            }
            # Detect font from first run.
            for para in cell.paragraphs:
                if para.runs:
                    cell_info['font'] = _run_font_summary(para.runs[0])
                    break
            cells_info.append(cell_info)
        rows_info.append({
            'row': ri,
            'cells': cells_info,
        })
    return {
        'table_index': table_index,
        'num_rows': len(table.rows),
        'num_cols': len(table.columns) if table.columns else 0,
        'rows': rows_info,
    }


def extract_document_structure(doc: Document) -> Dict[str, Any]:
    """Extract structural fingerprint from a python-docx Document.

    Returns a dict with ``paragraphs``, ``tables``, ``sections`` (page
    layout), and ``headers``/``footers`` suitable for sending to AI.
    """
    # Body paragraphs (first 50, skip blanks for compactness).
    body_paras: List[Dict[str, Any]] = []
    for idx, para in enumerate(doc.paragraphs):
        if not para.text.strip():
            continue
        body_paras.append({
            'index': idx,
            **_para_summary(para, include_runs=True),
        })
        if len(body_paras) >= 50:
            break

    # Tables.
    tables_info: List[Dict[str, Any]] = []
    for ti, table in enumerate(doc.tables):
        tables_info.append(_table_summary(table, ti))

    # Headers / footers from each section.
    hf_info: List[Dict[str, Any]] = []
    for si, section in enumerate(doc.sections):
        sec_info: Dict[str, Any] = {'section': si}
        header_paras = [p.text.strip() for p in section.header.paragraphs if p.text.strip()]
        footer_paras = [p.text.strip() for p in section.footer.paragraphs if p.text.strip()]
        if header_paras:
            sec_info['header_text'] = header_paras
        if footer_paras:
            sec_info['footer_text'] = footer_paras
        if header_paras or footer_paras:
            hf_info.append(sec_info)

    return {
        'body_paragraphs': body_paras,
        'tables': tables_info,
        'headers_footers': hf_info,
        'total_paragraphs': len(doc.paragraphs),
        'total_tables': len(doc.tables),
    }


def file_hash(file_bytes: bytes) -> str:
    """SHA-256 hex digest of the file contents."""
    return hashlib.sha256(file_bytes).hexdigest()


# ---------------------------------------------------------------------------
# AI profiling prompt
# ---------------------------------------------------------------------------

PROFILE_SYSTEM_PROMPT = """\
You are an expert document analyst specializing in academic Course Learning \
Plan (CLP) templates used by Philippine higher-education institutions.

You will receive a JSON structural extract of a DOCX template.  Your job is \
to produce a **template_profile** JSON object that describes the template's \
layout so that a software system can automatically fill generated content \
into the correct cells.

## Rules
1. Identify every meaningful section:
   - **COURSE INFORMATION** — Look for the first section that contains \
     key-value metadata pairs (course code, course title, description, \
     units, pre/co-requisites, schedule, room, etc.).  This is often a \
     dedicated table with two columns (label + value) or a block of \
     labeled paragraphs near the top of page 1.  Mark it as a section \
     with ``content_type: "key_value"`` and ALSO list each field in \
     ``metadata_fields`` with its own ``table_cell`` locator.
   - **COURSE OUTCOMES / CLO TABLE** — The table that lists the course \
     learning outcomes.  Rows may be grouped by domain (Cognitive, \
     Affective, Psychomotor).  Detect ALL CLO rows regardless of grouping.
   - CLO-PO **alignment table** (checkmark-matrix or text-based).
   - **Weekly course outline** table (18 rows typical).
   - Program-to-Institutional Outcomes table, references, signatories, \
     consultation schedule, rubric/evaluation criteria, SDG display, etc.
2. For each section, provide a **locator** that pinpoints it in the document:
   - `table_cell`: `{type, table_index, row, col}` — a single cell.
   - `table_region`: `{type, table_index, start_row, end_row}` — a row range.
   - `paragraph`: `{type, paragraph_index}` — a body paragraph.
   - `paragraph_range`: `{type, start_index, end_index}` — contiguous body paragraphs.
   - `header_match`: `{type, header_text}` — match by text (regex OK).
3. List every **alignment column** the CLO table uses (e.g. PLO, SGA, Core \
   Values, PQF, AQRF, SDG).  Use the exact column header text. If a table has \
   one column per outcome code, preserve every code column dynamically; never \
   summarize or truncate BPED/BSED/PLO-style columns.
4. List front-page **metadata fields** (course code, course title, course description, \
   pre-requisite, co-requisite, units/credit, class hours, class schedule, room \
   assignment, type of course, etc.) with their locators.
   - **CRITICAL**: For metadata fields inside a table, use ``table_cell`` \
     locators (type: table_cell, table_index, row, col).
   - Scan EVERY table on the first 1-2 pages for labeled key-value pairs.
   - Common label patterns: "Course Code:", "Descriptive Title:", \
     "Course Description:", "Pre-requisite:", "Credit:", "Units:", \
     "Class Hours:", "Schedule:", "Room:", "Type of Course:".
   - Even if a label appears as a separate paragraph/cell from its value, \
     capture the value cell as the locator target.
5. Treat bold/title-like academic blocks such as Institutional Outcomes and \
   Program Outcomes as separate sections when they appear in adjacent cells. \
   Do not merge Program Outcomes into the final Institutional Outcome item.
6. Return **only** valid JSON, no markdown fences, no commentary.

## Output schema (schema_version "1")
```
{
  "schema_version": "1",
  "department": "<department name>",
  "sections": [
    {
      "id": "<snake_case_id>",
      "label": "<human label>",
      "locator": { ... },
      "content_type": "table_region|table_cell|paragraph|key_value",
      "notes": "<optional extra info>"
    }
  ],
  "metadata_fields": [
    {
      "field": "<field_name>",
      "label": "<visible label in template>",
      "locator": { ... }
    }
  ],
  "alignment_columns": {
    "<column_id>": {
      "label": "<column header text>",
      "table_index": <int>,
      "col_index": <int>
    }
  }
}
```
"""


def _build_profile_prompt(structure: Dict[str, Any], department: str) -> str:
    """Assemble the full prompt for AI profiling."""
    return f"""\
{PROFILE_SYSTEM_PROMPT}

DEPARTMENT: {department}

DOCUMENT STRUCTURE (JSON):
{json.dumps(structure, indent=2, default=str)}

Produce the template_profile JSON now.
"""


def _add_first_page_paragraph_metadata(profile: Dict[str, Any], doc: Document) -> None:
    """Add known first-page metadata locators missed by AI profiling.

    Checks BOTH body paragraphs AND table cells.  For table-cell matches
    it creates a proper ``table_cell`` locator (table_index, row, col); for
    body-paragraph matches it uses ``paragraph_prefix``.
    """
    mappings = [
        ("course_code", "Course Number"),
        ("course_code", "Course Code"),
        ("course_title", "Descriptive Title"),
        ("course_title", "Course Title"),
        ("course_title", "Title"),
        ("course_description", "Course Description"),
        ("course_description", "Description"),
        ("units_display", "Units"),
        ("units_display", "Credit Units"),
        ("credit_display", "Credit"),
        ("credit_display", "Units"),
        ("contact_hours_display", "Contact Hours per Week"),
        ("contact_hours_display", "Contact Hours"),
        ("contact_hours_display", "Class Hours"),
        ("contact_hours_display", "No. of Hours"),
        ("type_of_course", "Type of Course"),
        ("pre_requisite", "Pre-requisite"),
        ("pre_requisite", "Prerequisite"),
        ("pre_requisite", "Pre-Requisite"),
        ("co_requisite", "Co-requisite"),
        ("co_requisite", "Corequisite"),
        ("co_requisite", "Co-Requisite"),
        ("class_schedule", "Class Schedule"),
        ("class_schedule", "Schedule"),
        ("room_assignment", "Room Assignment"),
        ("room_assignment", "Room"),
    ]
    fields = profile.setdefault("metadata_fields", [])
    existing = {
        (str(item.get("field") or ""), str((item.get("locator") or {}).get("prefix") or item.get("label") or ""))
        for item in fields
        if isinstance(item, dict)
    }

    def _is_delimiter(text):
        return str(text or '').replace('\xa0', ' ').strip() in {'', ':', '-', '–', '—'}

    def _find_in_table_cell(prefix_lower):
        """Return (table_index, row, value_col) or None."""
        for ti, table in enumerate(doc.tables):
            for ri, row in enumerate(table.rows):
                for ci, cell in enumerate(row.cells):
                    text = _cell_text(cell).strip()
                    if text.lower().startswith(prefix_lower):
                        previous_tc = cell._tc
                        saw_separator = False
                        for candidate_idx in range(ci + 1, len(row.cells)):
                            candidate = row.cells[candidate_idx]
                            if candidate._tc is previous_tc:
                                continue
                            previous_tc = candidate._tc
                            candidate_text = _cell_text(candidate).strip()
                            if _is_delimiter(candidate_text):
                                if saw_separator:
                                    return ti, ri, candidate_idx
                                saw_separator = True
                                continue
                            return ti, ri, candidate_idx
                        return ti, ri, ci
        return None

    def _find_in_body_para(prefix_lower):
        """Return paragraph index or None."""
        for idx, para in enumerate(doc.paragraphs):
            if para.text.strip().lower().startswith(prefix_lower):
                return idx
        return None

    for field, prefix in mappings:
        if (field, prefix) in existing:
            continue
        prefix_lower = prefix.lower()

        # Try table cells first (most first-page metadata is in tables)
        cell_loc = _find_in_table_cell(prefix_lower)
        if cell_loc is not None:
            ti, ri, ci = cell_loc
            fields.append({
                "field": field,
                "label": prefix,
                "locator": {
                    "type": "table_cell",
                    "table_index": ti,
                    "row": ri,
                    "col": ci,
                    "prefix": prefix,
                },
            })
            continue

        # Fall back to body paragraphs
        para_idx = _find_in_body_para(prefix_lower)
        if para_idx is None:
            continue
        fields.append({
            "field": field,
            "label": prefix,
            "locator": {
                "type": "paragraph_prefix",
                "search_range": [0, len(doc.paragraphs)],
                "prefix": prefix,
            },
        })


def _table_text(table) -> str:
    return " ".join(
        _cell_text(cell)
        for row in table.rows
        for cell in row.cells
    ).lower()


def _guess_table_index_for_section(section: Optional[Dict[str, Any]], doc: Document) -> Optional[int]:
    if not isinstance(section, dict):
        return None
    section_key = f"{section.get('id', '')} {section.get('label', '')}".lower()

    def matches(text: str) -> bool:
        if "consultation" in section_key:
            return (
                "time / availability" in text
                or "office hours" in text
                or "consultation period" in text
                or ("days" in text and "room" in text and ("time" in text or "availability" in text))
            )
        if any(token in section_key for token in ("course_information", "course_info", "course_detail")):
            return (
                "course code" in text
                or "course title" in text
                or "descriptive title" in text
                or ("units" in text and ("credit" in text or "contact" in text))
                or "prerequisite" in text
                or "co-requisite" in text
            )
        if any(token in section_key for token in ("approval", "signator", "signature")):
            return (
                "signature" in text
                and any(token in text for token in ("prepared", "reviewed", "approved", "last revised", "last updated"))
            )
        if "sdg" in section_key:
            return "goal " in text or "sustainable development" in text or "target sdg" in text
        if "reference" in section_key:
            return "reference" in text or "bibliograph" in text
        if any(token in section_key for token in ("rubric", "evaluation", "assessment")):
            return (
                "rubric" in text
                or "criteria" in text
                or "performance" in text
                or "excellent" in text
                or "satisfactory" in text
            )
        return False

    if "sdg" in section_key:
        best_match = None
        best_score = 0
        for idx, table in enumerate(doc.tables):
            text = _table_text(table)
            score = 0
            if re.search(r"\bgoal\s+\d+", text):
                score += 4
            if "sustainable development goal" in text:
                score += 3
            if "quality education" in text:
                score += 2
            if "sdg" in text or "sustainable development" in text:
                score += 1
            if score > best_score:
                best_score = score
                best_match = idx
        return best_match

    if any(token in section_key for token in ("rubric", "evaluation", "assessment")):
        best_match = None
        best_score = 0
        for idx, table in enumerate(doc.tables):
            text = _table_text(table)
            score = 0
            if "rubric" in text:
                score += 8
            if "criteria" in text:
                score += 4
            if "component" in text:
                score += 3
            rating_words = {"poor", "fair", "good", "excellent"}
            rating_hits = sum(1 for word in rating_words if re.search(rf"\b{word}\b", text))
            if rating_hits >= 3:
                score += 8
            elif rating_hits:
                score += rating_hits
            if "performance" in text or "rating" in text or re.search(r"\bscore\b", text):
                score += 2
            if score > best_score:
                best_score = score
                best_match = idx
        if best_score >= 3:
            return best_match

    for idx, table in enumerate(doc.tables):
        if matches(_table_text(table)):
            return idx
    return None


def _coerce_reversed_paragraph_range_locator(
    locator: Any,
    doc: Document,
    section: Optional[Dict[str, Any]] = None,
) -> tuple[Dict[str, Any], bool]:
    """Repair AI paragraph ranges with end_index before start_index.

    Rubric and other table-backed sections are sometimes described by Gemini as
    an impossible paragraph range even when the document structure shows the
    content lives in a table. Prefer a guessed table_region for those sections;
    otherwise swap the paragraph bounds so the locator remains resolvable.
    """
    if not isinstance(locator, dict) or locator.get("type") != "paragraph_range":
        return locator, False

    start = locator.get("start_index", locator.get("start"))
    end = locator.get("end_index", locator.get("end"))
    if not isinstance(start, int) or not isinstance(end, int) or end >= start:
        return locator, False

    table_index = _guess_table_index_for_section(section, doc)
    if table_index is not None and 0 <= table_index < len(doc.tables):
        return {
            **locator,
            "type": "table_region",
            "table_index": table_index,
            "start_row": 0,
            "end_row": len(doc.tables[table_index].rows),
            "source_locator_type": "paragraph_range",
            "source_locator_repair": "reversed_range_to_table_region",
            "source_start_index": start,
            "source_end_index": end,
        }, True

    repaired = dict(locator)
    repaired["start_index"] = end
    repaired["end_index"] = start
    repaired["source_locator_repair"] = "swapped_reversed_range"
    return repaired, True


def _coerce_table_index_locator(
    locator: Any,
    doc: Document,
    section: Optional[Dict[str, Any]] = None,
) -> tuple[Dict[str, Any], bool]:
    """Convert AI table-only locators into writable table regions.

    Gemini occasionally returns {"type": "table_index", "table_index": N}
    even though the canonical profile schema only supports concrete fill
    targets.  Treat that as "the whole table" so validation and rendering can
    still work, while preserving the original table index.
    """
    if not isinstance(locator, dict):
        return locator, False

    loc_type = locator.get("type")
    table_index = locator.get("table_index")
    if loc_type not in {"table_index", "table"} and not (loc_type is None and table_index is not None):
        return locator, False

    table_index_int = None
    try:
        table_index_int = int(table_index)
    except (TypeError, ValueError):
        pass
    if table_index_int is None or table_index_int < 0 or table_index_int >= len(doc.tables):
        table_index_int = _guess_table_index_for_section(section, doc)
    if table_index_int is None or table_index_int < 0 or table_index_int >= len(doc.tables):
        return locator, False

    row_count = len(doc.tables[table_index_int].rows)
    repaired = dict(locator)
    repaired["type"] = "table_region"
    repaired["table_index"] = table_index_int
    repaired.setdefault("start_row", 0)
    repaired.setdefault("end_row", row_count)
    repaired.setdefault("source_locator_type", loc_type or "table_index")
    return repaired, True


def normalize_profile_locators(profile: Dict[str, Any], doc: Document) -> int:
    """Repair common non-canonical locators in a generated profile in-place."""
    if not isinstance(profile, dict):
        return 0

    repaired_count = 0
    for section in profile.get("sections") or []:
        if not isinstance(section, dict):
            continue
        repaired, changed = _coerce_reversed_paragraph_range_locator(section.get("locator"), doc, section)
        if changed:
            section["locator"] = repaired
            if repaired.get("type") == "table_region" and section.get("content_type") in {None, "", "paragraph_range"}:
                section["content_type"] = "table_region"
            note = str(section.get("notes") or "").strip()
            repair_note = "Reversed paragraph_range locator was normalized."
            if repair_note not in note:
                section["notes"] = f"{note} {repair_note}".strip()
            repaired_count += 1
        repaired, changed = _coerce_table_index_locator(section.get("locator"), doc, section)
        if not changed:
            continue
        section["locator"] = repaired
        if section.get("content_type") in {None, "", "table", "table_index"}:
            section["content_type"] = "table_region"
        note = str(section.get("notes") or "").strip()
        repair_note = "Table-only locator was normalized to a table_region."
        if repair_note not in note:
            section["notes"] = f"{note} {repair_note}".strip()
        repaired_count += 1

    for field in profile.get("metadata_fields") or []:
        if not isinstance(field, dict):
            continue
        repaired, changed = _coerce_table_index_locator(field.get("locator"), doc)
        if changed:
            field["locator"] = repaired
            repaired_count += 1

    # ── Add table fingerprints for locator stability ──
    for section in profile.get("sections") or []:
        loc = section.get("locator") if isinstance(section, dict) else None
        if not isinstance(loc, dict):
            continue
        fingerprint = _compute_table_fingerprint(loc, doc)
        if fingerprint is not None:
            loc["fingerprint"] = fingerprint
    for field in profile.get("metadata_fields") or []:
        loc = field.get("locator") if isinstance(field, dict) else None
        if not isinstance(loc, dict):
            continue
        fingerprint = _compute_table_fingerprint(loc, doc)
        if fingerprint is not None:
            loc["fingerprint"] = fingerprint

    return repaired_count


def _compute_table_fingerprint(locator: Dict[str, Any], doc: Document) -> Optional[str]:
    """Compute a stable fingerprint for a table-based locator.

    Uses the first data row's cell texts and column count. Returns None for
    non-table locators.
    """
    loc_type = locator.get("type")
    table_index = locator.get("table_index")
    if loc_type not in {"table_cell", "table_region", "table", "table_index"} or not isinstance(table_index, int):
        return None
    if table_index < 0 or table_index >= len(doc.tables):
        return None
    table = doc.tables[table_index]
    start_row = locator.get("start_row", locator.get("row", 0))
    if not isinstance(start_row, int):
        start_row = 0
    if start_row < 0 or start_row >= len(table.rows):
        start_row = 0
    first_row = table.rows[start_row]
    row_texts = [_cell_text(cell)[:60] for cell in first_row.cells]
    col_count = len(table.columns)
    approx_rows = len(table.rows)
    raw = "|".join(row_texts) + f"|c{col_count}|r{approx_rows}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _find_table_cell_locator(doc: Document, pattern: str) -> Optional[Dict[str, Any]]:
    text_re = re.compile(pattern, re.IGNORECASE)
    for table_index, table in enumerate(doc.tables):
        for row_index, row in enumerate(table.rows):
            for col_index, cell in enumerate(row.cells):
                if text_re.search(_cell_text(cell)):
                    return {
                        "type": "table_cell",
                        "table_index": table_index,
                        "row": row_index,
                        "col": col_index,
                    }
    return None


def _add_template_context_sections(profile: Dict[str, Any], doc: Document, template_context: Dict[str, Any]) -> None:
    if not isinstance(profile, dict) or not template_context.get("institutional_outcomes"):
        return
    sections = profile.setdefault("sections", [])
    if any(section.get("id") == "institutional_outcomes" for section in sections if isinstance(section, dict)):
        return
    locator = _find_table_cell_locator(doc, r"\bINSTITUTIONAL\s+OUTCOMES?\b")
    if not locator:
        return
    sections.append({
        "id": "institutional_outcomes",
        "label": "Institutional Outcomes",
        "notes": "Detected institutional outcomes block for template-owned alignment context.",
        "locator": locator,
        "content_type": "table_cell",
    })


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def profile_template_offline(
    file_bytes: bytes,
    department: str,
) -> Dict[str, Any]:
    """Extract structure only (no AI).  Returns the raw structural extract
    plus a skeleton profile the caller can complete manually.

    Returns
    -------
    dict with keys ``structure`` and ``skeleton_profile``.
    """
    try:
        doc = Document(BytesIO(file_bytes))
    except Exception as exc:
        logger.error("Template profiling failed to read DOCX: %s", exc)
        raise ValueError("Template file is not a valid DOCX or is corrupted.") from exc
    structure = extract_document_structure(doc)
    template_context = extract_template_context(doc)
    skeleton = {
        'schema_version': CURRENT_SCHEMA_VERSION,
        'department': department,
        'sections': [],
        'metadata_fields': [],
        'alignment_columns': {},
    }
    generation_spec = extract_generation_spec(doc, skeleton, template_context)
    skeleton = merge_spec_into_profile(skeleton, generation_spec)
    _add_template_context_sections(skeleton, doc, template_context)
    _add_first_page_paragraph_metadata(skeleton, doc)
    normalize_profile_locators(skeleton, doc)
    return {
        'structure': structure,
        'skeleton_profile': skeleton,
        'template_context': template_context,
        'template_context_counts': context_counts(template_context),
        'generation_spec': generation_spec,
        'file_hash': file_hash(file_bytes),
    }


def profile_template(
    file_bytes: bytes,
    department: str,
    *,
    user_id: Optional[str] = None,
    plan_id: Optional[int] = None,
) -> Dict[str, Any]:
    """End-to-end template profiling: extract → AI → validate.

    Returns
    -------
    dict with keys:
      - ``profile``: the validated template_profile dict
      - ``structure``: raw structural extract
      - ``file_hash``: SHA-256 of the uploaded file
      - ``validation_errors``: list (empty if valid)
      - ``raw_ai_response``: the raw text from AI (for debugging)

    Raises
    ------
    ValueError
        If the AI returns unparseable JSON or the profile has critical
        validation errors that cannot be auto-corrected.
    """
    try:
        doc = Document(BytesIO(file_bytes))
    except Exception as exc:
        logger.error("Template profiling failed to read DOCX: %s", exc)
        raise ValueError("Template file is not a valid DOCX or is corrupted.") from exc
    structure = extract_document_structure(doc)
    template_context = extract_template_context(doc)
    generation_spec = extract_generation_spec(doc, template_context=template_context)
    fhash = file_hash(file_bytes)

    prompt = _build_profile_prompt(structure, department)

    logger.info(
        "Profiling template for department=%s file_hash=%s",
        department, fhash[:12],
    )

    try:
        from app.services.ai_client import AIClient

        model = AIClient.get_model()
        resp = AIClient.generate_with_retry(
            model,
            [prompt],
            {"response_mime_type": "application/json"},
            retries=3,
            task_type="template_profile",
            plan_id=plan_id,
            user_id=user_id,
        )
        raw_text = resp.text
        cleaned = AIClient.clean_ai_json(raw_text)  # noqa: F821 — imported above
        profile_data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.error("AI returned unparseable JSON for template_profile: %s", exc)
        raise ValueError("AI returned invalid JSON for template profiling. Please retry.") from exc
    except Exception as exc:
        logger.error("AI template profiling failed: %s", exc)
        raise ValueError(f"AI template profiling failed: {exc}") from exc

    # Ensure schema_version and department are set.
    profile_data.setdefault('schema_version', CURRENT_SCHEMA_VERSION)
    profile_data.setdefault('department', department)
    profile_data = merge_spec_into_profile(profile_data, generation_spec)
    _add_template_context_sections(profile_data, doc, template_context)
    _add_first_page_paragraph_metadata(profile_data, doc)
    normalize_profile_locators(profile_data, doc)

    # Validate.
    errors = validate_profile(profile_data)

    # Also validate locators against the actual document.
    from app.services.template_profile import validate_profile_against_doc
    doc_errors = validate_profile_against_doc(doc, profile_data)

    return {
        'profile': profile_data,
        'structure': structure,
        'template_context': template_context,
        'template_context_counts': context_counts(template_context),
        'generation_spec': generation_spec,
        'file_hash': fhash,
        'validation_errors': errors,
        'doc_locator_errors': doc_errors,
        'raw_ai_response': raw_text,
    }
