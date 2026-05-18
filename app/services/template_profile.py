"""
Template profile schema, validation, and locator resolution.

A *template_profile* is a JSON document describing the structural layout
of a departmental CLP DOCX template.  It tells the system:

  - which sections exist and in what order
  - where each section lives (table index, row/col, paragraph index, etc.)
  - what alignment columns are expected (for CLO tables)
  - what metadata fields appear on the front page

This module provides:

  validate_profile(data)      – validate against the canonical JSON schema
  resolve_locator(doc, loc)   – turn a locator dict into a docx element
  validate_profile_against_doc(doc, profile) – verify every locator resolves

The canonical schema mirrors the JSON structure agreed upon in the design
session.  Minimal required top-level keys:

  {
    "schema_version": "1",
    "department": str,
    "sections": [ ... ],      // ordered list of section definitions
    "metadata_fields": [ ... ],
    "alignment_columns": { ... }
  }
"""

from __future__ import annotations

import hashlib
import re

import copy
from typing import Any, Dict, List, Optional, Tuple

from docx import Document
from docx.table import Table, _Row, _Cell


# ── canonical schema description (light-weight, no jsonschema dependency) ──

CURRENT_SCHEMA_VERSION = "1"

_REQUIRED_TOP_KEYS = {"schema_version", "department", "sections"}

_SECTION_REQUIRED_KEYS = {"id", "label", "locator"}

_LOCATOR_TYPES = {
    "table_cell",      # table_index, row, col
    "table_region",    # table_index, start_row, end_row (optional col range)
    "table",           # table_index (alias for whole table)
    "table_index",     # table_index (alias for whole table)
    "paragraph",       # paragraph_index
    "paragraph_range", # start_index, end_index
    "paragraph_prefix", # search_range, prefix
    "header_match",    # header_text (regex or exact) → next element
}


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

class ProfileValidationError(Exception):
    """Raised when a template_profile dict fails validation."""

    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__(f"Profile validation failed: {'; '.join(errors)}")


def validate_profile(data: Dict[str, Any]) -> List[str]:
    """Validate *data* against the canonical template_profile schema.

    Returns a list of error strings.  An empty list means the profile is
    valid.
    """
    errors: List[str] = []

    if not isinstance(data, dict):
        return ["Profile must be a JSON object (dict)."]

    # Top-level keys
    for key in _REQUIRED_TOP_KEYS:
        if key not in data:
            errors.append(f"Missing required top-level key: '{key}'.")

    if data.get("schema_version") != CURRENT_SCHEMA_VERSION:
        errors.append(
            f"Unsupported schema_version '{data.get('schema_version')}'; "
            f"expected '{CURRENT_SCHEMA_VERSION}'."
        )

    # Sections
    sections = data.get("sections")
    if sections is not None:
        if not isinstance(sections, list):
            errors.append("'sections' must be an array.")
        else:
            seen_ids = set()
            for idx, sec in enumerate(sections):
                prefix = f"sections[{idx}]"
                if not isinstance(sec, dict):
                    errors.append(f"{prefix} must be an object.")
                    continue
                for k in _SECTION_REQUIRED_KEYS:
                    if k not in sec:
                        errors.append(f"{prefix}: missing required key '{k}'.")
                sec_id = sec.get("id")
                if sec_id is not None:
                    if sec_id in seen_ids:
                        errors.append(f"{prefix}: duplicate section id '{sec_id}'.")
                    seen_ids.add(sec_id)

                loc = sec.get("locator")
                if loc is not None:
                    _validate_locator(loc, prefix, errors)

    # metadata_fields (optional but typed)
    meta = data.get("metadata_fields")
    if meta is not None and not isinstance(meta, list):
        errors.append("'metadata_fields' must be an array.")

    # alignment_columns (optional but typed)
    align = data.get("alignment_columns")
    if align is not None and not isinstance(align, dict):
        errors.append("'alignment_columns' must be an object.")

    return errors


