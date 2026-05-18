"""Derive AI Copilot generation shape from a profiled DOCX template.

The template profile tells the renderer where to write content.  The
generation spec tells the AI Copilot how much content to create before the
renderer runs: how many CLO rows, how many weekly rows, and which alignment
groups exist in the source template.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional


SPEC_SCHEMA_VERSION = "1"

DEFAULT_WEEK_LABELS = [
    "Week 1",
    "Week 2",
    "Week 3",
    "Week 4",
    "Week 5",
    "Week 6",
    "Week 7",
    "Week 8 & 9",
    "Week 10 & 11",
    "Week 12",
    "Week 13",
    "Week 14 & 15",
    "Week 16-17",
    "Week 18",
]

DEFAULT_CLO_ROWS = [
    ("CLO 1", "cognitive"),
    ("CLO 2", "cognitive"),
    ("CLO 3", "cognitive"),
    ("CLO 4", "affective"),
    ("CLO 5", "affective"),
    ("CLO 6", "psychomotor"),
    ("CLO 7", "psychomotor"),
    ("CLO 8", "psychomotor"),
]

_CLO_RE = re.compile(r"\bCLO\s*0*(\d+)\b", re.IGNORECASE)
_WEEK_RE = re.compile(r"\bWEEK\s*[\dIVX]+(?:\s*(?:&|-|–|TO)\s*[\dIVX]+)?", re.IGNORECASE)
_TIMEFRAME_LABEL_RE = re.compile(
    r"\b(?:WEEK|MODULE|UNIT|SESSION|LESSON|MEETING|DAY|TOPIC|PHASE|PART)\s*[\dIVX]+(?:\s*(?:&|-|–|TO)\s*[\dIVX]+)?",
    re.IGNORECASE,
)
_EXAM_RE = re.compile(r"\b(?:PRELIM|PRELIMINARY|MIDTERM|FINAL)\b.*\bEXAM", re.IGNORECASE)
_PLO_RE = re.compile(r"\bPLO\s*0*\d+\b|\bPL0?\d+\b", re.IGNORECASE)
_PROGRAM_OUTCOME_CODE_RE = re.compile(
    r"\b(?:PLO|PO|PL|BPED|BSED[A-Z]*|BS[A-Z]{2,8}|[A-Z]{3,12})\s*0*\d+\b",
    re.IGNORECASE,
)
_BINARY_ALIGNMENT_VALUES = {"", "✓", "✔", "x", "X", "/", "\\", "1", "0", "yes", "no", "Y", "N"}
_REFERENCE_HEADING_RE = re.compile(r"^(?:REFERENCES|BIBLIOGRAPHY|WORKS\s+CITED)$", re.IGNORECASE)
_REFERENCE_STOP_RE = re.compile(r"^(?:REVISION|APPROVAL|NOTATION|APPENDIX|RUBRIC|GRADING\s+SYSTEM)\b", re.IGNORECASE)

_METADATA_ALIASES = {
    "course_code": ["course code", "course number", "course no"],
    "course_title": ["course title", "descriptive title", "title"],
    "course_description": ["course description", "description"],
    "service_learning_component": ["service-learning component", "service learning component"],
    "target_sdgs_display": ["target sdg", "target sdgs", "sdg"],
    "type_of_course": ["type of course", "course type"],
    "units_display": ["units", "unit"],
    "credit_display": ["credit", "credits"],
    "contact_hours_display": ["contact hours per week", "contact hours", "class hours", "hours per week"],
    "contact_hours_per_week": ["contact hours per week", "contact hours", "class hours", "hours per week"],
    "pre_requisite": ["pre-requisite", "prerequisite", "pre requisite"],
    "co_requisite": ["co-requisite", "corequisite", "co requisite"],
    "class_schedule": ["class schedule", "schedule"],
    "room_assignment": ["room assignment", "room"],
}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split()).strip()


def _table_rows(table) -> List[List[str]]:
    rows: List[List[str]] = []
    for row in table.rows:
        rows.append([_clean(cell.text) for cell in row.cells])
    return rows


def _norm_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _clean(value).lower()).strip()


def _table_text(rows: List[List[str]]) -> str:
    return " ".join(_clean(cell) for row in rows for cell in row if _clean(cell))


def _metadata_field_for_label(label: str) -> Optional[str]:
    normalized = _norm_label(label)
    for field, aliases in _METADATA_ALIASES.items():
        for alias in aliases:
            if normalized == _norm_label(alias) or normalized.startswith(_norm_label(alias)):
                return field
    return None


def _is_metadata_delimiter(value: str) -> bool:
    cleaned = _clean(value)
    return cleaned in {"", ":", "-", "–", "—"}


def _metadata_value_column(row: List[str]) -> int:
    """Return the likely value column for a label/value metadata row."""
    for idx in range(1, len(row)):
        if not _is_metadata_delimiter(row[idx]):
            return idx
    return 1 if len(row) > 1 else 0


def _normalize_clo_code(value: str, fallback_index: int) -> str:
    match = _CLO_RE.search(value or "")
    if match:
        return f"CLO {int(match.group(1))}"
    return f"CLO {fallback_index}"


def normalize_alignment_row_label(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", _clean(value).upper())


def normalize_alignment_column_label(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", _clean(value).upper())


def normalize_duplicate_alignment_columns(labels: List[str]) -> List[str]:
    normalized = [normalize_alignment_column_label(label) for label in labels]
    totals: Dict[str, int] = {}
    for label in normalized:
        if label:
            totals[label] = totals.get(label, 0) + 1
    seen: Dict[str, int] = {}
    output = []
    for label in normalized:
        if not label:
            output.append("")
        elif totals.get(label, 0) <= 1:
            output.append(label)
        else:
            seen[label] = seen.get(label, 0) + 1
            output.append(f"{label}{seen[label]}")
    return output


def _is_code_header(value: str) -> bool:
    return _norm_label(value) in {
        "code", "outcome code", "outcomes code", "program outcome code",
        "po code", "plo code", "no", "number", "item",
    }


def _is_description_header(value: str) -> bool:
    normalized = _norm_label(value)
    return (
        normalized in {"description", "outcome", "outcomes", "program outcome", "program outcomes", "learning outcomes"}
        or ("outcome" in normalized and "code" not in normalized)
        or "description" in normalized
    )


def _is_short_alignment_header(value: str) -> bool:
    cleaned = normalize_alignment_column_label(value)
    return bool(cleaned) and 1 <= len(cleaned) <= 6 and not cleaned.isdigit()


def _is_binary_alignment_value(value: str) -> bool:
    cleaned = _clean(value)
    return cleaned in _BINARY_ALIGNMENT_VALUES or cleaned.lower() in {"true", "false", "checked", "unchecked"}


def detect_code_column(header: List[str]) -> Optional[int]:
    for idx, cell in enumerate(header):
        if _is_code_header(cell):
            return idx
    return None


def detect_description_column(header: List[str]) -> Optional[int]:
    for idx, cell in enumerate(header):
        if _is_description_header(cell):
            return idx
    return None


def detect_alignment_value_columns(header: List[str], code_col: int, description_col: int) -> List[int]:
    return [
        idx for idx, cell in enumerate(header)
        if idx not in {code_col, description_col} and _is_short_alignment_header(cell)
    ]


def detect_header_row(rows: List[List[str]]) -> Optional[int]:
    for idx, row in enumerate(rows[:8]):
        code_col = detect_code_column(row)
        desc_col = detect_description_column(row)
        if code_col is None or desc_col is None or code_col == desc_col:
            continue
        if len(detect_alignment_value_columns(row, code_col, desc_col)) >= 3:
            return idx
    return None


def build_required_alignment_keys(row_labels: List[str], column_labels: List[str]) -> List[str]:
    return [
        f"{row_label}_{column_label}"
        for row_label in row_labels
        for column_label in column_labels
        if row_label and column_label
    ]


def detect_program_institutional_checkmark_table(rows: List[List[str]], table_index: int, alignment_number: int) -> Optional[Dict[str, Any]]:
    header_idx = detect_header_row(rows)
    if header_idx is None:
        return None
    header = rows[header_idx]
    code_col = detect_code_column(header)
    desc_col = detect_description_column(header)
    if code_col is None or desc_col is None:
        return None
    alignment_cols = detect_alignment_value_columns(header, code_col, desc_col)
    if len(alignment_cols) < 3:
        return None

    row_labels_original: List[str] = []
    row_labels_normalized: List[str] = []
    first_data_row = None
    last_data_row = None
    binary_cells = 0
    total_alignment_cells = 0
    for row_idx, row in enumerate(rows[header_idx + 1:], start=header_idx + 1):
        code = _clean(row[code_col]) if code_col < len(row) else ""
        description = _clean(row[desc_col]) if desc_col < len(row) else ""
        normalized_code = normalize_alignment_row_label(code)
        if not normalized_code or not re.search(r"\d", normalized_code):
            continue
        if not description and not any(_clean(row[col]) if col < len(row) else "" for col in alignment_cols):
            continue
        first_data_row = row_idx if first_data_row is None else first_data_row
        last_data_row = row_idx
        row_labels_original.append(code)
        row_labels_normalized.append(normalized_code)
        for col in alignment_cols:
            value = _clean(row[col]) if col < len(row) else ""
            total_alignment_cells += 1
            if _is_binary_alignment_value(value):
                binary_cells += 1
    if len(row_labels_original) < 2:
        return None
    if total_alignment_cells and binary_cells / total_alignment_cells < 0.75:
        return None

    column_labels_original = [_clean(header[col]) for col in alignment_cols]
    column_labels_normalized = normalize_duplicate_alignment_columns(column_labels_original)
    return {
        "id": f"program_institutional_alignment_{alignment_number}",
        "alignment_format": "program_institutional_checkmark",
        "value_style": "checkmark",
        "row_labels_original": row_labels_original,
        "row_labels_normalized": row_labels_normalized,
        "column_labels_original": column_labels_original,
        "column_labels_normalized": column_labels_normalized,
        "required_alignment_keys": build_required_alignment_keys(row_labels_normalized, column_labels_normalized),
        "code_column": code_col,
        "description_column": desc_col,
        "alignment_column_indices": alignment_cols,
        "column_index_by_normalized": {
            normalized: col
            for normalized, col in zip(column_labels_normalized, alignment_cols)
            if normalized
        },
        "locator": {
            "type": "table_region",
            "table_index": table_index,
            "header_row": header_idx,
            "start_row": first_data_row,
            "end_row": (last_data_row + 1) if last_data_row is not None else header_idx + 1,
            "writable_rows": list(range(first_data_row, last_data_row + 1)) if first_data_row is not None and last_data_row is not None else [],
        },
    }


def _domain_from_row(row: Iterable[str]) -> Optional[str]:
    joined = " ".join(_clean(cell).upper() for cell in row if _clean(cell))
    if not joined:
        return None
    if "COGNITIVE" in joined:
        return "cognitive"
    if "AFFECTIVE" in joined:
        return "affective"
    if "PSYCHOMOTOR" in joined:
        return "psychomotor"
    return None


def _is_repeated_label_row(row: List[str]) -> bool:
    values = [_clean(cell).upper() for cell in row if _clean(cell)]
    return bool(values) and len(set(values)) == 1


def _find_clo_header(rows: List[List[str]]) -> Optional[int]:
    for idx, row in enumerate(rows[:6]):
        joined = " ".join(row).upper()
        if "COURSE LEARNING OUTCOMES" in joined and (
            "ALIGNED" in joined or "PROGRAM" in joined or "SDG" in joined
        ):
            return idx
        if "COURSE OUTCOMES" in joined:
            # Check for program outcome codes in adjacent columns (alignment-style)
            if any(_PROGRAM_OUTCOME_CODE_RE.search(cell) for cell in row[1:]):
                return idx
            # Also accept standalone "COURSE OUTCOMES" header when data rows follow
            # (e.g. LP EDUC 010 style: header followed by domain-grouped CLOs)
            if _clean(row[0]).upper() == "COURSE OUTCOMES":
                return idx
    return None


def _alignment_columns(header: List[str]) -> Dict[str, Dict[str, Any]]:
    first_header = _clean(header[0]).upper() if header else ""
    if first_header == "COURSE OUTCOMES":
        columns: Dict[str, Dict[str, Any]] = {
            "clo_statement": {"label": header[0] or "Course Outcomes", "col_index": 0},
        }
    else:
        columns = {
            "clo_code": {"label": "CLO Code", "col_index": 0},
            "clo_statement": {"label": "Course Learning Outcomes", "col_index": 1},
        }
    for idx, cell in enumerate(header):
        upper = cell.upper()
        if "ALIGNED" in upper and "PROGRAM" in upper:
            columns["aligned_plos"] = {"label": cell or "Aligned Program Learning Outcomes", "col_index": idx}
        elif "GRADUATE" in upper and "ATTRIBUTE" in upper:
            columns["graduate_attributes"] = {"label": cell or "Graduate Attributes", "col_index": idx}
        elif "CORE" in upper and ("VALUE" in upper or "CALUES" in upper):
            columns["core_values"] = {"label": cell or "Core Values", "col_index": idx}
        elif "PQF" in upper:
            columns["pqf_level_6_alignment"] = {"label": cell or "PQF Level 6 Alignment", "col_index": idx}
        elif "AQRF" in upper:
            columns["aqrf_level_6_alignment"] = {"label": cell or "AQRF Level 6 Alignment", "col_index": idx}
        elif "SDG" in upper:
            columns["relevant_sdgs"] = {"label": cell or "Relevant SDG", "col_index": idx}
        elif _PROGRAM_OUTCOME_CODE_RE.fullmatch(_clean(cell)):
            key = f"program_outcome_{re.sub(r'[^a-z0-9]+', '_', _clean(cell).lower()).strip('_')}"
            columns[key] = {"label": cell, "col_index": idx}

    # Fill gaps in checkmark matrices (if we have at least 2 POs, treat everything between them as POs)
    po_indices = [v["col_index"] for v in columns.values() if v.get("col_index") is not None and v.get("col_index") > 0 and v["col_index"] < len(header)]
    # Only PO keys start with program_outcome_
    po_keys = [k for k, v in columns.items() if k.startswith("program_outcome_")]
    if len(po_keys) >= 2:
        po_min = min(columns[k]["col_index"] for k in po_keys)
        po_max = max(columns[k]["col_index"] for k in po_keys)
        for idx in range(po_min, po_max + 1):
            if idx == 1 and columns.get("clo_statement") and columns["clo_statement"]["col_index"] == 1:
                continue # Keep statement column if explicitly placed at 1
            # Check if this index is already mapped
            already_mapped = any(v["col_index"] == idx for v in columns.values())
            if not already_mapped:
                cell = header[idx]
                key = f"program_outcome_col_{idx}"
                columns[key] = {"label": cell or f"PO {idx}", "col_index": idx}
    return columns


_CLO_BASED_COLUMN_KEYS = frozenset({
    "aligned_plos", "graduate_attributes", "core_values",
    "pqf_level_6_alignment", "aqrf_level_6_alignment", "relevant_sdgs",
})


def detect_alignment_style(columns: Dict[str, Dict[str, Any]]) -> str:
    """Classify CLO table alignment as 'checkmark' or 'clo_based'.

    * **checkmark** — Column headers are PO codes (e.g. BPED 1, BSEDENG 2).
      Each intersection cell receives a check mark (✓) or remains blank.
    * **clo_based** — Column headers are text labels (Aligned PLOs, Graduate
      Attributes, Core Values, PQF, AQRF, SDG) with free-text alignment per CLO.
    """
    non_identity_keys = {
        k for k in columns
        if k not in {"clo_code", "clo_statement"}
    }
    if not non_identity_keys:
        return "clo_based"
    po_keys = {k for k in non_identity_keys if k.startswith("program_outcome_")}
    text_keys = non_identity_keys & _CLO_BASED_COLUMN_KEYS
    if po_keys and not text_keys:
        return "checkmark"
    return "clo_based"


def _program_scopes(template_context: Dict[str, Any]) -> List[Dict[str, Any]]:
    scopes = []
    for index, group in enumerate((template_context or {}).get("program_outcomes") or [], start=1):
        if not isinstance(group, dict):
            continue
        items = group.get("items") if isinstance(group.get("items"), list) else []
        program_name = str(group.get("program_name") or group.get("program_code") or "").strip()
        if len(program_name) > 80 or re.match(r"^(apply|analyze|resolve|conduct|employ|demonstrate|use)\b", program_name, re.IGNORECASE):
            program_name = f"Program {index}"
        scopes.append({
            "program_code": group.get("program_code") or f"program_{index}",
            "program_name": program_name or f"Program {index}",
            "items": items,
        })
    return scopes


def _code_from_text(value: str) -> str:
    match = _PROGRAM_OUTCOME_CODE_RE.search(value or "")
    if not match:
        return ""
    raw = re.sub(r"\s+", "", match.group(0).upper())
    if re.match(r"^(?:CLO|SDG|PQF|AQRF|CV)\d+", raw):
        return ""
    if raw.startswith("PL") and not raw.startswith("PLO"):
        raw = raw.replace("PL", "PLO", 1)
    return raw


def _infer_program_name_from_outcomes(rows: List[List[str]], fallback_index: int) -> str:
    text = _table_text(rows).lower()
    signals = [
        ("Nursing", ("nursing", "health sciences", "delivery of care", "nursing theories")),
        ("Accountancy", ("accounting", "taxation", "audit", "financial accounting", "accounting information")),
        ("Marketing", ("marketing", "consumer", "market", "sales", "organizational contexts")),
        ("Architecture", ("architecture", "architectural", "built environment", "design and construction")),
        ("Information Technology", ("computing", "information technology", "software", "program correctness", "computer")),
        ("Business Administration", ("business", "management", "entrepreneur", "strategic perspective")),
    ]
    for label, keywords in signals:
        if any(keyword in text for keyword in keywords):
            return label
    code_match = _PROGRAM_OUTCOME_CODE_RE.search(_table_text(rows))
    if code_match:
        code = re.sub(r"\s*0*\d+\b", "", code_match.group(0).upper()).strip()
        code_names = {
            "BPED": "Physical Education",
            "BSEDENG": "English Education",
        }
        if code in code_names:
            return code_names[code]
        if code and code not in {"CLO", "SDG", "PQF", "AQRF", "CV"}:
            return code
    for row in rows[:3]:
        joined = " ".join(row)
        if re.search(r"BACHELOR|SCIENCE|NURSING|ACCOUNT|MARKETING|ARCHITECTURE|TECHNOLOGY|PROGRAM OUTCOMES?", joined, re.IGNORECASE):
            cleaned = re.sub(r"\bCODE\b|\bPROGRAM OUTCOMES?\b", "", joined, flags=re.IGNORECASE)
            cleaned = _clean(cleaned)
            if (
                cleaned
                and len(cleaned) <= 80
                and not _PLO_RE.search(cleaned)
                and not re.fullmatch(r"(?:[A-Z]\s*){2,12}", cleaned)
            ):
                return cleaned
    return f"Program {fallback_index}"


def _infer_program_code_from_outcomes(rows: List[List[str]], program_name: str, fallback_index: int) -> str:
    text = _table_text(rows)
    match = _PROGRAM_OUTCOME_CODE_RE.search(text)
    if match:
        code = re.sub(r"\s*0*\d+\b", "", match.group(0).upper()).strip()
        if code and code not in {"PLO", "PO", "PL", "CLO", "SDG", "PQF", "AQRF", "CV"}:
            return code
    match = re.search(r"\bBS[A-Z]{2,6}\b|\b[A-Z]{2,8}\b(?=\s+PROGRAM OUTCOMES?)", text, re.IGNORECASE)
    if match and match.group(0).upper() not in {"CODE", "PROGRAM"}:
        return match.group(0).upper()
    aliases = {
        "Nursing": "NURSING",
        "Accountancy": "ACCOUNTANCY",
        "Marketing": "MARKETING",
        "Architecture": "ARCHITECTURE",
        "Information Technology": "BSIT",
        "Business Administration": "BUSINESS",
    }
    return aliases.get(program_name, f"PROGRAM_{fallback_index}")


def _extract_program_items(rows: List[List[str]]) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    seen = set()
    for row in rows:
        cells = [_clean(cell) for cell in row if _clean(cell)]
        if not cells:
            continue
        joined = " ".join(cells)
        code = _code_from_text(joined)
        if not code or code in seen:
            continue
        parts = []
        for cell in cells:
            cleaned = _PROGRAM_OUTCOME_CODE_RE.sub("", cell).strip(" :-–")
            if cleaned and not re.fullmatch(r"CODE|PROGRAM OUTCOMES?", cleaned, re.IGNORECASE):
                parts.append(cleaned)
        description = _clean(" ".join(parts))
        items.append({"code": code, "description": description})
        seen.add(code)
    return items


def _detect_program_scope(rows: List[List[str]], table_index: int, fallback_index: int) -> Optional[Dict[str, Any]]:
    text = _table_text(rows)
    if not (_PROGRAM_OUTCOME_CODE_RE.search(text) and re.search(r"OUTCOMES?|PROGRAM", text, re.IGNORECASE)):
        return None
    items = _extract_program_items(rows)
    if len(items) < 2:
        return None
    program_name = _infer_program_name_from_outcomes(rows, fallback_index)
    return {
        "program_code": _infer_program_code_from_outcomes(rows, program_name, fallback_index),
        "program_name": program_name,
        "items": items,
        "source_table_index": table_index,
        "scope_source": "program_outcome_table",
    }


def _extract_metadata_values(rows: List[List[str]]) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for row in rows:
        if len(row) < 2:
            continue
        field = _metadata_field_for_label(row[0])
        if field and field not in values:
            values[field] = _clean(row[_metadata_value_column(row)])
    return values


def _scope_with_course_context(scope: Optional[Dict[str, Any]], course_context: Dict[str, str]) -> Optional[Dict[str, Any]]:
    if not isinstance(scope, dict):
        return scope
    enriched = dict(scope)
    if course_context:
        enriched["course_context"] = {
            key: value
            for key, value in {
                "course_code": course_context.get("course_code"),
                "course_title": course_context.get("course_title"),
                "course_description": course_context.get("course_description"),
                "department": course_context.get("department"),
            }.items()
            if value
        }
    return enriched


def _detect_metadata_fields(rows: List[List[str]], table_index: int) -> List[Dict[str, Any]]:
    fields = []
    seen = set()
    for row_idx, row in enumerate(rows):
        if len(row) < 2:
            continue
        field = _metadata_field_for_label(row[0])
        if not field or field in seen:
            continue
        seen.add(field)
        value_col = _metadata_value_column(row)
        fields.append({
            "field": field,
            "label": row[0],
            "aliases": _METADATA_ALIASES.get(field, []),
            "detected_value": _clean(row[value_col]) if len(row) > value_col else "",
            "locator": {
                "type": "table_cell",
                "table_index": table_index,
                "row": row_idx,
                "col": value_col,
            },
        })
    return fields


def _collect_all_paragraphs(doc) -> List[Any]:
    """Recursively collect all paragraphs from body, table cells, and nested table cells."""
    paras: List[Any] = []
    for para in (getattr(doc, "paragraphs", []) or []):
        paras.append(para)
    _collect_table_paragraphs(doc, paras)
    return paras


def _collect_table_paragraphs(table_or_doc, paras: List[Any]):
    tables = getattr(table_or_doc, "tables", []) or ([] if not hasattr(table_or_doc, "rows") else [])
    for table in tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    if para.text.strip():
                        paras.append(para)
                # Recurse into nested tables within cells
                for nested in cell.tables:
                    _collect_table_paragraphs(nested, paras)


def _detect_paragraph_metadata_fields(doc) -> List[Dict[str, Any]]:
    """Scan body paragraphs AND (nested) table cell paragraphs for label: value metadata."""
    fields: List[Dict[str, Any]] = []
    seen = set()
    all_paras = _collect_all_paragraphs(doc)

    for idx, paragraph in enumerate(all_paras[:200]):
        text = _clean(getattr(paragraph, "text", ""))
        if not text or ":" not in text:
            continue
        label = _clean(text.split(":", 1)[0])
        field = _metadata_field_for_label(label)
        if not field:
            continue
        key = (field, _norm_label(label), idx)
        if key in seen:
            continue
        seen.add(key)
        fields.append({
            "field": field,
            "label": label,
            "aliases": _METADATA_ALIASES.get(field, []),
            "detected_value": _clean(text.split(":", 1)[1]),
            "locator": {
                "type": "paragraph_prefix",
                "prefix": label,
                "search_range": [max(0, idx - 2), min(len(all_paras) - 1, idx + 2)],
            },
        })
    return fields


def _detect_clo_group(rows: List[List[str]], table_index: int, scope: Optional[Dict[str, Any]], group_number: int) -> Optional[Dict[str, Any]]:
    header_idx = _find_clo_header(rows)
    if header_idx is None:
        return None
    header = rows[header_idx]
    row_specs = []
    skipped_rows = []
    current_domain = "cognitive"
    first_data_row = None
    last_data_row = None

    for row_idx, row in enumerate(rows[header_idx + 1:], start=header_idx + 1):
        domain = _domain_from_row(row)
        if domain and _is_repeated_label_row(row):
            current_domain = domain
            skipped_rows.append({"row": row_idx, "reason": "domain_header", "label": domain})
            continue
        first_cell = _clean(row[0]) if row else ""
        is_clo_row = bool(_CLO_RE.search(first_cell)) or (
            _clean(header[0]).upper() == "COURSE OUTCOMES"
            and first_cell
            and not _PROGRAM_OUTCOME_CODE_RE.fullmatch(first_cell)
        )
        if not is_clo_row and first_data_row is not None and not first_cell:
            # In checkmark matrices, allow empty first cell if row has alignment data
            if any(_clean(cell) for cell in row[1:]):
                is_clo_row = True

        if not is_clo_row:
            if any(_clean(cell) for cell in row):
                skipped_rows.append({"row": row_idx, "reason": "non_clo_row", "label": first_cell})
            continue
        first_data_row = row_idx if first_data_row is None else first_data_row
        last_data_row = row_idx
        row_number = len(row_specs) + 1
        row_specs.append({
            "index": row_number,
            "code": _normalize_clo_code(first_cell, row_number),
            "label": first_cell or f"CLO {row_number}",
            "domain": current_domain,
            "source_row": row_idx,
            "source_statement": first_cell if _clean(header[0]).upper() == "COURSE OUTCOMES" else (_clean(row[1]) if len(row) > 1 else ""),
            "source_alignment_preview": [_clean(cell) for cell in row[2:8]],
        })

    if not row_specs:
        return None

    columns = _alignment_columns(header)
    alignment_style = detect_alignment_style(columns)

    # For checkmark groups, extract the ordered list of PO column codes.
    checkmark_po_codes: List[str] = []
    if alignment_style == "checkmark":
        checkmark_po_codes = [
            col_def.get("label", "")
            for key, col_def in sorted(columns.items(), key=lambda x: x[1].get("col_index", 0))
            if key.startswith("program_outcome_")
        ]

    label_scope = (scope or {}).get("program_name") or (scope or {}).get("program_code")
    label = f"CLO Alignment - {label_scope}" if label_scope else f"CLO Alignment {group_number}"
    locator = {
        "type": "table_region",
        "table_index": table_index,
        "start_row": first_data_row,
        "end_row": last_data_row + 1,
        "writable_rows": [row["source_row"] for row in row_specs],
    }
    group: Dict[str, Any] = {
        "id": f"clo_group_{group_number}",
        "label": label,
        "program_scope": scope or {},
        "locator": locator,
        "row_count": len(row_specs),
        "rows": row_specs,
        "columns": columns,
        "alignment_style": alignment_style,
        "skipped_rows": skipped_rows,
    }
    if checkmark_po_codes:
        group["checkmark_po_codes"] = checkmark_po_codes
    return group


def _find_weekly_header(rows: List[List[str]]) -> Optional[int]:
    for idx, row in enumerate(rows):
        joined = " ".join(row).upper()
        if (
            ("TIME" in joined and "FRAME" in joined)
            or "SCHEDULE" in joined
        ) and (
            "TOPICS" in joined
            or "TOPIC OUTLINE" in joined
            or "INTENDED LEARNING" in joined
            or "LEARNING OUTCOME" in joined
        ):
            return idx
    return None


def _weekly_columns(header: List[str]) -> Dict[str, Dict[str, Any]]:
    mapping = {}
    for idx, cell in enumerate(header):
        upper = cell.upper()
        if ("TIME" in upper and "FRAME" in upper) or "SCHEDULE" in upper:
            mapping["time_frame_label"] = {"label": cell or "Time Frame", "col_index": idx}
        elif "INTENDED" in upper or "LEARNING OUTCOME" in upper:
            mapping["intended_learning_outcomes"] = {"label": cell or "Intended Learning Outcomes", "col_index": idx}
        elif "TOPIC" in upper:
            mapping["topics"] = {"label": cell or "Topics", "col_index": idx}
        elif "TEACHING" in upper or "METHODOLOGY" in upper or "TLA" in upper:
            mapping["teaching_learning_activities"] = {"label": cell or "Teaching-Learning Activities", "col_index": idx}
        elif "ASSESS" in upper:
            mapping["assessment"] = {"label": cell or "Assessment", "col_index": idx}
        elif "RESOURCE" in upper:
            mapping["learning_resources"] = {"label": cell or "Learning Resources", "col_index": idx}
    return mapping or {
        "time_frame_label": {"label": "Time Frame", "col_index": 0},
        "intended_learning_outcomes": {"label": "Intended Learning Outcomes", "col_index": 1},
        "topics": {"label": "Topics", "col_index": 2},
        "teaching_learning_activities": {"label": "Teaching-Learning Activities", "col_index": 3},
        "assessment": {"label": "Assessment", "col_index": 4},
        "learning_resources": {"label": "Learning Resources", "col_index": 5},
    }


def _docx_cell_lines(cell) -> List[str]:
    lines = []
    for paragraph in getattr(cell, "paragraphs", []) or []:
        raw = getattr(paragraph, "text", "")
        if raw:
            # Split by newlines BEFORE cleaning, so multi-line content in
            # a single paragraph is detected as separate lines for format
            # detection (numbered items, hierarchy, item count, etc.).
            for segment in raw.split("\n"):
                cleaned = _clean(segment)
                if cleaned:
                    lines.append(cleaned)
    if not lines:
        raw = getattr(cell, "text", "")
        if raw:
            for segment in raw.split("\n"):
                cleaned = _clean(segment)
                if cleaned:
                    lines.append(cleaned)
    return lines


def _docx_row_segments(row) -> List[Any]:
    segments = []
    seen = set()
    for cell in getattr(row, "cells", []) or []:
        marker = id(cell._tc)
        if marker in seen:
            continue
        seen.add(marker)
        segments.append(cell)
    return segments


def _weekly_format_hints(table, weekly_outline: Dict[str, Any]) -> Dict[str, Any]:
    fields = [
        "time_frame_label",
        "intended_learning_outcomes",
        "topics",
        "teaching_learning_activities",
        "assessment",
        "learning_resources",
    ]

    # Regexes for detecting list patterns inside cell paragraphs
    # All patterns are universal — they match structural markers, never
    # template-specific content.  Any weekly outline from any department
    # or institution will be detected correctly.
    _NUMBERED_ITEM_RE = re.compile(r"^\s*((?:\d+|[a-z]|[ivxlcdm]+)[\.\)]\s*)", re.IGNORECASE)
    _CATEGORY_LABEL_RE = re.compile(r"^\s*([A-Z][\w\s&/()\-]{2,50})\s*:\s*$")
    _LEAD_IN_RE = re.compile(
        r"(?:at the end of|should be able to|will be able to|students will|learners will|"
        r"upon completion|by the end of|learning objectives|intended learning outcomes|"
        r"participants will|student will)",
        re.IGNORECASE,
    )
    _COMPLETE_SENTENCE_RE = re.compile(r"[.!?]\s*$")
    _SEMICOLON_END_RE = re.compile(r";\s*$")
    _HIERARCHY_PATTERN_RE = re.compile(
        r"^\s*(?:unit|module|lesson|chapter|part|section|topic|theme|week|session|meeting)\s+\d+",
        re.IGNORECASE,
    )

    samples: Dict[str, List[Dict[str, Any]]] = {field: [] for field in fields}
    for row_spec in (weekly_outline or {}).get("rows", [])[:4]:
        source_row = row_spec.get("source_row")
        if not isinstance(source_row, int) or source_row >= len(table.rows):
            continue
        segments = _docx_row_segments(table.rows[source_row])
        for index, field in enumerate(fields):
            if index >= len(segments):
                continue
            lines = _docx_cell_lines(segments[index])
            if not lines:
                continue
            text = "\n".join(lines)
            lowered = text.lower()

            # ── category labels ──
            category_labels = []
            for line in lines:
                m = _CATEGORY_LABEL_RE.match(line.strip())
                if m:
                    category_labels.append(m.group(1).strip())

            # ── numbered / lettered items ──
            numbered_count = sum(1 for line in lines if _NUMBERED_ITEM_RE.match(line))
            uses_numbered_items = numbered_count >= 2  # need at least 2 to be a real list

            # ── dash/bullet items ──
            dash_count = sum(1 for line in lines if line.lstrip().startswith(("-", "•", "●")))
            uses_dash_bullets = dash_count >= 1

            # ── sub-bullet detection (indented lines under a parent bullet) ──
            sub_bullet_count = 0
            prev_indent = -1
            for line in lines:
                stripped = line.lstrip()
                if not stripped:
                    continue
                indent = len(line) - len(stripped)
                if prev_indent >= 0 and indent > prev_indent and (stripped.startswith(("-", "•", "●")) or _NUMBERED_ITEM_RE.match(stripped)):
                    sub_bullet_count += 1
                prev_indent = indent

            # ── item counts ──
            non_empty = [l for l in lines if l.strip()]
            item_count = max(dash_count, numbered_count, len(non_empty))

            # ── lead-in sentence (e.g. "At the end of this week,... should be able to:") ──
            lead_in = None
            if non_empty:
                first = non_empty[0]
                if _LEAD_IN_RE.search(first.lower()):
                    lead_in = first

            # ── sentence style of data items ──
            data_lines = [l for l in lines if l.strip() and (_NUMBERED_ITEM_RE.match(l) or not _NUMBERED_ITEM_RE.match(l))]
            # Use only the substantive items (skip lead-in, skip blank category labels)
            substantive = [l for l in non_empty if not _CATEGORY_LABEL_RE.match(l.strip()) and l != lead_in]
            complete_sentences = sum(1 for l in substantive if _COMPLETE_SENTENCE_RE.search(l.strip()))
            semicolon_items = sum(1 for l in substantive if _SEMICOLON_END_RE.search(l.strip()))
            
            # Determine predominant item style
            if len(substantive) >= 2:
                fragment_like = len(substantive) - complete_sentences
                if complete_sentences >= fragment_like * 0.5:
                    item_format = "complete_sentences"
                elif semicolon_items >= len(substantive) * 0.5:
                    item_format = "semicolon_list"
                else:
                    item_format = "label_fragments"
            elif substantive:
                item_format = "complete_sentences" if complete_sentences else "label_fragments"
            else:
                item_format = "inline"

            # ── hierarchical structure (Unit/Lesson/Chapter patterns) ──
            hierarchical_lines = [l for l in non_empty if _HIERARCHY_PATTERN_RE.match(l.strip())]
            hierarchy_levels = len(hierarchical_lines)

            # ── boilerplate detection: separate structural/static text from replaceable content ──
            # Lead-in sentences ("At the end of this week...") are boilerplate — preserve verbatim.
            # Structural headings (Unit 1, Lesson 2, etc.) are boilerplate — preserve verbatim.
            boilerplate_structural_headings = [
                l.strip() for l in hierarchical_lines
            ]
            # Content lines = substantive lines MINUS boilerplate headings
            content_only = [
                l for l in substantive
                if l.strip() not in {h.strip() for h in hierarchical_lines}
            ]
            content_only_count = len(content_only)

            samples[field].append({
                "paragraph_count": len(lines),
                "has_category_labels": len(category_labels) > 0,
                "category_labels": category_labels,
                "uses_dash_bullets": uses_dash_bullets,
                "uses_numbered_items": uses_numbered_items,
                "numbered_count": numbered_count,
                "dash_count": dash_count,
                "item_count": item_count,
                "has_sub_bullets": sub_bullet_count > 0,
                "lead_in": lead_in,
                "item_format": item_format,
                "has_hierarchy": hierarchy_levels >= 2,
                "hierarchy_levels": hierarchy_levels,
                "sample": text,
                "boilerplate_lead_in": lead_in,
                "boilerplate_structural_headings": boilerplate_structural_headings,
                "content_only_lines": content_only,
                "content_only_count": content_only_count,
            })

    hints = {}
    for field, field_samples in samples.items():
        if not field_samples:
            hints[field] = {
                "output_style": "flat_lines",
                "list_marker": "plain",
                "typical_paragraph_count": 0,
                "samples": [],
                "typical_item_count": 0,
                "min_item_count": 0,
                "median_item_count": 0,
                "seed_item_counts": [],
                "is_flat_short_labels": False,
                "item_format": "inline",
            }
            continue
        paragraph_counts = [item.get("paragraph_count", 0) for item in field_samples]
        all_category_labels = []
        for item in field_samples:
            all_category_labels.extend(item.get("category_labels", []))
        unique_categories = list(dict.fromkeys(all_category_labels))

        # topics/topic outline is hierarchical text, NEVER a categorized dict.
        # Unit/Lesson/Module patterns are structural headings, not domain categories.
        is_topics = (field == "topics")
        hints[field] = {
            "output_style": "flat_lines" if is_topics else ("categorized" if any(item.get("has_category_labels") for item in field_samples) else "flat_lines"),
            "list_marker": "dash" if any(item.get("uses_dash_bullets") for item in field_samples) else "plain",
            "typical_paragraph_count": max(paragraph_counts) if paragraph_counts else 0,
            "samples": [item.get("sample", "") for item in field_samples[:2] if item.get("sample")],
        }
        # ── new fields for richer AI context ──
        hints[field]["uses_numbered_items"] = any(item.get("uses_numbered_items") for item in field_samples)
        hints[field]["uses_dash_bullets"] = any(item.get("uses_dash_bullets") for item in field_samples)
        hints[field]["category_labels"] = unique_categories
        hints[field]["has_sub_bullets"] = any(item.get("has_sub_bullets") for item in field_samples)
        # ── per-row item counts: capture the full distribution so prompts and
        # quality checks can reason about the template's actual cadence rather
        # than only the upper bound. ──
        seed_item_counts = [int(item.get("item_count", 0) or 0) for item in field_samples]
        seed_item_counts_nonzero = [c for c in seed_item_counts if c > 0]
        hints[field]["seed_item_counts"] = seed_item_counts
        hints[field]["typical_item_count"] = max(seed_item_counts) if seed_item_counts else 0
        hints[field]["min_item_count"] = min(seed_item_counts_nonzero) if seed_item_counts_nonzero else 0
        if seed_item_counts_nonzero:
            sorted_counts = sorted(seed_item_counts_nonzero)
            mid = len(sorted_counts) // 2
            if len(sorted_counts) % 2 == 1:
                hints[field]["median_item_count"] = sorted_counts[mid]
            else:
                hints[field]["median_item_count"] = (sorted_counts[mid - 1] + sorted_counts[mid]) // 2 or sorted_counts[mid]
        else:
            hints[field]["median_item_count"] = 0
        hints[field]["lead_in"] = field_samples[0].get("lead_in")
        hints[field]["item_format"] = field_samples[0].get("item_format", "inline")
        hints[field]["has_hierarchy"] = any(item.get("has_hierarchy") for item in field_samples)
        hints[field]["hierarchy_levels"] = max(item.get("hierarchy_levels", 0) for item in field_samples)
        # ── boilerplate: text that must be COPIED VERBATIM, not regenerated ──
        # Lead-in sentences (e.g. "At the end of this week, ... should be able to:")
        bp_lead_ins = [item.get("boilerplate_lead_in") for item in field_samples if item.get("boilerplate_lead_in")]
        hints[field]["boilerplate_lead_in"] = bp_lead_ins[0] if bp_lead_ins else None
        # Structural headings (e.g. "Unit 1 – Title", "Lesson 1")
        bp_headings = []
        for item in field_samples:
            bp_headings.extend(item.get("boilerplate_structural_headings") or [])
        hints[field]["boilerplate_structural_headings"] = list(dict.fromkeys(bp_headings))  # unique, preserve order
        # Content-only count (lines that are actually replaceable content, not boilerplate)
        content_counts = [item.get("content_only_count", 0) for item in field_samples]
        hints[field]["content_only_count"] = max(content_counts) if content_counts else 0
        # ── flat short-label signal: drives "no padding, no expansion" downstream.
        hints[field]["is_flat_short_labels"] = (
            hints[field]["output_style"] == "flat_lines"
            and hints[field]["item_format"] == "label_fragments"
            and 0 < hints[field]["typical_item_count"] <= 4
        )

    return {"fields": hints}


def _ai_weekly_format_analysis(table, weekly_outline):
    """Use AI to analyze weekly cell structure when algorithmic detection
    (``_weekly_format_hints``) is uncertain — i.e. the cells have rich
    structure but the algorithm couldn't identify categories, numbering,
    or hierarchy patterns.

    This runs ONCE during template profiling.  Returns a dict that
    **overrides** the algorithmic hints for any field where the AI detects
    a clearer format than the regex-based approach.

    Returns ``None`` when not needed or when AI is unavailable.
    """
    current_hints = _weekly_format_hints(table, weekly_outline)
    fields_hints = current_hints.get("fields", {})
    if not fields_hints:
        return None

    # Only fire AI when at least one field has enough text but no
    # clear pattern detected (flat_lines with samples but no markers).
    needs_ai = False
    cell_texts = []
    for field in ("intended_learning_outcomes", "topics", "teaching_learning_activities"):
        fh = fields_hints.get(field, {})
        samples = fh.get("samples", [])
        flat = fh.get("output_style") == "flat_lines"
        no_markers = not fh.get("uses_numbered_items") and not fh.get("uses_dash_bullets") and not fh.get("has_category_labels")
        has_content = any(len(s) > 50 for s in samples if isinstance(s, str))
        if flat and no_markers and has_content:
            needs_ai = True
            for s in samples[:2]:
                if isinstance(s, str) and len(s) > 50:
                    cell_texts.append(f"[{field}] {s[:300]}")

    if not needs_ai or not cell_texts:
        return None

    try:
        from app.services.ai_client import AIClient
        prompt = f"""You are analyzing a CLP weekly outline table cell.
