import io
import json
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from copy import deepcopy
from datetime import datetime, timezone

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_ROW_HEIGHT_RULE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt
from flask import current_app

from app import STORAGE_BUCKET_NAME, supabase
from app.services.ai_client import AIClient, types
from app.compat_supabase import read_storage_bytes, write_storage_bytes
from app.services.docx_service import extract_docx_placeholders
from app.services.template_generation_spec import (
    normalize_alignment_column_label,
    normalize_alignment_row_label,
    normalize_duplicate_alignment_columns,
)


# ---------------------------------------------------------------------------
# Canonical placeholder schema (must match new_template.docx exactly)
# ---------------------------------------------------------------------------

# Course info table: label text (lowered) → canonical placeholder
COURSE_INFO_MAP = {
    "course code":            "course_code",
    "course number":          "course_code",
    "course no":              "course_code",
    "course title":           "course_title",
    "descriptive title":      "course_title",
    "title":                  "course_title",
    "course description":     "course_description",
    "description":            "course_description",
    "units":                  "units_display",
    "credit units":           "units_display",
    "service learning component": "service_learning_component",
    "target sdg":             "target_sdgs_display",
    "target sdgs":            "target_sdgs_display",
    "pre-requisite":          "pre_requisite",
    "prerequisite":           "pre_requisite",
    "pre requisite":          "pre_requisite",
    "co-requisite":           "co_requisite",
    "corequisite":            "co_requisite",
    "co requisite":           "co_requisite",
    "credit":                 "credit_display",
    "contact hours per week": "contact_hours_display",
    "contact hours":          "contact_hours_display",
    "class hours":            "contact_hours_display",
    "no. of hours":           "contact_hours_display",
    "hours per week":         "contact_hours_display",
    "class schedule":         "class_schedule",
    "schedule":               "class_schedule",
    "room assignment":        "room_assignment",
    "room":                   "room_assignment",
    "type of course":         "type_of_course",
    "course type":            "type_of_course",
    "semester":               "semester_label",
    "academic year":          "academic_year",
    "school year":            "academic_year",
}

# CLO table column header (lowered, substring match) → suffix
CLO_COLUMN_MAP = {
    "aligned program":    "aligned_plos",
    "program learning":   "aligned_plos",
    "graduate attribute":  "graduate_attributes",
    "salettinian graduate": "graduate_attributes",
    "core value":          "core_values",
    "salettinian core":    "core_values",
    "pqf":                 "pqf_alignment",
    "aqrf":                "aqrf_alignment",
    "relevant sdg":        "relevant_sdgs",
    "sdg":                 "relevant_sdgs",
}

# Weekly plan column header (lowered, substring match) → suffix
WEEK_COLUMN_MAP = {
    "topic":       "topics",
    "intended learning": "ilo",
    "learning outcome":  "ilo",
    "ilo":         "ilo",
    "teaching-learning": "tla",
    "tla":         "tla",
    "assessment":  "assessment",
    "time":        "time_frame",
    "frame":       "time_frame",
    "learning resource": "learning_resources",
    "resource":    "learning_resources",
}

SPLIT_WEEK_GROUPS = {
    "8": ("9", "8_9"),
    "10": ("11", "10_11"),
    "14": ("15", "14_15"),
    "16": ("17", "16_17"),
}

WEEKS_WITH_TIME_FRAME = {
    "1", "2", "3", "4", "5", "6", "7",
    "8", "9", "8_9",
    "10", "11", "10_11",
    "12", "13",
    "14", "15", "14_15",
    "16", "17", "16_17",
    "18",
}

# Map source week text → dynamic key that matches Copilot replacement keys.
def _week_label_to_key(label):
    """Return canonical week key or None."""
    label = _normalize_text(label).lower()
    label = label.strip("()[]{} ")
    label = re.sub(r"^\(+\s*", "", label)
    # Handle "Week 8 & 9" or "Week 8-9" or "Week 8–9"
    m = re.search(r"weeks?\s*(\d+)\s*(?:&|and|–|-|to)\s*(\d+)", label)
    if m:
        return f"{m.group(1)}_{m.group(2)}"
    # Handle "Week 7" standalone
    m = re.search(r"weeks?\s*(\d+)", label)
    if m:
        return m.group(1)
    return None

# Signatory role labels → canonical prefix
SIGNATORY_MAP = {
    "prepared and submitted by": "prepared_by",
    "prepared by":               "prepared_by",
    "last revised by":           "last_revised_by",
    "last updated by":           "last_updated_by",
    "reviewed by":               "reviewed_by",
    "endorsed by":               "endorsed_by",
    "approved by":               "approved_by",
}

# Exam separator labels → canonical placeholder
EXAM_LABEL_MAP = {
    "preliminary examination": "prelim_exam_label",
    "prelim examination":      "prelim_exam_label",
    "midterm examination":     "midterm_exam_label",
    "final examination":       "final_exam_label",
}
REFERENCE_BLOCK_MAP = {
    "references": "references_all_block",
    "bibliography": "references_all_block",
    "book": "references_textbook_block",
    "books": "references_textbook_block",
    "website": "references_website_block",
    "websites": "references_website_block",
    "online": "references_website_block",
    "textbook": "references_textbook_block",
    "textbooks": "references_textbook_block",
    "journal": "references_journal_block",
    "journals": "references_journal_block",
}

PLACEHOLDER_PATTERN = re.compile(r"^\{\{[a-z0-9_]+\}\}$")
PROGRAM_OUTCOME_CODE_RE = re.compile(r"\b(?:PLO|PO|PL|BPED|BSED[A-Z]*|BS[A-Z]{2,8}|[A-Z]{3,12})\s*0*\d+\b", re.IGNORECASE)
LABEL_VALUE_PATTERN = re.compile(
    r"^(?P<label>[A-Za-z][A-Za-z0-9/&(),.\- ]{1,80}?)\s*:\s*(?P<value>.+?)\s*$"
)
SEMESTER_YEAR_PATTERN = re.compile(
    r"^(?P<semester>.+?semester)\s*,\s*academic year\s*(?P<year>.+?)\s*$",
    re.IGNORECASE,
)
GENERIC_LABEL_BLACKLIST = {
    "vision",
    "mission",
    "core values",
    "core competencies",
    "institutional objectives",
    "course learning plan",
    "graduate attribute",
    "graduate attributes",
    "bsit program outcomes",
    "course learning outcomes (clos)",
    "course learning outcomes",
    "time frame",
    "intended learning outcomes",
    "topics",
    "teaching-learning activities (tla)",
    "assessment",
    "learning resources",
    "service learning",
}
GENERIC_LABEL_BLACKLIST_KEYWORDS = {
    "policy",
    "policies",
    "rubric",
    "rubrics",
    "plagiarism",
    "grading",
    "attendance",
    "prepared",
    "reviewed",
    "endorsed",
    "approved",
    "date submitted",
    "date reviewed",
    "date endorsed",
    "date approved",
}
SIGNATORY_BLOCK_MAP = {
    "prepared and submitted by": "prepared_by",
    "prepared by": "prepared_by",
    "reviewed by": "reviewed_by",
    "endorsed by": "endorsed_by",
    "approved by": "approved_by",
}
AI_CONFIDENCE_THRESHOLD = 0.85
AI_MAX_CANDIDATES = 10
AI_REQUEST_TIMEOUT_SECONDS = 90
TEMPLATE_INSERTION_ALGORITHM_VERSION = "v13_body_clo_paragraph_scanner"
_AI_ASSIST_EXECUTOR = ThreadPoolExecutor(max_workers=2)
_DEFAULT_TIMEOUT = object()