def _validate_locator(loc: Any, prefix: str, errors: List[str]):
    """Validate a single locator dict."""
    if not isinstance(loc, dict):
        errors.append(f"{prefix}.locator must be an object.")
        return
    loc_type = loc.get("type")
    if loc_type not in _LOCATOR_TYPES:
        errors.append(
            f"{prefix}.locator.type '{loc_type}' not in "
            f"{sorted(_LOCATOR_TYPES)}."
        )
        return

    if loc_type == "table_cell":
        for k in ("table_index", "row", "col"):
            if k not in loc:
                errors.append(f"{prefix}.locator ({loc_type}): missing '{k}'.")
            elif not isinstance(loc[k], int):
                errors.append(f"{prefix}.locator.{k} must be an integer.")

    elif loc_type == "table_region":
        for k in ("table_index", "start_row"):
            if k not in loc:
                errors.append(f"{prefix}.locator ({loc_type}): missing '{k}'.")
            elif not isinstance(loc[k], int):
                errors.append(f"{prefix}.locator.{k} must be an integer.")

    elif loc_type in ("table", "table_index"):
        if "table_index" not in loc:
            errors.append(f"{prefix}.locator ({loc_type}): missing 'table_index'.")
        elif not isinstance(loc["table_index"], int):
            errors.append(f"{prefix}.locator.table_index must be an integer.")

    elif loc_type == "paragraph":
        if "paragraph_index" not in loc:
            errors.append(f"{prefix}.locator ({loc_type}): missing 'paragraph_index'.")
        elif not isinstance(loc["paragraph_index"], int):
            errors.append(f"{prefix}.locator.paragraph_index must be an integer.")

    elif loc_type == "paragraph_range":
        start = loc.get("start_index", loc.get("start"))
        end = loc.get("end_index", loc.get("end"))
        if start is None:
            errors.append(f"{prefix}.locator ({loc_type}): missing 'start_index'.")
        elif not isinstance(start, int):
            errors.append(f"{prefix}.locator.start_index must be an integer.")
        if end is None:
            errors.append(f"{prefix}.locator ({loc_type}): missing 'end_index'.")
        elif not isinstance(end, int):
            errors.append(f"{prefix}.locator.end_index must be an integer.")

    elif loc_type == "paragraph_prefix":
        if "prefix" not in loc:
            errors.append(f"{prefix}.locator ({loc_type}): missing 'prefix'.")
        search_range = loc.get("search_range")
        if search_range is not None and (
            not isinstance(search_range, list) or len(search_range) != 2 or
            not all(isinstance(item, int) for item in search_range)
        ):
            errors.append(f"{prefix}.locator.search_range must be [start, end].")

    elif loc_type == "header_match":
        if "header_text" not in loc:
            errors.append(f"{prefix}.locator ({loc_type}): missing 'header_text'.")


# ---------------------------------------------------------------------------
# Locator resolution
# ---------------------------------------------------------------------------

class LocatorResolutionError(Exception):
    """Raised when a locator cannot be resolved against a document."""
    pass


def _cell_text_for_fingerprint(cell) -> str:
    """Get first 60 chars of cell text for fingerprint matching."""
    return (cell.text or "").strip()[:60]


def _compute_fingerprint_for_table(table, start_row=0) -> str:
    """Compute the same fingerprint as template_profiler._compute_table_fingerprint."""
    if start_row < 0 or start_row >= len(table.rows):
        start_row = 0
    first_row = table.rows[start_row]
    row_texts = [_cell_text_for_fingerprint(cell) for cell in first_row.cells]
    col_count = len(table.columns)
    approx_rows = len(table.rows)
    raw = "|".join(row_texts) + f"|c{col_count}|r{approx_rows}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _recover_table_index(doc: Document, locator: dict) -> int | None:
    """Try to find a table matching the locator's fingerprint.

    Returns the table index if found, None otherwise.
    """
    stored_fingerprint = locator.get("fingerprint")
    if not stored_fingerprint:
        return None
    start_row = locator.get("start_row", locator.get("row", 0))
    if not isinstance(start_row, int):
        start_row = 0
    for idx, table in enumerate(doc.tables):
        try:
            fp = _compute_fingerprint_for_table(table, start_row)
            if fp == stored_fingerprint:
                return idx
        except Exception:
            continue
    # Fallback: try start_row=0 for all tables
    if start_row != 0:
        for idx, table in enumerate(doc.tables):
            try:
                fp = _compute_fingerprint_for_table(table, 0)
                if fp == stored_fingerprint:
                    return idx
            except Exception:
                continue
    return None


def resolve_locator(doc: Document, locator: Dict[str, Any]) -> Any:
    """Resolve *locator* against *doc* and return the target element.

    Returns
    -------
    - For ``table_cell``: the :class:`docx.table._Cell` object.
    - For ``table_region``: a list of :class:`docx.table._Row` objects.
    - For ``paragraph``: the :class:`docx.text.paragraph.Paragraph`.
    - For ``header_match``: the first paragraph whose text matches.

    Raises
    ------
    LocatorResolutionError
        If the target element does not exist in the document.
    """
    loc_type = locator.get("type")

    if loc_type == "table_cell":
        return _resolve_table_cell(doc, locator)
    elif loc_type == "table_region":
        return _resolve_table_region(doc, locator)
    elif loc_type in ("table", "table_index"):
        # Alias for full table region
        loc_copy = dict(locator)
        loc_copy["start_row"] = 0
        return _resolve_table_region(doc, loc_copy)
    elif loc_type == "paragraph":
        return _resolve_paragraph(doc, locator)
    elif loc_type == "paragraph_range":
        return _resolve_paragraph_range(doc, locator)
    elif loc_type == "paragraph_prefix":
        return _resolve_paragraph_prefix(doc, locator)
    elif loc_type == "header_match":
        return _resolve_header_match(doc, locator)
    else:
        raise LocatorResolutionError(f"Unknown locator type: '{loc_type}'.")