Determine the formatting structure of each cell.

For each field, decide:
1. output_style: "flat_lines" or "categorized"
2. category_labels: list of category names if categorized (e.g. ["Cognitive", "Affective", "Psychomotor"]), else []
3. uses_numbered_items: true/false
4. uses_dash_bullets: true/false
5. item_format: "complete_sentences", "semicolon_list", or "label_fragments"
6. typical_item_count: estimated number of items
7. has_hierarchy: true/false (Unit/Lesson/Section headings)

Cell contents:
{chr(10).join(cell_texts)}

Return JSON with a "fields" object keyed by field name.
"""
        model = AIClient.get_model()
        resp = AIClient.generate_with_retry(
            model, [prompt],
            {"response_mime_type": "application/json"},
            retries=1, task_type="weekly_format_analysis",
        )
        raw = AIClient.clean_ai_json(resp.text)
        import json as _json
        ai_result = _json.loads(raw) if isinstance(raw, str) else raw
        ai_fields = ai_result.get("fields", {}) if isinstance(ai_result, dict) else {}
        if not isinstance(ai_fields, dict):
            return None
        # Merge AI results into algorithmic hints — AI overrides when confident.
        for field in list(fields_hints.keys()):
            ai_f = ai_fields.get(field, {})
            if not isinstance(ai_f, dict):
                continue
            if ai_f.get("output_style") in ("categorized", "flat_lines"):
                fields_hints[field]["output_style"] = ai_f["output_style"]
            if ai_f.get("category_labels"):
                fields_hints[field]["category_labels"] = ai_f["category_labels"]
            if ai_f.get("uses_numbered_items") is not None:
                fields_hints[field]["uses_numbered_items"] = ai_f["uses_numbered_items"]
            if ai_f.get("uses_dash_bullets") is not None:
                fields_hints[field]["uses_dash_bullets"] = ai_f["uses_dash_bullets"]
            if ai_f.get("item_format") in ("complete_sentences", "semicolon_list", "label_fragments"):
                fields_hints[field]["item_format"] = ai_f["item_format"]
            if ai_f.get("typical_item_count"):
                fields_hints[field]["typical_item_count"] = ai_f["typical_item_count"]
            if ai_f.get("has_hierarchy") is not None:
                fields_hints[field]["has_hierarchy"] = ai_f["has_hierarchy"]
        return {"fields": fields_hints}
    except Exception:
        import traceback
        import logging
        logging.getLogger(__name__).warning(
            "AI weekly format analysis failed, using algorithmic result", exc_info=True)
        return None


def _normalize_week_label(value: str, fallback_index: int) -> str:
    cleaned = _clean(value)
    return cleaned or f"Row {fallback_index}"


def _weekly_row_kind(row: List[str]) -> str:
    first = _clean(row[0]) if row else ""
    joined = " ".join(row).upper()
    if not first and not any(_clean(cell) for cell in row):
        return "empty"
    if ("TIME" in joined and "FRAME" in joined) or "SCHEDULE" in joined:
        return "repeated_header"
    if _EXAM_RE.search(first) or _EXAM_RE.search(joined):
        return "exam_row"
    if _TIMEFRAME_LABEL_RE.search(first):
        return "timeframe_label"
    if first and len(first) <= 80 and any(_clean(cell) for cell in row[1:]):
        return "detected_writable"
    if not first and any(_clean(cell) for cell in row[1:]):
        return "detected_writable"
    return "non_week_row"


def _detect_weekly_outline(rows: List[List[str]], table_index: int) -> Optional[Dict[str, Any]]:
    header_idx = _find_weekly_header(rows)
    if header_idx is None:
        return None
    header = rows[header_idx]
    row_specs = []
    skipped_rows = []
    first_data_row = None
    last_data_row = None

    for row_idx, row in enumerate(rows[header_idx + 1:], start=header_idx + 1):
        first = _clean(row[0]) if row else ""
        row_kind = _weekly_row_kind(row)
        if row_kind == "empty":
            continue
        if row_kind in {"repeated_header", "exam_row", "non_week_row"}:
            skipped_rows.append({
                "row": row_idx,
                "reason": row_kind,
                "label": first,
                "source_preview": [_clean(cell) for cell in row[:6]],
            })
            continue
        first_data_row = row_idx if first_data_row is None else first_data_row
        last_data_row = row_idx
        row_specs.append({
            "index": len(row_specs) + 1,
            "label": _normalize_week_label(first, len(row_specs) + 1),
            "source_row": row_idx,
            "row_kind": row_kind,
            "source_preview": [_clean(cell) for cell in row[:6]],
        })

    if not row_specs:
        return None

    region_end = max(
        [last_data_row] + [
            item["row"]
            for item in skipped_rows
            if isinstance(item.get("row"), int) and item["row"] >= first_data_row
        ]
    ) + 1

    return {
        "locator": {
            "type": "table_region",
            "table_index": table_index,
            "start_row": first_data_row,
            "end_row": region_end,
            "writable_rows": [row["source_row"] for row in row_specs],
        },
        "row_count": len(row_specs),
        "rows": row_specs,
        "columns": _weekly_columns(header),
        "skipped_rows": skipped_rows,
        "header_row": header_idx,
        "header_labels": header,
    }


def _detect_reference_sections(doc) -> List[Dict[str, Any]]:
    sections = []
    paragraphs = list(getattr(doc, "paragraphs", []) or [])
    total = len(paragraphs)
    for index, paragraph in enumerate(paragraphs):
        heading = _clean(getattr(paragraph, "text", "")).rstrip(":")
        if not _REFERENCE_HEADING_RE.match(heading):
            continue
        content_start = None
        end_index = total
        for j in range(index + 1, total):
            text = _clean(getattr(paragraphs[j], "text", ""))
            if not text:
                continue
            if _REFERENCE_STOP_RE.match(text):
                end_index = j
                break
            if content_start is None:
                content_start = j
        if content_start is None or content_start >= end_index:
            continue
        sections.append({
            "id": "references" if not sections else f"references_{len(sections) + 1}",
            "label": heading.title() if heading else "References",
            "locator": {
                "type": "paragraph_range",
                "start_index": content_start,
                "end_index": end_index,
            },
            "content_type": "paragraph_range",
            "row_capacity": end_index - content_start,
        })
    return sections


def _detect_consultation(rows: List[List[str]], table_index: int) -> Optional[Dict[str, Any]]:
    if not rows:
        return None
    header = " ".join(rows[0]).upper()
    if not (("DAYS" in header or "NAME OF INSTRUCTOR" in header) and ("TIME" in header or "AVAILABILITY" in header)):
        return None
    columns: Dict[str, int] = {}
    for idx, cell in enumerate(rows[0]):
        text = _clean(cell).lower()
        if ("name" in text and "instructor" in text) or text in {"instructor", "faculty"}:
            columns["name"] = idx
        elif "day" in text:
            columns["days"] = idx
        elif "time" in text or "availability" in text:
            columns["time"] = idx
        elif "room" in text or "venue" in text:
            columns["room"] = idx
    return {
        "locator": {"type": "table_region", "table_index": table_index, "start_row": 1, "end_row": len(rows)},
        "row_capacity": max(len(rows) - 1, 0),
        "columns": columns or {"days": 0, "time": 1, "room": 2},
        "labels": [_clean(cell) for cell in rows[0]],
    }


def _detect_signatories(rows: List[List[str]], table_index: int) -> Optional[Dict[str, Any]]:
    table_text = " ".join(" ".join(row) for row in rows).upper()
    if not any(key in table_text for key in ["PREPARED", "REVIEWED", "ENDORSED", "APPROVED", "SIGNATURE"]):
        return None
    # Detect name, position, date, signature column indices from headers
    columns: Dict[str, int] = {}
    for idx, cell in enumerate(rows[0]):
        cell_lower = cell.lower()
        if any(kw in cell_lower for kw in ["name", "signatory"]):
            columns.setdefault("name", idx)
        if any(kw in cell_lower for kw in ["position", "designation", "title"]):
            columns.setdefault("position", idx)
        if any(kw in cell_lower for kw in ["date", "submitted", "reviewed", "endorsed"]):
            columns.setdefault("date", idx)
    return {
        "locator": {"type": "table_region", "table_index": table_index, "start_row": 0, "end_row": max(len(rows) - 1, 0)},
        "row_capacity": len(rows),
        "columns": columns or {"name": 1, "position": 2},
        "labels": [_clean(row[0]) for row in rows if row and _clean(row[0])],
    }


def default_generation_spec() -> Dict[str, Any]:
    return {
        "schema_version": SPEC_SCHEMA_VERSION,
        "source": "default",
        "alignment_style": "clo_based",
        "metadata_fields": [],
        "clo_groups": [{
            "id": "clo_group_1",
            "label": "CLO Alignment",
            "program_scope": {},
            "locator": {},
            "row_count": len(DEFAULT_CLO_ROWS),
            "rows": [
                {"index": idx, "code": code, "label": code, "domain": domain}
                for idx, (code, domain) in enumerate(DEFAULT_CLO_ROWS, start=1)
            ],
            "columns": {},
            "alignment_style": "clo_based",
            "skipped_rows": [],
        }],
        "weekly_outline": {
            "row_count": len(DEFAULT_WEEK_LABELS),
            "rows": [
                {"index": idx, "label": label}
                for idx, label in enumerate(DEFAULT_WEEK_LABELS, start=1)
            ],
            "columns": {},
            "skipped_rows": [],
        },
        "program_institutional_alignments": [],
        "signatories": {"sections": [], "row_capacity": 0},
        "consultation": {"sections": [], "row_capacity": 0},
        "references": {"sections": [], "row_capacity": 0},
        "warnings": [],
    }


def normalize_generation_spec(spec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(spec, dict):
        return default_generation_spec()
    base = default_generation_spec()
    merged = {**base, **spec}
    merged["schema_version"] = str(merged.get("schema_version") or SPEC_SCHEMA_VERSION)

    raw_pi_alignments = merged.get("program_institutional_alignments")
    has_explicit_pi = isinstance(raw_pi_alignments, list) and bool(raw_pi_alignments)
    clo_groups = merged.get("clo_groups") if isinstance(merged.get("clo_groups"), list) else []
    normalized_groups = []
    for group_index, group in enumerate(clo_groups, start=1):
        if not isinstance(group, dict):
            continue
        rows = group.get("rows") if isinstance(group.get("rows"), list) else []
        normalized_rows = []
        for idx, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                continue
            code = _normalize_clo_code(str(row.get("code") or row.get("label") or ""), idx)
            normalized_rows.append({
                **row,
                "index": idx,
                "code": code,
                "label": row.get("label") or code,
                "domain": str(row.get("domain") or "cognitive").lower(),
            })
        if not normalized_rows:
            normalized_rows = base["clo_groups"][0]["rows"]
        normalized_groups.append({
            **group,
            "id": group.get("id") or f"clo_group_{group_index}",
            "label": group.get("label") or f"CLO Alignment {group_index}",
            "row_count": len(normalized_rows),
            "rows": normalized_rows,
            "columns": group.get("columns") if isinstance(group.get("columns"), dict) else {},
            "program_scope": group.get("program_scope") if isinstance(group.get("program_scope"), dict) else {},
            "skipped_rows": group.get("skipped_rows") if isinstance(group.get("skipped_rows"), list) else [],
        })
    merged["clo_groups"] = normalized_groups if (normalized_groups or has_explicit_pi) else base["clo_groups"]

    weekly = merged.get("weekly_outline") if isinstance(merged.get("weekly_outline"), dict) else {}
    weekly_rows = weekly.get("rows") if isinstance(weekly.get("rows"), list) else []
    normalized_weekly = []
    for idx, row in enumerate(weekly_rows, start=1):
        if not isinstance(row, dict):
            continue
        normalized_weekly.append({
            **row,
            "index": idx,
            "label": _normalize_week_label(str(row.get("label") or row.get("time_frame_label") or ""), idx),
        })
    if not normalized_weekly:
        normalized_weekly = base["weekly_outline"]["rows"]
    merged["weekly_outline"] = {
        **base["weekly_outline"],
        **weekly,
        "row_count": len(normalized_weekly),
        "rows": normalized_weekly,
        "columns": weekly.get("columns") if isinstance(weekly.get("columns"), dict) else {},
        "skipped_rows": weekly.get("skipped_rows") if isinstance(weekly.get("skipped_rows"), list) else [],
    }
    pi_alignments = merged.get("program_institutional_alignments")
    normalized_pi = []
    if isinstance(pi_alignments, list):
        for index, alignment in enumerate(pi_alignments, start=1):
            if not isinstance(alignment, dict):
                continue
            row_original = [_clean(item) for item in (alignment.get("row_labels_original") or []) if _clean(item)]
            row_normalized = [
                normalize_alignment_row_label(item)
                for item in (alignment.get("row_labels_normalized") or row_original)
                if normalize_alignment_row_label(item)
            ]
            col_original = [_clean(item) for item in (alignment.get("column_labels_original") or []) if _clean(item)]
            col_normalized = [
                normalize_alignment_column_label(item)
                for item in (alignment.get("column_labels_normalized") or normalize_duplicate_alignment_columns(col_original))
                if normalize_alignment_column_label(item)
            ]
            if not row_normalized or not col_normalized:
                continue
            required_keys = alignment.get("required_alignment_keys")
            if not isinstance(required_keys, list) or not required_keys:
                required_keys = build_required_alignment_keys(row_normalized, col_normalized)
            normalized_pi.append({
                **alignment,
                "id": alignment.get("id") or f"program_institutional_alignment_{index}",
                "alignment_format": "program_institutional_checkmark",
                "value_style": alignment.get("value_style") or "checkmark",
                "row_labels_original": row_original,
                "row_labels_normalized": row_normalized,
                "column_labels_original": col_original,
                "column_labels_normalized": col_normalized,
                "required_alignment_keys": [str(item) for item in required_keys if str(item).strip()],
                "locator": alignment.get("locator") if isinstance(alignment.get("locator"), dict) else {},
                "column_index_by_normalized": alignment.get("column_index_by_normalized") if isinstance(alignment.get("column_index_by_normalized"), dict) else {},
            })
    merged["program_institutional_alignments"] = normalized_pi
    merged["warnings"] = [str(item).strip() for item in (merged.get("warnings") or []) if str(item).strip()]
    return merged


def extract_generation_spec(doc, profile: Optional[Dict[str, Any]] = None, template_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    metadata_fields: List[Dict[str, Any]] = []
    metadata_tables = 0
    clo_groups: List[Dict[str, Any]] = []
    weekly_outline = None
    consultation_sections = []
    signatory_sections = []
    program_institutional_alignments: List[Dict[str, Any]] = []
    warnings = []
    scopes = _program_scopes(template_context or {})
    detected_program_scopes: List[Dict[str, Any]] = []
    course_context: Dict[str, str] = {}

    for table_index, table in enumerate(doc.tables):
        rows = _table_rows(table)
        if not rows:
            continue

        found_meta = _detect_metadata_fields(rows, table_index)
        if found_meta:
            metadata_tables += 1
            existing = {item["field"] for item in metadata_fields}
            metadata_fields.extend(item for item in found_meta if item["field"] not in existing)
            for key, value in _extract_metadata_values(rows).items():
                if value and key not in course_context:
                    course_context[key] = value

        program_scope = _detect_program_scope(rows, table_index, len(detected_program_scopes) + 1)
        if program_scope:
            detected_program_scopes.append(program_scope)

        program_inst_alignment = detect_program_institutional_checkmark_table(
            rows,
            table_index,
            len(program_institutional_alignments) + 1,
        )
        if program_inst_alignment:
            program_institutional_alignments.append(program_inst_alignment)

        scope = None
        if len(clo_groups) < len(detected_program_scopes):
            scope = detected_program_scopes[len(clo_groups)]
        elif len(clo_groups) < len(scopes):
            scope = scopes[len(clo_groups)]
        scope = _scope_with_course_context(scope, course_context)
        clo_group = _detect_clo_group(rows, table_index, scope, len(clo_groups) + 1)
        if clo_group:
            clo_groups.append(clo_group)
            continue

        if weekly_outline is None:
            weekly_outline = _detect_weekly_outline(rows, table_index)
            if weekly_outline:
                hints = _weekly_format_hints(table, weekly_outline)
                ai_hints = _ai_weekly_format_analysis(table, weekly_outline)
                weekly_outline["format_hints"] = ai_hints if ai_hints else hints
                continue

        consultation = _detect_consultation(rows, table_index)
        if consultation:
            consultation_sections.append(consultation)
            continue

        signatories = _detect_signatories(rows, table_index)
        if signatories:
            signatory_sections.append(signatories)

    paragraph_meta = _detect_paragraph_metadata_fields(doc)
    if paragraph_meta:
        existing_locator_keys = {
            (
                _canonical_metadata_field(item.get("field"), item.get("label")),
                (item.get("locator") or {}).get("type"),
                (item.get("locator") or {}).get("prefix"),
                tuple((item.get("locator") or {}).get("search_range") or []),
            )
            for item in metadata_fields
            if isinstance(item, dict)
        }
        for item in paragraph_meta:
            loc = item.get("locator") or {}
            key = (
                _canonical_metadata_field(item.get("field"), item.get("label")),
                loc.get("type"),
                loc.get("prefix"),
                tuple(loc.get("search_range") or []),
            )
            if key not in existing_locator_keys:
                metadata_fields.append(item)
                existing_locator_keys.add(key)
    reference_sections = _detect_reference_sections(doc)

    if metadata_tables > 1:
        warnings.append("Multiple course metadata tables were detected; confirm the intended course block before generation.")
    if not metadata_fields:
        warnings.append("No course metadata table was confidently detected.")
    if not clo_groups and not program_institutional_alignments:
        warnings.append("No CLO alignment rows were detected; default 8 CLO rows will be used.")
    if not weekly_outline:
        warnings.append("No weekly outline rows were detected; default weekly labels will be used.")
    elif any(row.get("row_kind") == "detected_writable" for row in weekly_outline.get("rows", [])):
        warnings.append("Some weekly outline rows use template-specific labels instead of Week labels; Copilot will preserve the detected labels.")
    if len(clo_groups) > 1:
        warnings.append(f"{len(clo_groups)} CLO alignment groups were detected; Copilot will generate one CLP with grouped alignments.")
    if program_institutional_alignments:
        warnings.append(
            f"{len(program_institutional_alignments)} program-to-institutional checkmark alignment table(s) were detected; Copilot will preserve their rows and columns."
        )

    # Determine the dominant alignment style across all detected CLO groups.
    detected_styles = [
        group.get("alignment_style", "clo_based")
        for group in clo_groups
        if isinstance(group, dict)
    ]
    dominant_style = "checkmark" if detected_styles and all(s == "checkmark" for s in detected_styles) else "clo_based"

    spec = {
        "schema_version": SPEC_SCHEMA_VERSION,
        "source": "template_profile",
        "alignment_style": dominant_style,
        "metadata_fields": metadata_fields,
        "clo_groups": clo_groups or ([] if program_institutional_alignments else default_generation_spec()["clo_groups"]),
        "weekly_outline": weekly_outline or default_generation_spec()["weekly_outline"],
        "program_institutional_alignments": program_institutional_alignments,
        "signatories": {
            "sections": signatory_sections,
            "row_capacity": sum(section.get("row_capacity", 0) for section in signatory_sections),
        },
        "consultation": {
            "sections": consultation_sections,
            "row_capacity": sum(section.get("row_capacity", 0) for section in consultation_sections),
        },
        "references": {
            "sections": reference_sections,
            "row_capacity": sum(section.get("row_capacity", 0) for section in reference_sections),
        },
        "warnings": warnings,
    }
    return normalize_generation_spec(spec)


def build_generation_spec(profile_data: Optional[Dict[str, Any]], source_doc=None) -> Dict[str, Any]:
    """Return a normalized generation spec from saved profile data.

    Existing profiles remain valid: if they do not already contain a
    generation spec, callers may pass the stored source DOCX document and the
    spec will be derived on the fly.
    """
    data = profile_data or {}
    if source_doc is not None:
        derived = extract_generation_spec(
            source_doc,
            profile=data.get("profile") if isinstance(data.get("profile"), dict) else None,
            template_context=data.get("template_context") if isinstance(data.get("template_context"), dict) else None,
        )
        existing = normalize_generation_spec(data.get("generation_spec")) if isinstance(data.get("generation_spec"), dict) else None
        if existing:
            existing_pi = existing.get("program_institutional_alignments") if isinstance(existing.get("program_institutional_alignments"), list) else []
            derived_pi = derived.get("program_institutional_alignments") if isinstance(derived.get("program_institutional_alignments"), list) else []
            existing_clo_has_locators = any(
                isinstance(group, dict)
                and isinstance(group.get("locator"), dict)
                and group.get("locator", {}).get("type") == "table_region"
                for group in existing.get("clo_groups") or []
            )
            derived_clo_has_locators = any(
                isinstance(group, dict)
                and isinstance(group.get("locator"), dict)
                and group.get("locator", {}).get("type") == "table_region"
                for group in derived.get("clo_groups") or []
            )
            # Preserve existing alignment style when re-derivation produces different result
            if existing.get("alignment_style") in ("checkmark",) and derived.get("alignment_style") != existing.get("alignment_style"):
                derived["alignment_style"] = existing["alignment_style"]
                for i, g in enumerate(derived.get("clo_groups") or []):
                    if i < len(existing.get("clo_groups") or []):
                        eg = existing["clo_groups"][i]
                        if eg.get("checkmark_po_codes"):
                            g["checkmark_po_codes"] = eg["checkmark_po_codes"]
                            g["alignment_style"] = "checkmark"
            if not derived_pi and (existing_pi or existing_clo_has_locators or not derived_clo_has_locators):
                return existing
        return derived
    if isinstance(data.get("generation_spec"), dict):
        return normalize_generation_spec(data.get("generation_spec"))
    return normalize_generation_spec(None)


def _locator_table_index(section: Dict[str, Any]) -> Optional[int]:
    loc = section.get("locator") if isinstance(section, dict) else {}
    if not isinstance(loc, dict):
        return None
    if loc.get("type") != "table_region":
        return None
    try:
        return int(loc.get("table_index"))
    except (TypeError, ValueError):
        return None


def _section_matches_kind(section: Dict[str, Any], kind: str) -> bool:
    text = f"{section.get('id', '')} {section.get('label', '')} {section.get('content_type', '')}".lower()
    if kind == "clo":
        return (
            "clo" in text
            or "course learning outcome" in text
            or "course outcomes" in text
            or "co_po" in text
            or "alignment" in text
        )
    if kind == "weekly":
        return "weekly" in text or "learning plan" in text or "outline" in text
    if kind == "consultation":
        return "consultation" in text or "availability" in text
    if kind == "signatories":
        return "signator" in text or "signature" in text or "approval" in text
    if kind == "program_institutional":
        return "program" in text and ("institutional" in text or "outcome" in text or "alignment" in text)
    if kind == "references":
        return "reference" in text or "bibliography" in text or "works cited" in text
    return False


def _upsert_detected_section(sections: List[Dict[str, Any]], detected: Dict[str, Any], kind: str) -> bool:
    target_table = _locator_table_index(detected)
    for idx, section in enumerate(sections):
        if not isinstance(section, dict):
            continue
        if section.get("id") == detected.get("id"):
            sections[idx] = {**section, **detected}
            return True
        if target_table is not None and _locator_table_index(section) == target_table and _section_matches_kind(section, kind):
            sections[idx] = {**section, **detected}
            return True
    sections.append(detected)
    return False


def _canonical_metadata_field(field: Any, label: Any = "") -> str:
    value = str(field or "").strip().lower()
    aliases = {
        "course_number": "course_code",
        "course_no": "course_code",
        "descriptive_title": "course_title",
        "hours_per_week": "contact_hours_display",
        "class_hours": "contact_hours_display",
        "contact_hours": "contact_hours_display",
        "credit_units": "units_display",
        "unit": "units_display",
        "units": "units_display",
        "target_sdg": "target_sdgs_display",
        "target_sdgs": "target_sdgs_display",
        "service_learning": "service_learning_component",
        "pre_requisites": "pre_requisite",
        "pre_requisites_display": "pre_requisite",
        "co_requisites": "co_requisite",
        "co_requisites_display": "co_requisite",
    }
    if value in aliases:
        return aliases[value]
    if value:
        return value
    label_field = _metadata_field_for_label(str(label or ""))
    return label_field or ""


def _dedupe_metadata_fields(fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for item in fields or []:
        if not isinstance(item, dict):
            continue
        item = dict(item)
        canonical_field = _canonical_metadata_field(item.get("field"), item.get("label"))
        if canonical_field:
            item["field"] = canonical_field
        locator = item.get("locator") if isinstance(item.get("locator"), dict) else {}
        key = (
            canonical_field,
            _norm_label(str(item.get("label") or "")),
            locator.get("type"),
            locator.get("table_index"),
            locator.get("row"),
            locator.get("col"),
            locator.get("prefix"),
            tuple(locator.get("search_range") or []),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _metadata_locator_replace_key(item: Dict[str, Any]) -> Optional[tuple]:
    locator = item.get("locator") if isinstance(item.get("locator"), dict) else {}
    if locator.get("type") != "table_cell":
        return None
    try:
        return (
            _canonical_metadata_field(item.get("field"), item.get("label")),
            int(locator.get("table_index")),
            int(locator.get("row")),
        )
    except (TypeError, ValueError):
        return None


def _metadata_locator_unique_key(item: Dict[str, Any]) -> tuple:
    locator = item.get("locator") if isinstance(item.get("locator"), dict) else {}
    return (
        _canonical_metadata_field(item.get("field"), item.get("label")),
        _norm_label(str(item.get("label") or "")),
        locator.get("type"),
        locator.get("table_index"),
        locator.get("row"),
        locator.get("col"),
        locator.get("prefix"),
        tuple(locator.get("search_range") or []),
    )


def _label_program_outcome_sections(sections: List[Dict[str, Any]], generation_spec: Dict[str, Any]) -> None:
    scopes_by_table = {}
    for group in (generation_spec or {}).get("clo_groups") or []:
        scope = group.get("program_scope") if isinstance(group.get("program_scope"), dict) else {}
        table_index = scope.get("source_table_index")
        if table_index is None:
            continue
        try:
            scopes_by_table[int(table_index)] = scope
        except (TypeError, ValueError):
            continue
    for section in sections:
        if not isinstance(section, dict):
            continue
        table_index = _locator_table_index(section)
        scope = scopes_by_table.get(table_index)
        if not scope:
            continue
        text = f"{section.get('id', '')} {section.get('label', '')}".lower()
        if "program" not in text or "outcome" not in text:
            continue
        program_name = scope.get("program_name") or scope.get("program_code")
        if program_name:
            section["label"] = f"Program Outcomes - {program_name}"
            section["program_scope"] = scope


def merge_spec_into_profile(profile: Dict[str, Any], generation_spec: Dict[str, Any]) -> Dict[str, Any]:
    """Add detected writable sections/columns to a profile without dropping AI data."""
    profile = dict(profile or {})
    sections = [
        section
        for section in list(profile.get("sections") or [])
        if not (
            isinstance(section, dict)
            and str(section.get("id") or "").startswith("clo_group_")
            and _locator_table_index(section) is None
        )
    ]

    for group in (generation_spec or {}).get("clo_groups") or []:
        section_id = group.get("id")
        locator = group.get("locator") if isinstance(group.get("locator"), dict) else {}
        if not section_id or locator.get("type") != "table_region":
            continue
        detected = {
            "id": section_id,
            "label": group.get("label") or section_id,
            "locator": locator,
            "content_type": "table_region",
            "columns": group.get("columns") or {},
            "row_count": group.get("row_count") or len(group.get("rows") or []),
            "program_scope": group.get("program_scope") or {},
            "notes": "Detected CLO alignment group for AI Copilot generation.",
        }
        _upsert_detected_section(sections, detected, "clo")

    for index, alignment in enumerate((generation_spec or {}).get("program_institutional_alignments") or [], start=1):
        if not isinstance(alignment, dict):
            continue
        locator = alignment.get("locator") if isinstance(alignment.get("locator"), dict) else {}
        if locator.get("type") != "table_region":
            continue
        section_id = alignment.get("id") or f"program_institutional_alignment_{index}"
        detected = {
            "id": section_id,
            "label": "Program Outcomes to Institutional Outcomes Alignment",
            "locator": locator,
            "content_type": "table_region",
            "alignment_format": "program_institutional_checkmark",
            "value_style": alignment.get("value_style") or "checkmark",
            "row_labels_original": alignment.get("row_labels_original") or [],
            "row_labels_normalized": alignment.get("row_labels_normalized") or [],
            "column_labels_original": alignment.get("column_labels_original") or [],
            "column_labels_normalized": alignment.get("column_labels_normalized") or [],
            "required_alignment_keys": alignment.get("required_alignment_keys") or [],
            "alignment_column_indices": alignment.get("alignment_column_indices") or [],
            "column_index_by_normalized": alignment.get("column_index_by_normalized") or {},
            "row_count": len(alignment.get("row_labels_normalized") or []),
            "notes": "Detected program-to-institutional checkmark matrix for AI Copilot generation.",
        }
        _upsert_detected_section(sections, detected, "program_institutional")

    weekly = (generation_spec or {}).get("weekly_outline") or {}
    if weekly.get("locator"):
        detected = {
            "id": "weekly_outline",
            "label": "Weekly Course Outline",
            "locator": weekly.get("locator"),
            "content_type": "table_region",
            "columns": weekly.get("columns") or {},
            "format_hints": weekly.get("format_hints") if isinstance(weekly.get("format_hints"), dict) else {},
            "row_count": weekly.get("row_count") or len(weekly.get("rows") or []),
            "skipped_rows": weekly.get("skipped_rows") if isinstance(weekly.get("skipped_rows"), list) else [],
            "notes": "Detected weekly outline writable rows for AI Copilot generation.",
        }
        _upsert_detected_section(sections, detected, "weekly")

    for reference in (((generation_spec or {}).get("references") or {}).get("sections") or []):
        if not isinstance(reference, dict) or not reference.get("locator"):
            continue
        detected = {
            "id": reference.get("id") or "references",
            "label": reference.get("label") or "References",
            "locator": reference.get("locator"),
            "content_type": reference.get("content_type") or "paragraph_range",
            "row_capacity": reference.get("row_capacity") or 0,
            "notes": "Detected references section for AI Copilot generation.",
        }
        _upsert_detected_section(sections, detected, "references")

    for index, consultation in enumerate(((generation_spec or {}).get("consultation") or {}).get("sections") or [], start=1):
        if not isinstance(consultation, dict) or not consultation.get("locator"):
            continue
        detected = {
            "id": "consultation_hours" if index == 1 else f"consultation_hours_{index}",
            "label": "Consultation Hours",
            "locator": consultation.get("locator"),
            "content_type": "table_region",
            "columns": consultation.get("columns") or {},
            "row_capacity": consultation.get("row_capacity") or 0,
            "labels": consultation.get("labels") or [],
            "notes": "Detected consultation table for profile rendering.",
        }
        _upsert_detected_section(sections, detected, "consultation")

    for index, signatories in enumerate(((generation_spec or {}).get("signatories") or {}).get("sections") or [], start=1):
        if not isinstance(signatories, dict) or not signatories.get("locator"):
            continue
        detected = {
            "id": "approval_signatories" if index == 1 else f"approval_signatories_{index}",
            "label": "Approval and Signatory Table",
            "locator": signatories.get("locator"),
            "content_type": "table_region",
            "row_capacity": signatories.get("row_capacity") or 0,
            "labels": signatories.get("labels") or [],
            "columns": signatories.get("columns") or {},
            "notes": "Detected signatory table for profile rendering.",
        }
        _upsert_detected_section(sections, detected, "signatories")

    _label_program_outcome_sections(sections, generation_spec or {})
    profile["sections"] = sections
    merged_meta = _dedupe_metadata_fields(list(profile.get("metadata_fields") or []))
    if generation_spec.get("metadata_fields"):
        replacement_keys = {
            _metadata_locator_replace_key(item)
            for item in generation_spec["metadata_fields"]
            if isinstance(item, dict) and _metadata_locator_replace_key(item) is not None
        }
        if replacement_keys:
            merged_meta = [
                item for item in merged_meta
                if not (
                    isinstance(item, dict)
                    and _metadata_locator_replace_key(item) in replacement_keys
                    and _metadata_locator_unique_key(item) not in {
                        _metadata_locator_unique_key(gen_item)
                        for gen_item in generation_spec["metadata_fields"]
                        if isinstance(gen_item, dict)
                    }
                )
            ]
        existing_meta = {
            _metadata_locator_unique_key(item)
            for item in merged_meta
            if isinstance(item, dict)
        }
        for item in generation_spec["metadata_fields"]:
            unique_key = _metadata_locator_unique_key(item)
            if unique_key not in existing_meta:
                merged_meta.append(item)
                existing_meta.add(unique_key)
    profile["metadata_fields"] = _dedupe_metadata_fields(merged_meta)

    if generation_spec.get("clo_groups"):
        primary_columns = generation_spec["clo_groups"][0].get("columns") or {}
        if primary_columns:
            profile["alignment_columns"] = primary_columns
    if generation_spec.get("program_institutional_alignments"):
        profile["program_institutional_alignments"] = generation_spec["program_institutional_alignments"]
    return profile