def _debug_log(run_id, hypothesis_id, location, message, data):
    try:
        payload = {
            "sessionId": "2d00d4",
            "runId": run_id,
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
        }
        with open("/home/llppmmss/lpms/.cursor/debug-2d00d4.log", "a", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _base36(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 0
    if number <= 0:
        return "0"
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    chars = []
    while number:
        number, remainder = divmod(number, 36)
        chars.append(alphabet[remainder])
    return "".join(reversed(chars))


def _compact_clo_checkmark_placeholder(group_index, row_index, col_index):
    return f"x{_base36(group_index)}{_base36(row_index)}{_base36(col_index)}"


def _normalize_placeholder_name(value):
    text = _normalize_text(value)
    if text.startswith("{{") and text.endswith("}}"):
        text = text[2:-2].strip()
    if not re.fullmatch(r"[a-z0-9_]+", text):
        return None
    return text


def _finalize_summary(summary, output_template_bytes, output_template_name):
    placeholder_names = [entry["placeholder"] for entry in summary["placeholders"]]
    summary["placeholder_count"] = len(placeholder_names)
    summary["placeholder_counts"] = dict(Counter(placeholder_names))
    summary["output_template_name"] = output_template_name

    output_placeholders = extract_docx_placeholders(output_template_bytes)
    summary["output_placeholder_count"] = len(output_placeholders)
    summary["output_placeholders"] = sorted(output_placeholders)


def _collect_ai_candidates(doc):
    candidates = {}

    def add_candidate(candidate):
        location = candidate["location"]
        if location in candidates:
            return
        candidates[location] = candidate

    def scan_paragraphs(paragraphs, scope):
        for pi, paragraph in enumerate(paragraphs):
            text = _normalize_text(paragraph.text)
            if not text or "{{" in text:
                continue
            match = LABEL_VALUE_PATTERN.match(text)
            if not match:
                continue
            label = _normalize_text(match.group("label"))
            value = _normalize_text(match.group("value"))
            if not _is_viable_ai_candidate(label, value):
                continue
            add_candidate(
                {
                    "kind": "paragraph_label",
                    "location": f"{scope}:paragraph:{pi}",
                    "label": label,
                    "value": value,
                    "paragraph": paragraph,
                }
            )

    def scan_tables(tables, scope):
        for ti, table in enumerate(tables):
            table_scope = f"{scope}:table:{ti}"
            if len(table.columns) == 2:
                for ri, row in enumerate(table.rows):
                    label = _normalize_text(_cell_text(row.cells[0]))
                    value = _normalize_text(_cell_text(row.cells[1]))
                    if not label or not value or "{{" in value:
                        continue
                    if _normalize_text(label).lower() == _normalize_text(value).lower():
                        continue
                    if not _is_viable_ai_candidate(label, value):
                        continue
                    add_candidate(
                        {
                            "kind": "table_value",
                            "location": f"{table_scope}:row:{ri}:cell:1",
                            "label": label,
                            "value": value,
                            "cell": row.cells[1],
                        }
                    )
            for ri, row in enumerate(table.rows):
                for ci, cell in enumerate(row.cells):
                    if cell.tables:
                        scan_tables(cell.tables, f"{table_scope}:row:{ri}:cell:{ci}")

    scan_paragraphs(doc.paragraphs, "body")
    scan_tables(doc.tables, "body")
    for si, section in enumerate(doc.sections):
        scan_paragraphs(section.header.paragraphs, f"header:{si}")
        scan_tables(section.header.tables, f"header:{si}")
        scan_paragraphs(section.footer.paragraphs, f"footer:{si}")
        scan_tables(section.footer.tables, f"footer:{si}")
    return candidates


def _is_viable_ai_candidate(label, value):
    label_text = _normalize_text(label)
    value_text = _normalize_text(value)
    if not label_text or not value_text:
        return False
    lowered = label_text.lower().strip(" :")
    if lowered in GENERIC_LABEL_BLACKLIST:
        return False
    if re.fullmatch(r"(pqf|aqrf)\s*\d+", lowered):
        return False
    if any(keyword in lowered for keyword in GENERIC_LABEL_BLACKLIST_KEYWORDS):
        return False
    if _looks_like_reference_entry(label_text, value_text):
        return False
    if len(label_text.split()) > 10:
        return False
    if len(value_text) > 180:
        return False
    if any(marker in value_text for marker in ("•", " | ")):
        return False
    if len(re.findall(r"\b[\w/-]+\b", value_text)) > 24:
        return False
    return True


def _looks_like_reference_entry(label_text, value_text):
    combined = f"{label_text} {value_text}".lower()
    if "http://" in combined or "https://" in combined or "www." in combined:
        return True
    if "youtube" in combined or "medium." in combined or "doi" in combined:
        return True
    if re.search(r"\b(19|20)\d{2}\b", label_text) and ("," in label_text or "." in label_text):
        return True
    if any(word in combined for word in ("press", "journal", "publisher", "retrieved from")):
        return True
    return False


def _build_ai_assist_prompt(candidates, summary):
    existing = sorted(
        {
            entry["placeholder"][2:-2]
            for entry in summary.get("placeholders", [])
            if entry.get("placeholder", "").startswith("{{") and entry.get("placeholder", "").endswith("}}")
        }
    )[:24]
    compact_candidates = [
        {
            "location": item["location"],
            "label": item["label"][:60],
            "value": item["value"][:90],
        }
        for item in candidates
    ]
    return (
        "Return JSON only. Review these unresolved CLP inputs and suggest placeholders only for true variable fields. "
        "Do not suggest placeholders for policies, rubrics, mission/vision, standards tables, or boilerplate. "
        "Use snake_case without braces. Reuse obvious existing names when they fit. "
        f"Only include suggestions with confidence >= {AI_CONFIDENCE_THRESHOLD:.2f}. "
        f"Existing placeholders: {json.dumps(existing, ensure_ascii=False)}. "
        f"Candidates: {json.dumps(compact_candidates, ensure_ascii=False)}. "
        'Response schema: {"suggestions":[{"location":"...","placeholder":"snake_case","confidence":0.0,"reason":"short reason"}]}'
    )


def _ai_assist_generation_config():
    config = {
        "response_mime_type": "application/json",
        # Keep this pass deterministic and fast even when the global admin
        # settings are tuned for broader CLP generation work.
        "temperature": 0.0,
    }
    try:
        config["thinking_config"] = types.ThinkingConfig(
            thinking_level=types.ThinkingLevel.MINIMAL
        )
    except Exception:
        pass
    return config


def _request_ai_assist(app, prompt):
    with app.app_context():
        # Respect the Gemini model configured by the admin in system settings.
        model = AIClient.get_model()
        response = AIClient.generate_with_retry(
            model,
            [prompt],
            _ai_assist_generation_config(),
            retries=1,
            task_type="template_beta_placeholder_map",
        )
        cleaned = AIClient.clean_ai_json(response.text)
        return json.loads(cleaned)


def _run_ai_assist(doc, summary, *, timeout_seconds=_DEFAULT_TIMEOUT):
    if timeout_seconds is _DEFAULT_TIMEOUT:
        timeout_seconds = AI_REQUEST_TIMEOUT_SECONDS
    candidates = _collect_ai_candidates(doc)
    summary["ai_candidate_count"] = len(candidates)
    if not candidates:
        _warn(summary, "AI assist found no unresolved input candidates after the rule-based pass.")
        return

    candidate_items = list(candidates.values())[:AI_MAX_CANDIDATES]
    try:
        prompt = _build_ai_assist_prompt(candidate_items, summary)
        app = current_app._get_current_object()
        if timeout_seconds is None:
            parsed = _request_ai_assist(app, prompt)
        else:
            future = _AI_ASSIST_EXECUTOR.submit(_request_ai_assist, app, prompt)
            parsed = future.result(timeout=timeout_seconds)
    except FuturesTimeoutError:
        _warn(
            summary,
            "AI assist timed out after "
            f"{timeout_seconds} seconds; rule-based placeholder mapping was kept."
        )
        return
    except Exception as exc:
        _warn(summary, f"AI assist failed; rule-based placeholder mapping was kept: {exc}")
        return

    summary["used_ai"] = True
    suggestions = parsed.get("suggestions", []) if isinstance(parsed, dict) else []
    applied = 0
    skipped = 0

    for suggestion in suggestions:
        if not isinstance(suggestion, dict):
            skipped += 1
            continue
        location = _normalize_text(suggestion.get("location"))
        candidate = candidates.get(location)
        if not candidate:
            skipped += 1
            continue
        placeholder_name = _normalize_placeholder_name(suggestion.get("placeholder"))
        if not placeholder_name:
            skipped += 1
            continue
        try:
            confidence = float(suggestion.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < AI_CONFIDENCE_THRESHOLD:
            skipped += 1
            continue
        placeholder = "{{" + placeholder_name + "}}"
        reason = _normalize_text(suggestion.get("reason")) or f"ai_assist:{candidate['label']}"

        if candidate["kind"] == "paragraph_label":
            replacement = f"{candidate['label']}: {placeholder}"
            if not _replace_paragraph_text_safely(candidate["paragraph"], replacement):
                skipped += 1
                continue
        elif candidate["kind"] == "table_value":
            if not _replace_cell_text_safely(candidate["cell"], placeholder):
                skipped += 1
                continue
        else:
            skipped += 1
            continue

        _record_placeholder(summary, placeholder, candidate["value"], location, reason=reason)
        applied += 1

    summary["ai_suggestions_applied"] = applied
    if applied == 0:
        _warn(summary, "AI assist reviewed unresolved inputs but did not add any safe placeholder suggestions.")
    elif skipped:
        _warn(summary, f"AI assist applied {applied} suggestions and skipped {skipped} low-confidence or invalid suggestions.")


def generate_template_from_docx(file_bytes, filename, *, use_ai=False):
    doc = Document(io.BytesIO(file_bytes))
    summary = {
        "source_filename": filename,
        "source_type": "docx",
        "algorithm_version": TEMPLATE_INSERTION_ALGORITHM_VERSION,
        "used_ai": False,
        "ai_requested": bool(use_ai),
        "ai_suggestions_applied": 0,
        "segments_scanned": 0,
        "segments_replaced": 0,
        "segments_skipped": 0,
        "warnings": [],
        "placeholders": [],
    }

    _normalize_logo_title_header_tables(doc, summary)
    _process_signatory_blocks(doc.paragraphs, "body", summary)
    _process_label_value_paragraphs(doc.paragraphs, "body", summary)
    _process_reference_sections(doc.paragraphs, "body", summary)

    # Scan body paragraphs for CLO list patterns (some templates put
    # "COURSE OUTCOMES" + bulleted CLOs outside tables entirely).
    _process_simple_clo_body_paragraphs(doc.paragraphs, "body", summary)

    # Walk every table and dispatch to the right recogniser.
    # Track which tables are processed so unprocessed ones can be
    # scanned for loose label:value patterns afterwards.
    # Note: course_info and simple_clo_list can co-exist in the same table
    # (e.g. LP EDUC 010 has metadata + CLOs in one table), so we don't
    # short-circuit after those.
    processed_tables = set()
    for ti, table in enumerate(doc.tables):
        scope = f"body:table:{ti}"
        summary["segments_scanned"] += 1
        matched = False
        if _try_submission_signatory_table(table, scope, summary):
            matched = True
        if _try_course_info_table(table, scope, summary):
            matched = True
        if _try_simple_clo_list_table(table, scope, summary):
            matched = True
        if _try_clo_table(table, scope, summary):
            matched = True; processed_tables.add(ti); continue
        if _try_program_institutional_checkmark_table(table, scope, summary):
            matched = True; processed_tables.add(ti); continue
        if _try_weekly_plan_table(table, scope, summary):
            matched = True; processed_tables.add(ti); continue
        if _try_consultation_table(table, scope, summary):
            matched = True; processed_tables.add(ti); continue
        if _try_signatory_table(table, scope, summary):
            matched = True; processed_tables.add(ti); continue
        if matched:
            processed_tables.add(ti)
        # Tables that match no recogniser stay literal (rubrics, SDG goals,
        # PQF/AQRF descriptors, PLO tables, performance rubrics, etc.)
        # We'll scan their cell paragraphs for loose label:value patterns below.

    # Scan cell paragraphs of UNPROCESSED tables for label:value patterns
    # (catches metadata in non-standard table layouts).
    _process_label_value_paragraphs(
        _collect_all_table_paragraphs(doc, skip_indices=processed_tables),
        "body:unmatched_tables",
        summary,
    )
        # PQF/AQRF descriptors, PLO tables, performance rubrics, etc.)

    for si, section in enumerate(doc.sections):
        _process_signatory_blocks(section.header.paragraphs, f"header:{si}", summary)
        _process_label_value_paragraphs(section.header.paragraphs, f"header:{si}", summary)
        _process_reference_sections(section.header.paragraphs, f"header:{si}", summary)
        _process_signatory_blocks(section.footer.paragraphs, f"footer:{si}", summary)
        _process_label_value_paragraphs(section.footer.paragraphs, f"footer:{si}", summary)
        _process_reference_sections(section.footer.paragraphs, f"footer:{si}", summary)

    if use_ai:
        _run_ai_assist(doc, summary)

    out = io.BytesIO()
    doc.save(out)
    output_bytes = out.getvalue()
    _finalize_summary(summary, output_bytes, filename)
    return output_bytes, summary


def apply_ai_assist_to_draft_bytes(
    file_bytes,
    summary=None,
    *,
    output_template_name="draft.docx",
    background_mode=False,
):
    doc = Document(io.BytesIO(file_bytes))
    summary_data = dict(summary or {})
    summary_data.setdefault("source_type", "docx")
    summary_data.setdefault("algorithm_version", TEMPLATE_INSERTION_ALGORITHM_VERSION)
    summary_data["used_ai"] = False
    summary_data["ai_requested"] = True
    summary_data.setdefault("ai_suggestions_applied", 0)
    summary_data.setdefault("segments_scanned", 0)
    summary_data.setdefault("segments_replaced", 0)
    summary_data.setdefault("segments_skipped", 0)
    summary_data.setdefault("warnings", [])
    summary_data.setdefault("placeholders", [])

    _run_ai_assist(
        doc,
        summary_data,
        timeout_seconds=None if background_mode else AI_REQUEST_TIMEOUT_SECONDS,
    )

    out = io.BytesIO()
    doc.save(out)
    output_bytes = out.getvalue()
    _finalize_summary(summary_data, output_bytes, output_template_name)
    return output_bytes, summary_data


def run_template_beta_ai_assist_task(app_context, task_id, payload):
    from flask import current_app

    client = current_app.config.get("SUPABASE_SERVICE") or supabase
    draft_id = int(payload.get("draft_id") or 0)
    if draft_id <= 0:
        raise ValueError("Invalid draft_id for template beta AI assist task.")

    client.table("background_tasks").update(
        {"progress_percent": 20, "progress_label": "Loading beta template draft"}
    ).eq("id", task_id).execute()

    draft_res = client.table("generated_template_drafts").select("*").eq("id", draft_id).single().execute()
    draft = draft_res.data or {}
    if not draft:
        raise ValueError(f"Generated template draft {draft_id} was not found.")

    generated_filename = draft.get("generated_filename")
    if not generated_filename:
        raise ValueError(f"Generated template draft {draft_id} has no generated document path.")

    current_meta = dict(draft.get("generation_meta") or {})
    current_meta.update({"ai_status": "processing", "ai_task_id": task_id})
    client.table("generated_template_drafts").update(
        {"generation_meta": current_meta, "updated_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", draft_id).execute()

    client.table("background_tasks").update(
        {"progress_percent": 45, "progress_label": "Applying AI placeholder assist"}
    ).eq("id", task_id).execute()

    generated_bytes = read_storage_bytes(STORAGE_BUCKET_NAME, generated_filename)
    output_name = os.path.basename(generated_filename)
    updated_bytes, updated_summary = apply_ai_assist_to_draft_bytes(
        generated_bytes,
        draft.get("placeholder_summary") or {},
        output_template_name=output_name,
        background_mode=True,
    )

    write_storage_bytes(STORAGE_BUCKET_NAME, generated_filename, updated_bytes)

    updated_meta = dict(draft.get("generation_meta") or {})
    updated_meta.update(
        {
            "used_ai": updated_summary.get("used_ai", False),
            "ai_requested": updated_summary.get("ai_requested", True),
            "ai_suggestions_applied": updated_summary.get("ai_suggestions_applied", 0),
            "ai_status": "completed",
            "ai_task_id": task_id,
        }
    )

    client.table("generated_template_drafts").update(
        {
            "placeholder_summary": summarize_placeholder_summary(updated_summary),
            "warnings": updated_summary.get("warnings", []),
            "generation_meta": updated_meta,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", draft_id).execute()

    client.table("background_tasks").update(
        {"progress_percent": 100, "progress_label": "AI template assist completed"}
    ).eq("id", task_id).execute()


# ---------------------------------------------------------------------------
# Table recognisers
# ---------------------------------------------------------------------------

def _is_merged_row(row):
    """True when every cell in the row shares the same XML element (merged)."""
    cells = row.cells
    if len(cells) < 2:
        return False
    first_tc = cells[0]._tc
    return all(c._tc is first_tc for c in cells[1:])


def _row_cell_segments(row):
    """Return one cell for each contiguous logical cell segment in a row."""
    segments = []
    seen = set()
    for cell in row.cells:
        marker = id(cell._tc)
        if marker in seen:
            continue
        seen.add(marker)
        segments.append(cell)
    return segments


def _try_course_info_table(table, scope, summary):
    """Recognise the key-value course info table.

    Handles multiple layouts:
    - 1-col: "label: value" in a single cell
    - 2-col: label in col 0, value in col 1
    - 3-col: label in col 0, separator (:) in col 1, value in col 2
    - Any col count: finds the rightmost non-empty meaningful cell as value
    """
    num_cols = len(table.columns)
    if num_cols < 1 or num_cols > 6:
        return False

    candidates = []
    known_hits = 0
    SEPARATOR_RE = re.compile(r'^[:\s\-–—]+$')

    for ri, row in enumerate(table.rows):
        if not row.cells:
            continue

        # Strategy 1: Try full-row text as "label: value" (catches 1-col or merged layouts)
        full_row_text = _normalize_text(" ".join(
            _cell_text(c) for c in row.cells
        )).strip()
        full_match = LABEL_VALUE_PATTERN.match(full_row_text)
        if full_match:
            label = _normalize_text(full_match.group("label")).lower()
            value_text = _normalize_text(full_match.group("value"))
            if value_text and not PLACEHOLDER_PATTERN.match(value_text):
                canon = _placeholder_name_from_label(label)
                if canon:
                    # Use col 0 as replacement target (entire row text gets replaced there)
                    candidates.append((ri, row.cells[0], canon, value_text, label, full_match.group("label"), "onecol"))
                    if label in COURSE_INFO_MAP:
                        known_hits += 1
                    continue

        # Strategy 2: Multi-column layout — label in col 0, find value in rightmost
        # non-empty, non-separator cell
        label = _normalize_text(_cell_text(row.cells[0])).lower().strip()
        if not label:
            continue
        canon = _placeholder_name_from_label(label)
        if not canon:
            continue

        # Find the rightmost cell that contains a real value (not just separators)
        value_cell = None
        value_text = ""
        value_col_idx = 1
        for ci in range(len(row.cells) - 1, 0, -1):
            cell_text = _normalize_text(_cell_text(row.cells[ci]))
            if not cell_text or PLACEHOLDER_PATTERN.match(cell_text):
                continue
            if SEPARATOR_RE.match(cell_text):
                continue
            if cell_text.lower() == label:
                continue
            value_cell = row.cells[ci]
            value_text = cell_text
            value_col_idx = ci
            break

        if not value_cell:
            # No non-empty value cell found — still replace the first
            # non-label cell with a placeholder (templates often have
            # empty value cells that need placeholders, e.g. co-requisite).
            if len(row.cells) >= 2:
                value_cell = row.cells[1]
                value_text = ""
            else:
                continue
        candidates.append((ri, value_cell, canon, value_text, label, label, "multicol"))
        if label in COURSE_INFO_MAP:
            known_hits += 1

    if len(candidates) < 2 or known_hits < 1:
        return False

    for candidate in candidates:
        ri, cell, canon, value_text, label, raw_label, mode = candidate
        ph = "{{" + canon + "}}"
        if mode == "onecol":
            # Col 0 gets "Label: {{placeholder}}"
            new_text = f"{raw_label}: {ph}"
            if _replace_cell_text_safely(cell, new_text):
                _record_placeholder(summary, ph, value_text, f"{scope}:row:{ri}:cell:0", reason=f"course_info:{label}")
            # Clear all other cells in this row (old value + separator columns)
            if ri < len(table.rows):
                for ci in range(1, len(table.rows[ri].cells)):
                    _replace_cell_text_safely(table.rows[ri].cells[ci], "")
        else:
            new_text = ph
            loc = f"{scope}:row:{ri}:cell:1"
            if _replace_cell_text_safely(cell, new_text):
                _record_placeholder(summary, ph, value_text, loc, reason=f"course_info:{label}")
    return True


def _try_submission_signatory_table(table, scope, summary):
    if len(table.columns) < 5 or len(table.rows) < 3:
        return False

    first_row = [_normalize_text(_cell_text(cell)).lower() for cell in table.rows[0].cells]
    joined = " ".join(first_row)
    if "prepared and submitted by" not in joined or "reviewed by" not in joined:
        return False

    name_row = table.rows[1].cells
    title_row = table.rows[2].cells
    replacements = [
        (name_row[0], "{{prepared_by_name}}", "prepared_name", f"{scope}:row:1:cell:0"),
        (title_row[0], "{{prepared_by_position}}", "prepared_position", f"{scope}:row:2:cell:0"),
        (name_row[1], "{{date_submitted}}", "date_submitted", f"{scope}:row:1:cell:1"),
        (name_row[3], "{{reviewed_by_name}}", "reviewed_name", f"{scope}:row:1:cell:3"),
        (title_row[3], "{{reviewed_by_position}}", "reviewed_position", f"{scope}:row:2:cell:3"),
        (name_row[4], "{{date_reviewed}}", "date_reviewed", f"{scope}:row:1:cell:4"),
    ]

    hits = 0
    for cell, placeholder, reason, location in replacements:
        current_text = _normalize_text(_cell_text(cell))
        if not current_text or PLACEHOLDER_PATTERN.match(current_text):
            continue
        if _replace_cell_text_safely(cell, placeholder):
            _record_placeholder(summary, placeholder, current_text, location, reason=reason)
            hits += 1
    return hits > 0


def _collect_all_table_paragraphs(doc, skip_indices=None):
    """Recursively collect all paragraphs from table cells.

    If *skip_indices* is a set of table indices, those tables are excluded.
    """
    paras = []
    skip = skip_indices or set()

    def _walk(tables, table_offset=0):
        for ti, table in enumerate(tables):
            if (table_offset + ti) in skip:
                continue
            for row in table.rows:
                for cell in row.cells:
                    for para in cell.paragraphs:
                        if para.text.strip():
                            paras.append(para)
                    _walk(cell.tables, 0)
    _walk(doc.tables, 0)
    return paras


def _try_simple_clo_list_table(table, scope, summary):
    """Recognise a simple CLO list table (no alignment columns).

    Handles templates like LP EDUC 010 where CLOs are listed under domain
    headers (Cognitive/Affective/Psychomotor) without a full alignment matrix.
    """
    if len(table.rows) < 3:
        return False

    # Search ALL rows for "COURSE OUTCOMES" header.
    # Also accept rows that contain domain keywords (Cognitive/Affective/
    # Psychomotor) as indicators this is a CLO table.
    header_row_idx = None
    domain_labels = {"cognitive", "affective", "psychomotor"}
    found_domain_in_table = False

    for check_idx in range(len(table.rows)):
        row_texts = [_normalize_text(_cell_text(c)).lower() for c in table.rows[check_idx].cells]
        joined = " ".join(row_texts)
        if "course learning outcome" in joined or "course outcomes" in joined:
            header_row_idx = check_idx
        if any(d in joined for d in domain_labels):
            found_domain_in_table = True

    # Accept table if header found OR if domain labels found (some templates
    # put "COURSE OUTCOMES" outside the table as a paragraph)
    if header_row_idx is None and not found_domain_in_table:
        return False
    if header_row_idx is None:
        # No explicit header row — start from row 0
        header_row_idx = -1

    # Don't steal from the full alignment CLO table recognizer
    all_text = " ".join(
        _normalize_text(_cell_text(c)).lower()
        for row in table.rows for c in row.cells
    )
    if "aligned" in all_text and ("program" in all_text or "plo" in all_text):
        return False

    # Bullet characters to strip (Unicode bullets, Wingdings, dashes, dots)
    BULLET_RE = re.compile(
        r'^[\u2022\u25E6\u25CB\u25CF\u2219\u2023\u2043\u25AA\u25AB'
        r'\u00B7\uF0B7\uF0FC\uF06E\uF0D8'
        r'\-\*\•\◦\\\\]\s*'
    )

    num_replaced = 0
    clo_counter = 0
    current_domain = "cognitive"

    for ri in range(header_row_idx + 1, len(table.rows)):
        row = table.rows[ri]
        if not row.cells:
            continue
        cell_text = _normalize_text(_cell_text(row.cells[0])).strip()
        cell_clean = BULLET_RE.sub('', cell_text).strip()
        cell_lower = cell_clean.lower()

        if not cell_clean:
            continue

        # Domain header row (e.g. "1. Cognitive", "Cognitive Domain")
        if any(d in cell_lower for d in domain_labels):
            for d in domain_labels:
                if d in cell_lower:
                    current_domain = d
                    break
            continue

        # Explicit CLO numbering like "CLO 1" or "CLO1"
        clo_match = re.match(r"clo\s*(\d+)", cell_lower)
        if clo_match:
            clo_counter = int(clo_match.group(1))
        elif len(cell_clean) >= 15 and not PROGRAM_OUTCOME_CODE_RE.search(cell_clean):
            # Looks like a CLO statement (long enough, no PO codes)
            clo_counter += 1
        else:
            continue

        ph = f"{{{{clo_{clo_counter}_statement}}}}"
        if _replace_cell_text_safely(row.cells[0], ph):
            _record_placeholder(summary, ph, cell_clean,
                                f"{scope}:row:{ri}:cell:0",
                                reason=f"simple_clo_list:{current_domain}")
            num_replaced += 1

    return num_replaced > 0


def _is_domain_row(row, domain_labels):
    """Check if a row is a domain header (Cognitive/Affective/Psychomotor)."""
    cell_text = _normalize_text(_cell_text(row.cells[0]) if row.cells else "").strip().lower()
    return any(d in cell_text for d in domain_labels)


def _process_simple_clo_body_paragraphs(paragraphs, scope, summary):
    """Scan body paragraphs for CLO list patterns outside tables.

    Looks for a "COURSE OUTCOMES" heading followed by domain-grouped
    bullet-point CLO statements.
    """
    if not paragraphs:
        return

    BULLET_RE = re.compile(
        r'^[\u2022\u25E6\u25CB\u25CF\u2219\u2023\u2043\u25AA\u25AB'
        r'\u00B7\uF0B7\uF0FC\uF06E\uF0D8'
        r'\-\*\•\◦\\\]\s*'
    )
    domain_labels = {"cognitive", "affective", "psychomotor"}

    # Find the "COURSE OUTCOMES" heading paragraph
    clo_section_start = None
    for pi, para in enumerate(paragraphs):
        text = _normalize_text(para.text).lower()
        if text in ("course outcomes", "course learning outcomes"):
            clo_section_start = pi
            break

    if clo_section_start is None:
        return

    clo_counter = 0
    current_domain = "cognitive"
    replaced = 0

    for pi in range(clo_section_start + 1, len(paragraphs)):
        para = paragraphs[pi]
        text = _normalize_text(para.text).strip()
        clean = BULLET_RE.sub('', text).strip()
        lower = clean.lower()

        if not clean:
            continue

        # Stop if we hit another section heading
        if len(clean) < 40 and any(
            kw in lower for kw in (
                "course ", "program ", "weekly ", "reference",
                "consultation", "signator", "approval", "rubric",
                "learning plan", "time frame", "assessment"
            )
        ):
            break

        # Domain header
        if any(d in lower for d in domain_labels):
            for d in domain_labels:
                if d in lower:
                    current_domain = d
                    break
            continue

        # CLO numbering
        clo_match = re.match(r"clo\s*(\d+)", lower)
        if clo_match:
            clo_counter = int(clo_match.group(1))
        elif len(clean) >= 15 and not PROGRAM_OUTCOME_CODE_RE.search(clean):
            clo_counter += 1
        else:
            continue

        ph = f"{{{{clo_{clo_counter}_statement}}}}"
        loc = f"{scope}:paragraph:{pi}"
        if _replace_paragraph_text_safely(para, ph):
            _record_placeholder(summary, ph, clean, loc,
                                reason=f"body_clo_list:{current_domain}")
            replaced += 1

    return replaced > 0


def _domain_from_text(text):
    """Extract domain from cell text."""
    t = text.lower()
    if "cognitive" in t:
        return "cognitive"
    if "affective" in t:
        return "affective"
    if "psychomotor" in t:
        return "psychomotor"
    return "cognitive"


def _try_clo_table(table, scope, summary):
    """Recognise the CLO mapping table (8-col, header row with
    'COURSE LEARNING OUTCOMES' and 'ALIGNED PROGRAM')."""
    if len(table.columns) < 5:
        return False
    header_texts = [_normalize_text(_cell_text(c)).lower() for c in table.rows[0].cells]
    header_joined = " ".join(header_texts)
    if (
        "course learning outcome" not in header_joined
        and "course outcomes" not in header_joined
        and "clo" not in header_joined
    ):
        return False
    checkmark_mode = False
    if "aligned" not in header_joined and "program" not in header_joined:
        po_like_headers = [
            ht for ht in header_texts[1:]
            if PROGRAM_OUTCOME_CODE_RE.search(ht or "")
        ]
        if len(po_like_headers) >= 2:
            checkmark_mode = True
        else:
            return False
    # region agent log
    _debug_log(
        "template-profile-checkmark",
        "H1",
        "template_ai_service.py:_try_clo_table",
        "CLO table mode detection",
        {
            "scope": scope,
            "checkmark_mode": checkmark_mode,
            "header_joined_sample": header_joined[:180],
            "columns": len(table.columns),
            "rows": len(table.rows),
        },
    )
    # endregion

    # Build column-index → suffix map from headers
    col_suffix = {}
    for ci, ht in enumerate(header_texts):
        if checkmark_mode:
            if ci == 0:
                continue  # col 0 is the CLO/category label in checkmark matrices
        elif ci <= 1:
            continue  # cols 0-1 are CLO code + statement
        if checkmark_mode:
            # In checkmark mode, treat all columns after col 0 as potential alignment columns
            # unless they are explicitly course outcome statement columns.
            if ci > 0 and (not ht or len(ht) <= 40 or PROGRAM_OUTCOME_CODE_RE.search(ht)):
                col_suffix[ci] = _checkmark_placeholder_suffix(ht)
                continue
        for keyword, suffix in CLO_COLUMN_MAP.items():
            if keyword in ht:
                col_suffix[ci] = suffix
                break
    checkmark_group_index = None
    if checkmark_mode:
        checkmark_group_index = int(summary.get("_clo_checkmark_group_index", 0)) + 1
        summary["_clo_checkmark_group_index"] = checkmark_group_index

    clo_number = 0
    domain_labels = {"cognitive", "affective", "psychomotor"}
    for ri, row in enumerate(table.rows[1:], start=1):
        cell0_text = _normalize_text(_cell_text(row.cells[0])).strip()
        cell0_lower = cell0_text.lower()

        # Skip domain separator rows (COGNITIVE / AFFECTIVE / PSYCHOMOTOR)
        if _is_merged_row(row) or cell0_lower in domain_labels:
            continue

        # Detect CLO row: cell[0] starts with "CLO" or matches "CLO N"
        clo_match = re.match(r"clo\s*(\d+)", cell0_lower)
        if clo_match:
            clo_number = int(clo_match.group(1))
        elif checkmark_mode and (cell0_text or clo_number > 0) and not PROGRAM_OUTCOME_CODE_RE.search(cell0_text):
            # In checkmark mode, if we haven't seen a code yet, look for any non-empty cell0.
            # If we are already in the block, allow empty cell0 if the row has alignment data.
            if not cell0_text:
                has_alignment_data = any(_normalize_text(_cell_text(cell)) for cell in row.cells[1:])
                if not has_alignment_data:
                    continue
            clo_number += 1
        else:
            # Skip non-CLO rows (could be spacing or nested headers)
            continue
        if clo_number < 1 or clo_number > 24:
            continue

        # Col 0 → clo_N_code (the "CLO 1" label)
        ph_code = "{{" + f"clo_{clo_number}_code" + "}}"
        loc0 = f"{scope}:row:{ri}:cell:0"
        if not PLACEHOLDER_PATTERN.match(cell0_text):
            _replace_cell_text_safely(row.cells[0], ph_code)
            _record_placeholder(summary, ph_code, cell0_text, loc0, reason="clo_code")

        # Col 1 → clo_N_statement for narrative CLO tables.  In checkmark
        # matrices col 1 can be the first PO column and must stay available
        # for an alignment placeholder.
        if not checkmark_mode and len(row.cells) > 1:
            cell1 = row.cells[1]
            cell1_text = _normalize_text(_cell_text(cell1))
            if cell1_text and not PLACEHOLDER_PATTERN.match(cell1_text):
                ph_stmt = "{{" + f"clo_{clo_number}_statement" + "}}"
                loc1 = f"{scope}:row:{ri}:cell:1"
                if _replace_cell_text_safely(cell1, ph_stmt):
                    _record_placeholder(summary, ph_stmt, cell1_text, loc1, reason="clo_statement")

        # Remaining cols → mapped suffixes
        for ci, suffix in col_suffix.items():
            if ci >= len(row.cells):
                continue
            cell = row.cells[ci]
            ct = _normalize_text(_cell_text(cell))
            if checkmark_mode:
                # Use absolute column index for stable mapping to the 14-column block
                # Col 1 -> x111, Col 14 -> x11e
                ph_name = _compact_clo_checkmark_placeholder(checkmark_group_index or 1, clo_number, ci)
                ph = "{{" + ph_name + "}}"
                if _replace_cell_text_safely(cell, ph, clear_style=True):
                    _record_placeholder(summary, ph, ct, f"{scope}:row:{ri}:cell:{ci}", reason=f"clo_{suffix}:alias_for:clo_{clo_number}_{suffix}")
            else:
                if PLACEHOLDER_PATTERN.match(ct):
                    continue
                ph_name = f"clo_{clo_number}_{suffix}"
                ph = "{{" + ph_name + "}}"
                loc = f"{scope}:row:{ri}:cell:{ci}"
                if _replace_cell_text_safely(cell, ph):
                    _record_placeholder(summary, ph, ct, loc, reason=f"clo_{suffix}")

    return clo_number > 0


def _checkmark_placeholder_suffix(header_text):
    raw = _normalize_text(header_text).lower()
    safe = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    return safe or "po"


def _try_program_institutional_checkmark_table(table, scope, summary):
    if len(table.rows) < 3 or len(table.columns) < 4:
        return False
    header_texts = [_normalize_text(_cell_text(c)) for c in table.rows[0].cells]
    joined = " ".join(text.lower() for text in header_texts if text).strip()
    if "outcome" not in joined and "institutional" not in joined and "program" not in joined:
        return False
    if "course outcome" in joined or "course learning outcome" in joined or "clo" in joined:
        return False

    code_col = None
    desc_col = None
    alignment_cols = []
    alignment_col_labels = []
    for ci, text in enumerate(header_texts):
        lowered = text.lower()
        if code_col is None and ("code" in lowered or lowered in {"no", "item"}):
            code_col = ci
            continue
        if desc_col is None and ("description" in lowered or ("outcome" in lowered and "code" not in lowered)):
            desc_col = ci
            continue
    # Find the range of alignment columns
    first_po_col = None
    last_po_col = None
    for ci, text in enumerate(header_texts):
        if ci in {code_col, desc_col}:
            continue
        normalized = normalize_alignment_column_label(text)
        if normalized and 1 <= len(normalized) <= 12:
            if first_po_col is None:
                first_po_col = ci
            last_po_col = ci

    if first_po_col is not None and last_po_col is not None:
        for ci in range(first_po_col, last_po_col + 1):
            if ci in {code_col, desc_col}:
                continue
            alignment_cols.append(ci)
            alignment_col_labels.append(header_texts[ci] or f"PO_{ci}")
    if len(alignment_cols) < 2:
        return False
    deduped_col_keys = normalize_duplicate_alignment_columns(alignment_col_labels)
    col_key_by_index = {}
    for ci, deduped in zip(alignment_cols, deduped_col_keys):
        col_key = normalize_alignment_column_label(deduped)
        if col_key:
            col_key_by_index[ci] = col_key

    detected_rows = []
    for ri, row in enumerate(table.rows[1:], start=1):
        code_text = _normalize_text(_cell_text(row.cells[code_col])) if code_col is not None and code_col < len(row.cells) else ""
        row_key = normalize_alignment_row_label(code_text)
        if not row_key or not re.search(r"\d", row_key):
            continue
        detected_rows.append((ri, row_key))
    if len(detected_rows) < 2:
        return False
    # region agent log
    _debug_log(
        "template-profile-checkmark",
        "H2",
        "template_ai_service.py:_try_program_institutional_checkmark_table",
        "Program-institutional table detected",
        {
            "scope": scope,
            "detected_rows": len(detected_rows),
            "alignment_cols": len(alignment_cols),
            "raw_labels": alignment_col_labels[:10],
            "deduped_keys": deduped_col_keys[:10],
        },
    )
    # endregion

    alignment_index = int(summary.get("_program_institutional_index", 0)) + 1
    summary["_program_institutional_index"] = alignment_index
    alignment_id = f"program_institutional_alignment_{alignment_index}"
    hits = 0

    for ri, row_key in detected_rows:
        row = table.rows[ri]
        for ci in alignment_cols:
            if ci >= len(row.cells):
                continue
            cell = row.cells[ci]
            current = _normalize_text(_cell_text(cell))
            col_key = col_key_by_index.get(ci)
            if not col_key:
                continue
            placeholder = "{{" + f"{alignment_id}_{row_key}_{col_key}" + "}}"
            loc = f"{scope}:row:{ri}:cell:{ci}"
            if _replace_cell_text_safely(cell, placeholder, clear_style=True):
                _record_placeholder(summary, placeholder, current, loc, reason="program_institutional_checkmark")
                hits += 1
    # region agent log
    _debug_log(
        "template-profile-checkmark",
        "H3",
        "template_ai_service.py:_try_program_institutional_checkmark_table",
        "Program-institutional replacement summary",
        {
            "scope": scope,
            "hits": hits,
            "alignment_id": alignment_id,
            "example_row_key": detected_rows[0][1] if detected_rows else "",
            "example_col_key": next(iter(col_key_by_index.values()), ""),
        },
    )
    # endregion
    return hits > 0


def _try_weekly_plan_table(table, scope, summary):
    """Recognise the weekly plan table.
    Structure: repeating pairs of rows —
      [label header: TIME FRAME | ILO | Topics | TLA | Assessment | Resources]
      [data row:     Week N     | ... | ...    | ... | ...        | ...       ]
    Plus exam separator rows (PRELIMINARY/MIDTERM/FINAL EXAMINATION)."""
    if len(table.rows) < 4 or len(table.columns) < 4:
        return False

    # Heuristic: check if any cell in the first few rows says "week"
    sample = " ".join(
        _normalize_text(_cell_text(c)).lower()
        for r in table.rows[:6] for c in r.cells
    )
    if "week" not in sample:
        return False
    if "topic" not in sample and "tla" not in sample and "teaching" not in sample:
        return False

    hits = 0
    current_col_map = {}  # col_index → suffix
    ri = 0

    while ri < len(table.rows):
        row = table.rows[ri]
        cells = row.cells
        cell0_text = _normalize_text(_cell_text(cells[0]))
        cell0_lower = cell0_text.lower().strip()

        # --- Exam separator row ---
        exam_key = _exam_row_key(row)
        if exam_key:
            ph = "{{" + exam_key + "}}"
            loc = f"{scope}:row:{ri}:cell:0"
            # Exam rows should remain exam separators even when col 0 carries
            # the week number for the examination period.
            col0_ph = ph
            current_exam_text = _normalize_text(_cell_text(cells[0]))
            if current_exam_text != col0_ph:
                _replace_cell_text_safely(cells[0], col0_ph)
                _record_placeholder(summary, col0_ph, cell0_text, loc, reason="exam_week_label")
            # Cols 1+: clear repeated exam labels so the separator appears once.
            for ci in range(1, len(cells)):
                cell_ci_text = _normalize_text(_cell_text(cells[ci]))
                if cell_ci_text:
                    _replace_cell_text_safely(cells[ci], "")
            _record_placeholder(summary, ph, cell0_text, f"{scope}:row:{ri}:cell:1", reason="exam_label")
            hits += 1
            ri += 1
            continue

        # --- Label header row (TIME FRAME | ILO | Topics | ...) ---
        if "time" in cell0_lower and "frame" in cell0_lower:
            current_col_map = {}
            for ci in range(len(cells)):
                ht = _normalize_text(_cell_text(cells[ci])).lower()
                if ci == 0:
                    # Column 0 is the TIME FRAME column itself
                    current_col_map[ci] = "time_frame"
                else:
                    for keyword, suffix in WEEK_COLUMN_MAP.items():
                        if keyword in ht:
                            current_col_map[ci] = suffix
                            break
            ri += 1
            continue

        # --- Week data row ---
        week_key = _week_label_to_key(cell0_text)
        pair_key, pair_row = _split_week_pair_key(table, ri)
        if pair_key:
            segments = _row_cell_segments(row)
            pair_segments = _row_cell_segments(pair_row)
            if len(segments) >= 6:
                effective_targets = {
                    0: (segments[0], pair_segments[0] if len(pair_segments) > 0 else None, "time_frame"),
                    1: (segments[1], pair_segments[1] if len(pair_segments) > 1 else None, "ilo"),
                    2: (segments[2], pair_segments[2] if len(pair_segments) > 2 else None, "topics"),
                    3: (segments[3], pair_segments[3] if len(pair_segments) > 3 else None, "tla"),
                    4: (segments[4], pair_segments[4] if len(pair_segments) > 4 else None, "assessment"),
                    5: (segments[5], pair_segments[5] if len(pair_segments) > 5 else None, "learning_resources"),
                }
            else:
                effective_map = dict(current_col_map)
                if not effective_map and len(cells) >= 6:
                    effective_map = {0: "time_frame", 1: "ilo", 2: "topics", 3: "tla", 4: "assessment", 5: "learning_resources"}
                effective_targets = {
                    ci: (cells[ci], pair_row.cells[ci] if ci < len(pair_row.cells) else None, suffix)
                    for ci, suffix in effective_map.items()
                    if ci < len(cells)
                }

            for ci, (cell, pair_cell, suffix) in effective_targets.items():
                combined_original = _combine_week_pair_cell_text(cell, pair_cell)
                ph = "{{" + f"week_{pair_key}_{suffix}" + "}}"
                loc = f"{scope}:row:{ri}:cell:{ci}"
                if _replace_cell_text_safely(cell, ph):
                    _record_placeholder(summary, ph, combined_original, loc, reason=f"weekly:{suffix}:paired")
                    hits += 1
                else:
                    _warn(summary, f"Skipped unsafe paired weekly replacement at {loc}.")
                if pair_cell is not None:
                    _replace_cell_text_safely(pair_cell, "")
            ri += 2
            continue

        if not week_key:
            ri += 1
            continue

        # Col 0 stays literal (the "Week N" label itself)
        # Col 1+ get replaced using current_col_map
        # If col_map is empty, try inferring from previous headers
        # For the ILO column (col 1, usually "Intended Learning Outcomes")
        # we use "ilo" suffix if not already mapped
        segments = _row_cell_segments(row)
        if len(segments) >= 6:
            effective_targets = {
                0: (segments[0], "time_frame"),
                1: (segments[1], "ilo"),
                2: (segments[2], "topics"),
                3: (segments[3], "tla"),
                4: (segments[4], "assessment"),
                5: (segments[5], "learning_resources"),
            }
        else:
            effective_map = dict(current_col_map)
            if not effective_map and len(cells) >= 6:
                # Fallback: assume standard 6-column layout
                effective_map = {1: "ilo", 2: "topics", 3: "tla", 4: "assessment", 5: "learning_resources"}
            effective_targets = {
                ci: (cells[ci], suffix)
                for ci, suffix in effective_map.items()
                if ci < len(cells)
            }

        for ci, (cell, suffix) in effective_targets.items():
            # Skip time_frame for weeks that don't have it in the target schema
            ct = _normalize_text(_cell_text(cell))
            if PLACEHOLDER_PATTERN.match(ct):
                continue
            ph = "{{" + f"week_{week_key}_{suffix}" + "}}"
            loc = f"{scope}:row:{ri}:cell:{ci}"
            if _replace_cell_text_safely(cell, ph):
                _record_placeholder(summary, ph, ct, loc, reason=f"weekly:{suffix}")
                hits += 1
            else:
                _warn(summary, f"Skipped unsafe weekly replacement at {loc}.")

        ri += 1

    return hits > 0


def _try_consultation_table(table, scope, summary):
    """Recognise the consultation hours table (3-col: DAYS | TIME | ROOM)."""
    if len(table.columns) < 3 or len(table.rows) < 2:
        return False
    header_texts = [_normalize_text(_cell_text(c)).lower() for c in table.rows[0].cells]
    header_joined = " ".join(header_texts)
    # Accept both "Days | Time | Room" and GEC-style "Name of Instructor | Time | Room".
    has_first_col = ("days" in header_joined or "instructor" in header_joined or "name" in header_joined)
    has_time_col = ("time" in header_joined or "availability" in header_joined)
    if not has_first_col or not has_time_col:
        return False

    # Find which columns are time and room
    time_col = None
    room_col = None
    for ci, ht in enumerate(header_texts):
        if "time" in ht or "availability" in ht:
            time_col = ci
        if "room" in ht:
            room_col = ci

    slot = 0
    for ri, row in enumerate(table.rows[1:], start=1):
        slot += 1
        if slot > 3:
            break
        days_cell = row.cells[0]
        days_text = _normalize_text(_cell_text(days_cell))
        if not days_text:
            continue

        # Days column → consultation_slot_N_days
        ph_days = "{{" + f"consultation_slot_{slot}_days" + "}}"
        loc_days = f"{scope}:row:{ri}:cell:0"
        if not PLACEHOLDER_PATTERN.match(days_text):
            _replace_cell_text_safely(days_cell, ph_days)
            _record_placeholder(summary, ph_days, days_text, loc_days, reason="consultation_days")

        # Time column
        if time_col is not None and time_col < len(row.cells):
            tc = row.cells[time_col]
            tt = _normalize_text(_cell_text(tc))
            if tt and not PLACEHOLDER_PATTERN.match(tt):
                ph_time = "{{" + f"consultation_slot_{slot}_time" + "}}"
                loc_time = f"{scope}:row:{ri}:cell:{time_col}"
                _replace_cell_text_safely(tc, ph_time)
                _record_placeholder(summary, ph_time, tt, loc_time, reason="consultation_time")

        # Room column
        if room_col is not None and room_col < len(row.cells):
            rc = row.cells[room_col]
            rt = _normalize_text(_cell_text(rc))
            if rt and not PLACEHOLDER_PATTERN.match(rt):
                ph_room = "{{" + f"consultation_slot_{slot}_room" + "}}"
                loc_room = f"{scope}:row:{ri}:cell:{room_col}"
                _replace_cell_text_safely(rc, ph_room)
                _record_placeholder(summary, ph_room, rt, loc_room, reason="consultation_room")

    return slot > 0


def _try_signatory_table(table, scope, summary):
    """Recognise the signatory table (NAME | POSITION | SIGNATURE | DATE)."""
    if len(table.columns) < 3 or len(table.rows) < 3:
        return False
    header_texts = [_normalize_text(_cell_text(c)).lower() for c in table.rows[0].cells]
    header_joined = " ".join(header_texts)
    if "name" not in header_joined or ("position" not in header_joined and "designation" not in header_joined):
        return False

    # Find name and position column indices
    name_col = None
    pos_col = None
    for ci, ht in enumerate(header_texts):
        if "name" in ht:
            name_col = ci
        if "position" in ht or "designation" in ht:
            pos_col = ci
    non_data_cols = [
        ci for ci, ht in enumerate(header_texts)
        if ci not in {0, name_col, pos_col}
        and any(token in ht for token in ("signature", "signed", "date", "initial"))
    ]

    def clear_non_data_cells(row):
        for ci in non_data_cols:
            if ci < len(row.cells):
                for para in row.cells[ci].paragraphs:
                    _clear_paragraph_images(para)
                _replace_cell_text_safely(row.cells[ci], "", clear_style=True)

    hits = 0
    last_was_signatory = False
    for ri, row in enumerate(table.rows[1:], start=1):
        role_text = _normalize_text(_cell_text(row.cells[0])).lower().strip().rstrip(":")
        if not role_text:
            # Continuation row (empty col 0) — clear name/position cells if they
            # follow a processed signatory row (removes extra instructors from GEC).
            if last_was_signatory:
                for ci in [name_col, pos_col]:
                    if ci is not None and ci < len(row.cells):
                        ct = _normalize_text(_cell_text(row.cells[ci]))
                        if ct and not PLACEHOLDER_PATTERN.match(ct):
                            _replace_cell_text_safely(row.cells[ci], "")
                clear_non_data_cells(row)
            continue
        canon_prefix = SIGNATORY_MAP.get(role_text)
        if not canon_prefix:
            last_was_signatory = False
            continue

        # Name column
        if name_col is not None and name_col < len(row.cells):
            nc = row.cells[name_col]
            nt = _normalize_text(_cell_text(nc))
            if nt and not PLACEHOLDER_PATTERN.match(nt):
                for para in nc.paragraphs:
                    _clear_paragraph_images(para)
                ph = "{{" + f"{canon_prefix}_name" + "}}"
                loc = f"{scope}:row:{ri}:cell:{name_col}"
                _replace_cell_text_safely(nc, ph)
                _record_placeholder(summary, ph, nt, loc, reason=f"signatory:{role_text}")
                hits += 1

        # Position column
        if pos_col is not None and pos_col < len(row.cells):
            pc = row.cells[pos_col]
            pt = _normalize_text(_cell_text(pc))
            if pt and not PLACEHOLDER_PATTERN.match(pt):
                for para in pc.paragraphs:
                    _clear_paragraph_images(para)
                ph = "{{" + f"{canon_prefix}_position" + "}}"
                loc = f"{scope}:row:{ri}:cell:{pos_col}"
                _replace_cell_text_safely(pc, ph)
                _record_placeholder(summary, ph, pt, loc, reason=f"signatory_pos:{role_text}")
                hits += 1
        clear_non_data_cells(row)
        last_was_signatory = True

    return hits > 0


def _process_label_value_paragraphs(paragraphs, scope, summary):
    for pi, paragraph in enumerate(paragraphs):
        text = _normalize_text(paragraph.text)
        if not text or PLACEHOLDER_PATTERN.match(text):
            continue

        semester_match = SEMESTER_YEAR_PATTERN.match(text)
        if semester_match:
            new_text = "{{semester_label}}, Academic Year {{academic_year}}"
            if _replace_paragraph_text_safely(paragraph, new_text):
                _record_placeholder(summary, "{{semester_label}}", semester_match.group("semester"), f"{scope}:paragraph:{pi}", reason="semester_label")
                _record_placeholder(summary, "{{academic_year}}", semester_match.group("year"), f"{scope}:paragraph:{pi}", reason="academic_year")
            continue

        match = LABEL_VALUE_PATTERN.match(text)
        if not match:
            continue
        label = _normalize_text(match.group("label")).lower()
        value = _normalize_text(match.group("value"))
        if not value or PLACEHOLDER_PATTERN.match(value):
            continue
        placeholder_name = _placeholder_name_from_label(label)
        if not placeholder_name:
            continue
        if label not in COURSE_INFO_MAP:
            if len(value) > 120:
                continue
            if any(marker in value for marker in ("•", "\n", " | ")):
                continue
        placeholder = "{{" + placeholder_name + "}}"
        new_text = f"{match.group('label').strip()}: {placeholder}"
        if _replace_paragraph_text_safely(paragraph, new_text):
            _record_placeholder(summary, placeholder, value, f"{scope}:paragraph:{pi}", reason=f"paragraph_label:{label}")


def _replace_text_in_paragraph_runs(paragraph, old_text, new_text):
    """Replace *old_text* with *new_text* across runs, preserving per-run formatting.
    
    Handles names that span multiple runs (e.g. "First" in Run 0 + " Last" in Run 1).
    Replaces only the affected portions of the spanning runs, preserving all non-matching
    runs (tabs, separators, underscores) and their formatting.
    """
    if not old_text or not paragraph.runs:
        return False
    
    # Fast path: check if any single run contains the full text
    for run in paragraph.runs:
        if old_text in run.text:
            run.text = run.text.replace(old_text, new_text)
            return True
    
    # Slow path: text spans multiple runs
    # Build a concatenated view of all run texts with run boundaries
    run_texts = [run.text for run in paragraph.runs]
    full_text = "".join(run_texts)
    
    if old_text not in full_text:
        # Try normalized (strip whitespace) match
        if old_text.strip() not in full_text:
            return False
        old_text = old_text.strip()
        if old_text not in full_text:
            return False
    
    start_idx = full_text.index(old_text)
    end_idx = start_idx + len(old_text)
    
    # Map character positions back to runs
    pos = 0
    for ri, rt in enumerate(run_texts):
        run_start = pos
        run_end = pos + len(rt)
        
        if run_end <= start_idx or run_start >= end_idx:
            # This run is entirely outside the replacement range
            pos = run_end
            continue
        
        # This run overlaps with the replacement range
        overlap_start = max(0, start_idx - run_start)
        overlap_end = min(len(rt), end_idx - run_start)
        
        # Replace only the overlapping portion
        old_part = rt[overlap_start:overlap_end]
        if old_part:
            # Calculate what portion of new_text goes here
            # For simplicity, put all of new_text in the first overlapping run
            # and clear the rest
            if ri == 0 or (ri > 0 and run_start <= start_idx < run_end):
                # First overlapping run gets the replacement
                paragraph.runs[ri].text = rt[:overlap_start] + new_text + rt[overlap_end:]
            else:
                # Subsequent overlapping runs get their portion cleared
                paragraph.runs[ri].text = rt[:overlap_start] + rt[overlap_end:]
        
        pos = run_end
    
    return True


def _signatory_text_parts(text):
    parts = re.split(r'\s{3,}|\t+', text or "")
    return [p.strip() for p in parts if p.replace('_', '').strip()]


def _first_nonempty_run_props(paragraph):
    from app.services.template_writer import _clone_run_props

    for run in paragraph.runs:
        if _normalize_text(run.text):
            return _clone_run_props(run)
    if paragraph.runs:
        return _clone_run_props(paragraph.runs[0])
    return None


def _reset_tab_stops(paragraph, positions):
    p_pr = paragraph._p.get_or_add_pPr()
    tabs = p_pr.find(qn("w:tabs"))
    if tabs is not None:
        p_pr.remove(tabs)
    for position in positions:
        paragraph.paragraph_format.tab_stops.add_tab_stop(Inches(position))


def _write_paragraph_runs(paragraph, pieces, *, tab_stops=None, alignment=None):
    from app.services.template_writer import _apply_run_props

    _clear_paragraph_list_formatting(paragraph)
    _clear_paragraph_content(paragraph)
    if alignment is not None:
        paragraph.alignment = alignment
    if tab_stops is not None:
        _reset_tab_stops(paragraph, tab_stops)
    for text, run_props in pieces:
        run = paragraph.add_run(text)
        if run_props is not None:
            _apply_run_props(run, run_props)


def _delete_paragraph_range(paragraphs, start_index, end_index):
    for paragraph in paragraphs[start_index:end_index + 1]:
        parent = paragraph._element.getparent()
        if parent is not None:
            parent.remove(paragraph._element)


def _set_table_borders_none(table):
    tbl_pr = table._tbl.tblPr
    existing = tbl_pr.find(qn("w:tblBorders"))
    if existing is not None:
        tbl_pr.remove(existing)
    borders = OxmlElement("w:tblBorders")
    for edge_name in ("top", "left", "bottom", "right", "insideH", "insideV"):
        edge = OxmlElement(f"w:{edge_name}")
        edge.set(qn("w:val"), "nil")
        borders.append(edge)
    tbl_pr.append(borders)


def _set_table_layout_fixed(table, column_widths):
    table.autofit = False
    tbl_pr = table._tbl.tblPr
    existing_layout = tbl_pr.find(qn("w:tblLayout"))
    if existing_layout is not None:
        tbl_pr.remove(existing_layout)
    tbl_layout = OxmlElement("w:tblLayout")
    tbl_layout.set(qn("w:type"), "fixed")
    tbl_pr.append(tbl_layout)

    total_width = sum(Inches(width).twips for width in column_widths)
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:type"), "dxa")
    tbl_w.set(qn("w:w"), str(int(total_width)))

    grid = table._tbl.tblGrid
    if grid is not None:
        for grid_col, width in zip(grid.gridCol_lst, column_widths):
            grid_col.set(qn("w:w"), str(int(Inches(width).twips)))

    for row in table.rows:
        for cell, width in zip(row.cells, column_widths):
            cell.width = Inches(width)
            tc_pr = cell._tc.get_or_add_tcPr()
            tc_w = tc_pr.find(qn("w:tcW"))
            if tc_w is None:
                tc_w = OxmlElement("w:tcW")
                tc_pr.append(tc_w)
            tc_w.set(qn("w:type"), "dxa")
            tc_w.set(qn("w:w"), str(int(Inches(width).twips)))


def _set_table_row_height(row, height_inches):
    row.height = Inches(height_inches)
    row.height_rule = WD_ROW_HEIGHT_RULE.EXACTLY


def _write_cell_runs(cell, pieces, *, alignment=None):
    while len(cell.paragraphs) > 1:
        paragraph = cell.paragraphs[-1]
        paragraph._element.getparent().remove(paragraph._element)
    paragraph = cell.paragraphs[0]
    _write_paragraph_runs(paragraph, pieces, tab_stops=[], alignment=alignment)
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.line_spacing = 1


def _insert_borderless_signatory_table(paragraph, column_widths, row_heights):
    table = paragraph._parent.add_table(rows=len(row_heights), cols=len(column_widths), width=Inches(sum(column_widths)))
    paragraph._p.addprevious(table._tbl)
    _set_table_borders_none(table)
    _set_table_layout_fixed(table, column_widths)
    for row, height in zip(table.rows, row_heights):
        _set_table_row_height(row, height)
    return table


def _cell_has_drawing(cell):
    return bool(cell._tc.xpath(".//w:drawing"))


def _paragraph_has_drawing(paragraph):
    return bool(paragraph._p.xpath(".//w:drawing"))


def _copy_first_drawing_paragraph(cell, target_cell):
    source_paragraph = None
    for paragraph in cell.paragraphs:
        if _paragraph_has_drawing(paragraph):
            source_paragraph = paragraph
            break
    if source_paragraph is None:
        return False

    target_paragraph = target_cell.paragraphs[0]
    _clear_paragraph_content(target_paragraph)
    target_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    target_paragraph.paragraph_format.space_before = Pt(0)
    target_paragraph.paragraph_format.space_after = Pt(0)
    for run in source_paragraph.runs:
        if run._r.xpath(".//w:drawing"):
            target_paragraph._p.append(deepcopy(run._r))
    return True


def _title_paragraph_sources(table):
    sources = []
    for row in table.rows:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                text = _normalize_text(paragraph.text)
                if text and not _paragraph_has_drawing(paragraph):
                    sources.append((paragraph, text.strip()))
    return sources


def _write_header_title_cell(cell, sources):
    while len(cell.paragraphs) > 1:
        paragraph = cell.paragraphs[-1]
        paragraph._element.getparent().remove(paragraph._element)
    first = True
    for source_paragraph, text in sources:
        target_paragraph = cell.paragraphs[0] if first else cell.add_paragraph()
        first = False
        props = _first_nonempty_run_props(source_paragraph)
        _write_paragraph_runs(
            target_paragraph,
            [(text, props)],
            tab_stops=[],
            alignment=WD_ALIGN_PARAGRAPH.CENTER,
        )
        target_paragraph.paragraph_format.space_before = Pt(0)
        target_paragraph.paragraph_format.space_after = Pt(0)
        target_paragraph.paragraph_format.line_spacing = 1


def _normalize_logo_title_header_tables(doc, summary):
    """Convert fragile logo/title header tables into true logo-title-logo columns."""
    for table in list(doc.tables[:3]):
        if len(table.columns) != 2 or not table.rows:
            continue
        joined = _normalize_text("\n".join(cell.text for row in table.rows for cell in row.cells)).upper()
        if "COURSE LEARNING PLAN" not in joined or "UNIVERSITY" not in joined:
            continue
        drawing_cells = [cell for row in table.rows for cell in row.cells if _cell_has_drawing(cell)]
        if len(drawing_cells) < 2:
            continue
        title_sources = _title_paragraph_sources(table)
        if len(title_sources) < 2:
            continue

        parent = table._tbl.getparent()
        new_table = table._parent.add_table(rows=1, cols=3, width=Inches(10))
        table._tbl.addprevious(new_table._tbl)
        _set_table_borders_none(new_table)
        _set_table_layout_fixed(new_table, [2.0, 6.0, 2.0])
        _set_table_row_height(new_table.rows[0], 1.25)
        for cell in new_table.rows[0].cells:
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER

        _copy_first_drawing_paragraph(drawing_cells[0], new_table.rows[0].cells[0])
        _write_header_title_cell(new_table.rows[0].cells[1], title_sources)
        _copy_first_drawing_paragraph(drawing_cells[1], new_table.rows[0].cells[2])
        parent.remove(table._tbl)
        summary.setdefault("layout_normalizations", []).append("logo_title_header_table")
        return True
    return False


def _record_signatory_placeholder(summary, placeholder, original_text, location, reason):
    original = _normalize_text(original_text)
    if original and not PLACEHOLDER_PATTERN.match(original):
        _record_placeholder(summary, placeholder, original, location, reason)


def _process_tabbed_multicolumn_signatory_block(paragraphs, scope, summary):
    """Normalize Education-style tabbed signatory paragraphs.

    Some source templates place the first-page signatories in plain
    paragraphs with tab characters instead of a table.  Replacing only the
    names leaves the shorter placeholder text to shift the later columns.
    This replaces the matching block with a fixed-width borderless table while
    preserving the original signatory roles and blank date lines.
    """
    nonempty = [
        (index, paragraph, _normalize_text(paragraph.text))
        for index, paragraph in enumerate(paragraphs)
        if _normalize_text(paragraph.text)
    ]

    def find_label(*phrases):
        for pos, (index, paragraph, text) in enumerate(nonempty):
            lowered = text.lower()
            if all(phrase in lowered for phrase in phrases):
                return pos, index, paragraph
        return None

    prepared = find_label("prepared and submitted by", "date submitted")
    reviewed = find_label("reviewed by", "date reviewed", "endorsed by", "date endorsed")
    approved = find_label("approved by")
    if not prepared or not reviewed or not approved:
        return False

    try:
        prepared_pos, prepared_label_index, prepared_label = prepared
        reviewed_pos, reviewed_label_index, reviewed_label = reviewed
        approved_pos, approved_label_index, approved_label = approved

        prepared_name_index, prepared_name = nonempty[prepared_pos + 1][0], nonempty[prepared_pos + 1][1]
        prepared_title_index, prepared_title = nonempty[prepared_pos + 2][0], nonempty[prepared_pos + 2][1]
        reviewed_name_index, reviewed_name = nonempty[reviewed_pos + 1][0], nonempty[reviewed_pos + 1][1]
        reviewed_title_index, reviewed_title = nonempty[reviewed_pos + 2][0], nonempty[reviewed_pos + 2][1]
        approved_name_index, approved_name = nonempty[approved_pos + 1][0], nonempty[approved_pos + 1][1]
        approved_title_index, approved_title = nonempty[approved_pos + 2][0], nonempty[approved_pos + 2][1]
    except (IndexError, TypeError):
        return False

    reviewed_names = _signatory_text_parts(reviewed_name.text)
    reviewed_titles = _signatory_text_parts(reviewed_title.text)
    if len(reviewed_names) < 2 or len(reviewed_titles) < 2:
        return False

    prepared_name_parts = _signatory_text_parts(prepared_name.text)
    prepared_title_parts = _signatory_text_parts(prepared_title.text)
    approved_name_parts = _signatory_text_parts(approved_name.text)
    approved_title_parts = _signatory_text_parts(approved_title.text)

    label_props = _first_nonempty_run_props(prepared_label)
    name_props = _first_nonempty_run_props(prepared_name)
    title_props = _first_nonempty_run_props(prepared_title)
    reviewed_name_props = _first_nonempty_run_props(reviewed_name)
    reviewed_title_props = _first_nonempty_run_props(reviewed_title)
    approved_name_props = _first_nonempty_run_props(approved_name)
    approved_title_props = _first_nonempty_run_props(approved_title)

    signatory_table = _insert_borderless_signatory_table(
        prepared_label,
        column_widths=[3.75, 1.50, 3.25, 1.50],
        row_heights=[
            0.24, 0.22, 0.22, 0.20,
            0.48, 0.24, 0.22, 0.22, 0.20,
            0.44, 0.24, 0.22, 0.22, 0.20,
        ],
    )

    _write_cell_runs(signatory_table.cell(0, 0), [("Prepared and Submitted by:", label_props)])
    _write_cell_runs(signatory_table.cell(0, 2), [("Date Submitted:", label_props)])
    _write_cell_runs(signatory_table.cell(2, 0), [("{{prepared_by_name}}", name_props)])
    _write_cell_runs(signatory_table.cell(2, 2), [("___", None)])
    _write_cell_runs(signatory_table.cell(3, 0), [("{{prepared_by_position}}", title_props)])

    _write_cell_runs(signatory_table.cell(5, 0), [("Reviewed by:", label_props)])
    _write_cell_runs(signatory_table.cell(5, 1), [("Date Reviewed:", label_props)])
    _write_cell_runs(signatory_table.cell(5, 2), [("Endorsed by:", label_props)])
    _write_cell_runs(signatory_table.cell(5, 3), [("Date Endorsed:", label_props)])
    _write_cell_runs(signatory_table.cell(7, 0), [("{{reviewed_by_name}}", reviewed_name_props)])
    _write_cell_runs(signatory_table.cell(7, 1), [("___________", None)])
    _write_cell_runs(signatory_table.cell(7, 2), [("{{endorsed_by_name}}", reviewed_name_props)])
    _write_cell_runs(signatory_table.cell(7, 3), [("___________", None)])
    _write_cell_runs(signatory_table.cell(8, 0), [("{{reviewed_by_position}}", reviewed_title_props)])
    _write_cell_runs(signatory_table.cell(8, 2), [("{{endorsed_by_position}}", reviewed_title_props)])

    approved_label_cell = signatory_table.cell(10, 0).merge(signatory_table.cell(10, 3))
    approved_name_cell = signatory_table.cell(12, 0).merge(signatory_table.cell(12, 3))
    approved_title_cell = signatory_table.cell(13, 0).merge(signatory_table.cell(13, 3))
    _write_cell_runs(approved_label_cell, [("Approved by:", label_props)], alignment=WD_ALIGN_PARAGRAPH.CENTER)
    _write_cell_runs(approved_name_cell, [("{{approved_by_name}}", approved_name_props)], alignment=WD_ALIGN_PARAGRAPH.CENTER)
    _write_cell_runs(approved_title_cell, [("{{approved_by_position}}", approved_title_props)], alignment=WD_ALIGN_PARAGRAPH.CENTER)

    _delete_paragraph_range(paragraphs, prepared_label_index, approved_title_index)

    _record_signatory_placeholder(
        summary, "{{prepared_by_name}}",
        prepared_name_parts[0] if prepared_name_parts else "",
        f"{scope}:paragraph:{prepared_name_index}",
        "tabbed_signatory:prepared_by:name",
    )
    _record_signatory_placeholder(
        summary, "{{prepared_by_position}}",
        prepared_title_parts[0] if prepared_title_parts else "",
        f"{scope}:paragraph:{prepared_title_index}",
        "tabbed_signatory:prepared_by:position",
    )
    _record_signatory_placeholder(
        summary, "{{reviewed_by_name}}",
        reviewed_names[0],
        f"{scope}:paragraph:{reviewed_name_index}",
        "tabbed_signatory:reviewed_by:name",
    )
    _record_signatory_placeholder(
        summary, "{{endorsed_by_name}}",
        reviewed_names[1],
        f"{scope}:paragraph:{reviewed_name_index}",
        "tabbed_signatory:endorsed_by:name",
    )
    _record_signatory_placeholder(
        summary, "{{reviewed_by_position}}",
        reviewed_titles[0],
        f"{scope}:paragraph:{reviewed_title_index}",
        "tabbed_signatory:reviewed_by:position",
    )
    _record_signatory_placeholder(
        summary, "{{endorsed_by_position}}",
        reviewed_titles[1],
        f"{scope}:paragraph:{reviewed_title_index}",
        "tabbed_signatory:endorsed_by:position",
    )
    _record_signatory_placeholder(
        summary, "{{approved_by_name}}",
        approved_name_parts[0] if approved_name_parts else approved_name.text,
        f"{scope}:paragraph:{approved_name_index}",
        "tabbed_signatory:approved_by:name",
    )
    _record_signatory_placeholder(
        summary, "{{approved_by_position}}",
        approved_title_parts[0] if approved_title_parts else approved_title.text,
        f"{scope}:paragraph:{approved_title_index}",
        "tabbed_signatory:approved_by:position",
    )
    return True


def _process_signatory_blocks(paragraphs, scope, summary):
    if _process_tabbed_multicolumn_signatory_block(paragraphs, scope, summary):
        return

    nonempty = [
        (index, paragraph, _normalize_text(paragraph.text))
        for index, paragraph in enumerate(paragraphs)
        if _normalize_text(paragraph.text)
    ]

    def _extract_parts(text):
        return _signatory_text_parts(text)

    sorted_labels = sorted(SIGNATORY_BLOCK_MAP.keys(), key=len, reverse=True)

    for pos, (index, label_paragraph, label_text) in enumerate(nonempty):
        text_lower = label_text.lower()
        
        mask = [False] * len(text_lower)
        found = []
        for label in sorted_labels:
            key = SIGNATORY_BLOCK_MAP[label]
            start = 0
            while True:
                idx = text_lower.find(label, start)
                if idx == -1:
                    break
                if not any(mask[idx:idx+len(label)]):
                    found.append((idx, label, key))
                    for i in range(idx, idx+len(label)):
                        mask[i] = True
                start = idx + len(label)
                
        if not found:
            continue
            
        found.sort()
        keys = [k for _, _, k in found]
        
        name_text_raw = ""
        name_paragraph = None
        name_index = -1
        if pos + 1 < len(nonempty):
            name_index, name_paragraph, _ = nonempty[pos + 1]
            name_text_raw = name_paragraph.text
            
        title_text_raw = ""
        title_paragraph = None
        title_index = -1
        if pos + 2 < len(nonempty):
            title_index, title_paragraph, _ = nonempty[pos + 2]
            title_text_raw = title_paragraph.text
            
        names = _extract_parts(name_text_raw)
        titles = _extract_parts(title_text_raw)
        
        # Strip digital signatures/images from the blocks
        _clear_paragraph_images(label_paragraph)
        if name_paragraph:
            _clear_paragraph_images(name_paragraph)
        if title_paragraph:
            _clear_paragraph_images(title_paragraph)
        
        for i, key in enumerate(keys):
            if i < len(names):
                name_val = names[i]
                if name_val and not PLACEHOLDER_PATTERN.match(name_val):
                    ph = "{{" + f"{key}_name" + "}}"
                    if _replace_text_in_paragraph_runs(name_paragraph, name_val, ph):
                        _record_placeholder(summary, ph, name_val, f"{scope}:paragraph:{name_index}", reason=f"signatory_block:{key}:name")
            
            if i < len(titles):
                title_val = titles[i]
                if title_val and not PLACEHOLDER_PATTERN.match(title_val):
                    ph = "{{" + f"{key}_position" + "}}"
                    if _replace_text_in_paragraph_runs(title_paragraph, title_val, ph):
                        _record_placeholder(summary, ph, title_val, f"{scope}:paragraph:{title_index}", reason=f"signatory_block:{key}:position")


def _process_reference_sections(paragraphs, scope, summary):
    i = 0
    total = len(paragraphs)
    while i < total:
        heading = _normalize_text(paragraphs[i].text).lower().rstrip(":")
        placeholder_name = REFERENCE_BLOCK_MAP.get(heading)
        if not placeholder_name:
            i += 1
            continue

        content_indexes = []
        content_texts = []
        j = i + 1
        while j < total:
            text = _normalize_text(paragraphs[j].text)
            lowered = text.lower().rstrip(":")
            if lowered in REFERENCE_BLOCK_MAP:
                break
            if text:
                content_indexes.append(j)
                content_texts.append(text)
            j += 1

        if content_indexes:
            placeholder = "{{" + placeholder_name + "}}"
            first_index = content_indexes[0]
            if _replace_paragraph_text_safely(paragraphs[first_index], placeholder):
                for extra_index in content_indexes[1:]:
                    _replace_paragraph_text_safely(paragraphs[extra_index], "")
                _record_placeholder(
                    summary,
                    placeholder,
                    "\n".join(content_texts)[:200],
                    f"{scope}:paragraph:{first_index}",
                    reason=f"reference_block:{heading}",
                )
        i = j


def _placeholder_name_from_label(label):
    label = _normalize_text(label).lower().strip(" :")
    if not label or label in GENERIC_LABEL_BLACKLIST:
        return None
    if any(keyword in label for keyword in GENERIC_LABEL_BLACKLIST_KEYWORDS):
        return None
    canon = COURSE_INFO_MAP.get(label)
    if canon:
        return canon
    if len(label.split()) > 6:
        return None
    words = re.findall(r"[a-z0-9]+", label)
    if not words:
        return None
    if len(words) == 1 and re.fullmatch(r"[a-z]+\d+", words[0]):
        return None
    if words[0] in {"pqf", "aqrf", "plo", "clo", "sdg", "goal"}:
        return None
    return "_".join(words)


def _replace_paragraph_text_safely(paragraph, new_text, clear_style=False):
    _clear_paragraph_list_formatting(paragraph)
    if clear_style:
        p_pr = paragraph._p.get_or_add_pPr()
        for child in list(p_pr):
            if child.tag.endswith('}pStyle') or child.tag.endswith('}ind') or child.tag.endswith('}rPr'):
                p_pr.remove(child)
    # Capture the exact run formatting from the first existing run
    # so the placeholder / replacement text inherits font, size, bold, etc.
    from app.services.template_writer import _clone_run_props, _apply_run_props
    donor_rPr = None
    if paragraph.runs:
        donor_rPr = _clone_run_props(paragraph.runs[0])
    _clear_paragraph_content(paragraph)
    if new_text:
        run = paragraph.add_run(new_text)
        if donor_rPr is not None:
            _apply_run_props(run, donor_rPr)
    return True


def _replace_cell_text_safely(cell, new_text, clear_style=False):
    paragraphs = list(cell.paragraphs) if clear_style else [
        paragraph for paragraph in cell.paragraphs if _normalize_text(paragraph.text)
    ]
    if not paragraphs:
        if cell.paragraphs:
            return _replace_paragraph_text_safely(cell.paragraphs[0], new_text, clear_style=clear_style)
        paragraph = cell.add_paragraph()
        return _replace_paragraph_text_safely(paragraph, new_text, clear_style=clear_style)
    if len(paragraphs) > 1:
        # Concatenate all paragraphs into one for replacement
        # (common in weekly plan cells with multiple domains: Cognitive / Affective / Psychomotor)
        _replace_paragraph_text_safely(paragraphs[0], new_text, clear_style=clear_style)
        for p in paragraphs[1:]:
            _replace_paragraph_text_safely(p, "", clear_style=clear_style)
        return True
    return _replace_paragraph_text_safely(paragraphs[0], new_text, clear_style=clear_style)


def _clear_paragraph_list_formatting(paragraph):
    p_pr = paragraph._p.get_or_add_pPr()
    num_pr = getattr(p_pr, "numPr", None)
    if num_pr is not None:
        p_pr.remove(num_pr)


def _clear_paragraph_images(paragraph):
    if not paragraph:
        return
    for run in paragraph.runs:
        r_element = run._r
        for child in list(r_element):
            if "drawing" in child.tag or "pict" in child.tag:
                r_element.remove(child)


def _clear_paragraph_content(paragraph):
    p_element = paragraph._p
    for child in list(p_element):
        if child.tag.endswith("}pPr"):
            continue
        p_element.remove(child)


def _record_placeholder(summary, placeholder, original_text, location, reason):
    summary["segments_replaced"] += 1
    summary["placeholders"].append(
        {
            "placeholder": placeholder,
            "original_text": original_text[:200],
            "location": location,
            "reason": reason,
        }
    )


def _warn(summary, message):
    summary["warnings"].append(message)


def _normalize_text(value):
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _exam_row_key(row):
    nonempty = [
        _normalize_text(_cell_text(cell)).lower().strip()
        for cell in row.cells
        if _normalize_text(_cell_text(cell))
    ]
    if not nonempty:
        return None
    for text in nonempty:
        exam_key = EXAM_LABEL_MAP.get(text)
        if exam_key:
            return exam_key
    return None


def _split_week_pair_key(table, row_index):
    """Return (pair_key, next_row) only when the next row is a content-continuation
    row (its first cell has no week label of its own).  When both rows carry
    independent "WEEK N" labels they must remain as separate placeholder rows."""
    if row_index + 1 >= len(table.rows):
        return None, None
    current_number = _week_number(_cell_text(table.rows[row_index].cells[0]))
    if not current_number:
        return None, None
    pair = SPLIT_WEEK_GROUPS.get(current_number)
    if not pair:
        return None, None
    # If the current label is already a compound key (e.g. "Week 8 & 9"),
    # it covers both weeks in one row — no continuation row needed.
    current_week_key = _week_label_to_key(_cell_text(table.rows[row_index].cells[0]))
    if current_week_key and "_" in current_week_key:
        return None, None
    # Merge paired week rows when the next row is either a content continuation
    # or the expected second week in a split pair.
    next_number = _week_number(_cell_text(table.rows[row_index + 1].cells[0]))
    expected_next, pair_key = pair
    if next_number is not None and next_number != expected_next:
        return None, None
    return pair_key, table.rows[row_index + 1]


def _combine_week_pair_cell_text(left_cell, right_cell=None):
    values = []
    for cell in (left_cell, right_cell):
        if cell is None:
            continue
        text = _normalize_text(_cell_text(cell))
        if text and not PLACEHOLDER_PATTERN.match(text) and text not in values:
            values.append(text)
    return "\n".join(values)


def _week_number(label):
    match = re.match(r"week\s*(\d+)", _normalize_text(label).lower())
    if not match:
        return None
    return match.group(1)


def _cell_text(cell):
    texts = [_normalize_text(paragraph.text) for paragraph in cell.paragraphs]
    return " ".join(part for part in texts if part)


def summarize_placeholder_summary(summary):
    if not summary:
        return {}
    return {
        "placeholder_count": summary.get("placeholder_count", 0),
        "algorithm_version": summary.get("algorithm_version", ""),
        "segments_replaced": summary.get("segments_replaced", 0),
        "segments_scanned": summary.get("segments_scanned", 0),
        "segments_skipped": summary.get("segments_skipped", 0),
        "used_ai": summary.get("used_ai", False),
        "ai_requested": summary.get("ai_requested", False),
        "ai_suggestions_applied": summary.get("ai_suggestions_applied", 0),
        "ai_candidate_count": summary.get("ai_candidate_count", 0),
        "placeholder_counts": summary.get("placeholder_counts", {}),
        "warnings": summary.get("warnings", []),
        "placeholders": summary.get("placeholders", []),
        "output_template_name": summary.get("output_template_name", ""),
        "output_placeholder_count": summary.get("output_placeholder_count", 0),
    }


def summary_to_json(summary):
    return json.dumps(summarize_placeholder_summary(summary), sort_keys=True)