def _resolve_table_cell(doc, loc):
    tables = doc.tables
    ti = loc["table_index"]
    if ti < 0 or ti >= len(tables):
        recovered = _recover_table_index(doc, loc)
        if recovered is not None:
            loc["table_index"] = recovered
            ti = recovered
        else:
            raise LocatorResolutionError(
                f"table_index {ti} out of range (doc has {len(tables)} tables)."
            )
    table = tables[ti]
    row_idx = loc["row"]
    col_idx = loc["col"]
    if row_idx < 0 or row_idx >= len(table.rows):
        raise LocatorResolutionError(
            f"row {row_idx} out of range (table {ti} has {len(table.rows)} rows)."
        )
    row = table.rows[row_idx]
    if col_idx < 0 or col_idx >= len(row.cells):
        raise LocatorResolutionError(
            f"col {col_idx} out of range (row {row_idx} has {len(row.cells)} cells)."
        )
    return row.cells[col_idx]


def _resolve_table_region(doc, loc):
    tables = doc.tables
    ti = loc["table_index"]
    if ti < 0 or ti >= len(tables):
        recovered = _recover_table_index(doc, loc)
        if recovered is not None:
            loc["table_index"] = recovered
            ti = recovered
        else:
            raise LocatorResolutionError(
                f"table_index {ti} out of range (doc has {len(tables)} tables)."
            )
    table = tables[ti]
    start = loc["start_row"]
    end = loc.get("end_row", len(table.rows))
    if start < 0 or start >= len(table.rows):
        raise LocatorResolutionError(
            f"start_row {start} out of range (table {ti} has {len(table.rows)} rows)."
        )
    end = min(end, len(table.rows))
    return [table.rows[i] for i in range(start, end)]


def _resolve_paragraph(doc, loc):
    pi = loc["paragraph_index"]
    if pi < 0 or pi >= len(doc.paragraphs):
        raise LocatorResolutionError(
            f"paragraph_index {pi} out of range (doc has {len(doc.paragraphs)} paragraphs)."
        )
    return doc.paragraphs[pi]


def _resolve_paragraph_range(doc, loc):
    start = loc.get("start_index", loc.get("start"))
    end = loc.get("end_index", loc.get("end"))
    if start < 0 or start >= len(doc.paragraphs):
        raise LocatorResolutionError(
            f"start_index {start} out of range (doc has {len(doc.paragraphs)} paragraphs)."
        )
    if end < start:
        raise LocatorResolutionError("end_index must be greater than or equal to start_index.")
    end = min(end, len(doc.paragraphs) - 1)
    return [doc.paragraphs[i] for i in range(start, end + 1)]


def _resolve_paragraph_prefix(doc, loc):
    prefix = str(loc.get("prefix") or "").strip().lower()
    start, end = loc.get("search_range") or [0, len(doc.paragraphs) - 1]
    end = min(end, len(doc.paragraphs) - 1)
    for idx in range(max(0, start), end + 1):
        text = doc.paragraphs[idx].text.strip().lower()
        if text.startswith(prefix.lower()):
            return doc.paragraphs[idx]
    raise LocatorResolutionError(f"No paragraph starting with prefix '{loc.get('prefix')}' found.")


def _resolve_header_match(doc, loc):
    import re
    pattern = loc["header_text"]
    for para in doc.paragraphs:
        if re.search(pattern, para.text, re.IGNORECASE):
            return para
    # Also search table cells.
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    if re.search(pattern, para.text, re.IGNORECASE):
                        return para
    raise LocatorResolutionError(
        f"No paragraph matching header_text '{pattern}' found."
    )


# ---------------------------------------------------------------------------
# Profile-vs-document validation
# ---------------------------------------------------------------------------

def validate_profile_against_doc(
    doc: Document,
    profile: Dict[str, Any],
) -> List[str]:
    """Check that every locator in *profile* resolves against *doc*.

    Returns a list of error strings.  Empty means all locators resolve.
    """
    errors: List[str] = []
    for sec in profile.get("sections", []):
        loc = sec.get("locator")
        if loc is None:
            continue
        try:
            resolve_locator(doc, loc)
        except LocatorResolutionError as exc:
            errors.append(f"Section '{sec.get('id')}': {exc}")
    return errors
