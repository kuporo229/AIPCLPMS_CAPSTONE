"""
Run-aware DOCX cell/paragraph writer.

Writes text into DOCX elements while preserving the original run-level
and paragraph-level formatting (font name, size, bold, italic, color,
alignment, spacing, etc.).  Only the *text* content changes; every other
property is cloned from a "style donor" run already present in the target
element.

Public API
----------
write_paragraph(paragraph, text)
    Replace the text of an existing paragraph, preserving formatting.

write_cell(cell, text)
    Replace the text of all paragraphs in a table cell.

write_table_kv_row(row, mapping)
    Write a {col_index: text} mapping into specific cells of a table row.

apply_profile(doc, profile, content)
    Orchestrator – given a Document, a template_profile dict, and
    generated content dict, resolve locators and fill every target element.
"""

import copy
import logging
import re
from docx.oxml.ns import qn

logger = logging.getLogger(__name__)

# Module-level fallback run properties for empty target elements.
# Set by ``set_fallback_font(doc)`` before writing.
_fallback_rPr = None

_METADATA_LABEL_ALIASES = {
    'course_code': ['course code', 'course number', 'course no'],
    'course_title': ['course title', 'descriptive title'],
    'course_description': ['course description', 'description'],
    'service_learning_component': ['service-learning component', 'service learning component'],
    'target_sdgs_display': ['target sdg', 'target sdgs', 'sdg'],
    'type_of_course': ['type of course', 'course type'],
    'units_display': ['units', 'unit'],
    'credit_display': ['credit', 'credits'],
    'contact_hours_display': ['contact hours per week', 'contact hours', 'class hours', 'hours per week'],
    'contact_hours_per_week': ['contact hours per week', 'contact hours', 'class hours', 'hours per week'],
    'pre_requisite': ['pre-requisite', 'prerequisite', 'pre requisite'],
    'co_requisite': ['co-requisite', 'corequisite', 'co requisite'],
    'class_schedule': ['class schedule', 'schedule'],
    'room_assignment': ['room assignment', 'room'],
}


def set_fallback_font(doc):
    """Detect the document's dominant font and store as module-level fallback."""
    global _fallback_rPr
    _fallback_rPr = _make_fallback_rPr(doc)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _clone_run_props(source_run):
    """Return a deep copy of the <w:rPr> element from *source_run*, or None."""
    rPr = source_run._element.find(qn('w:rPr'))
    if rPr is not None:
        return copy.deepcopy(rPr)
    return None


def _detect_document_font(doc):
    """Detect the dominant font name from the document's Normal style or first non-empty paragraph."""
    try:
        normal = doc.styles['Normal']
        if normal.font and normal.font.name:
            return normal.font.name
    except Exception:
        pass
    for para in doc.paragraphs:
        for run in para.runs:
            if run.font.name:
                return run.font.name
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    for run in para.runs:
                        if run.font.name:
                            return run.font.name
    return None


def _make_fallback_rPr(doc):
    """Build a minimal <w:rPr> element from the document's detected font.

    Used as a donor when the target cell/paragraph has no existing runs.
    """
    font_name = _detect_document_font(doc)
    if not font_name:
        return None
    from lxml import etree
    rPr = etree.SubElement(etree.Element(qn('w:rPr')), qn('w:rFonts'))
    rPr.set(qn('w:ascii'), font_name)
    rPr.set(qn('w:hAnsi'), font_name)
    rPr.set(qn('w:eastAsia'), font_name)
    rPr.set(qn('w:cs'), font_name)
    return rPr


def _clear_cell_images(cell):
    """Remove inline drawings/pictures from all paragraphs in a cell."""
    for para in cell.paragraphs:
        for run in para.runs:
            r_element = run._r
            for child in list(r_element):
                if "drawing" in child.tag or "pict" in child.tag:
                    r_element.remove(child)


def _norm_label(value):
    return re.sub(r'[^a-z0-9]+', ' ', str(value or '').replace('\xa0', ' ').lower()).strip()


def _metadata_aliases(field_name, label):
    aliases = list(_METADATA_LABEL_ALIASES.get(str(field_name or ''), []))
    if label:
        aliases.append(str(label))
    return [_norm_label(alias) for alias in aliases if _norm_label(alias)]


def _is_metadata_delimiter(value):
    return str(value or '').replace('\xa0', ' ').strip() in {'', ':', '-', '–', '—'}


def _looks_like_metadata_label_cell(cell_text, field_name, label):
    normalized = _norm_label(cell_text)
    if not normalized:
        return False
    raw = str(cell_text or '')
    for alias in _metadata_aliases(field_name, label):
        if normalized == alias:
            return True
        if ':' in raw and normalized.startswith(f'{alias} '):
            return True
    return False


def _find_adjacent_metadata_value_cell(doc, target, locator, field):
    """Return the value cell beside a mislabeled metadata locator when possible.

    Some generated/older template profiles point metadata fields at the left
    label cell (e.g. "Course Code:") instead of the wide value cell. Writing a
    long course title/description there collapses the text into a narrow column.
    This guard keeps the profile dynamic while correcting that common locator
    slip at render time.
    """
    if not isinstance(locator, dict) or locator.get('type') != 'table_cell':
        return target
    try:
        table = doc.tables[int(locator.get('table_index'))]
        row = table.rows[int(locator.get('row'))]
        col_idx = int(locator.get('col'))
    except (TypeError, ValueError, IndexError):
        return target
    cells = row.cells
    if col_idx < 0 or col_idx >= len(cells):
        return target
    current = cells[col_idx]
    if current._tc is not getattr(target, '_tc', None):
        return target
    field_name = field.get('field') if isinstance(field, dict) else ''
    label = field.get('label') if isinstance(field, dict) else ''
    if not _looks_like_metadata_label_cell(current.text, field_name, label):
        return target

    previous_tc = current._tc
    saw_separator = False
    for candidate in cells[col_idx + 1:]:
        if candidate._tc is previous_tc:
            continue
        previous_tc = candidate._tc
        candidate_text = candidate.text
        if _is_metadata_delimiter(candidate_text):
            if saw_separator:
                return candidate
            saw_separator = True
            continue
        if _looks_like_metadata_label_cell(candidate_text, field_name, label):
            continue
        return candidate

    previous_tc = current._tc
    for candidate in cells[col_idx + 1:]:
        if candidate._tc is previous_tc:
            continue
        previous_tc = candidate._tc
        return candidate
    return target


def _clone_paragraph_props(paragraph):
    """Return a deep copy of the <w:pPr> element from *paragraph*, or None."""
    pPr = paragraph._element.find(qn('w:pPr'))
    if pPr is not None:
        return copy.deepcopy(pPr)
    return None


def _apply_run_props(run, rPr_copy):
    """Attach a previously cloned <w:rPr> to *run*, replacing any existing."""
    existing = run._element.find(qn('w:rPr'))
    if existing is not None:
        run._element.remove(existing)
    if rPr_copy is not None:
        run._element.insert(0, copy.deepcopy(rPr_copy))


def _apply_paragraph_props(paragraph, pPr_copy):
    """Attach a previously cloned <w:pPr> to *paragraph*."""
    existing = paragraph._element.find(qn('w:pPr'))
    if existing is not None:
        paragraph._element.remove(existing)
    if pPr_copy is not None:
        paragraph._element.insert(0, copy.deepcopy(pPr_copy))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_paragraph(paragraph, text):
    """Replace *paragraph*'s text with *text*, preserving formatting.

    Formatting is cloned from the first run already present.  If the
    paragraph has no runs (empty cell), the text is written without any
    explicit run formatting — the inherited style will apply.

    Multi-line *text* (containing ``\\n``) produces multiple runs inside the
    same paragraph separated by ``<w:br/>`` elements, keeping all content
    in a single ``<w:p>`` to preserve paragraph-level spacing.  Use
    :func:`write_cell` when each line should be its own paragraph.
    """
    # Capture the style donor from the first existing run.
    donor_rPr = None
    if paragraph.runs:
        donor_rPr = _clone_run_props(paragraph.runs[0])
    elif _fallback_rPr is not None:
        donor_rPr = copy.deepcopy(_fallback_rPr)

    # Capture paragraph-level properties.
    pPr_copy = _clone_paragraph_props(paragraph)

    # Clear and rebuild.
    paragraph.clear()
    if pPr_copy is not None:
        numPr = pPr_copy.find(qn('w:numPr'))
        if numPr is not None:
            pPr_copy.remove(numPr)
    _apply_paragraph_props(paragraph, pPr_copy)

    lines = str(text).split('\n')
    for idx, line in enumerate(lines):
        run = paragraph.add_run(line)
        _apply_run_props(run, donor_rPr)
        if idx < len(lines) - 1:
            # Insert a line break element after this run.
            br = run._element.makeelement(qn('w:br'), {})
            run._element.append(br)


def _is_subheader_line(line: str) -> bool:
    """True when *line* looks like an inline sub-header label inside a cell.

    Sub-headers are short, end with a colon, and are not bullet points.
    They are rendered bold to preserve the visual emphasis present in many
    CLP templates (e.g. "Cognitive:", "Lecture:", "Assessment:").
    """
    stripped = line.strip()
    return (
        stripped.endswith(':')
        and len(stripped) <= 60
        and not stripped.startswith('-')
        and not stripped.startswith('\u2022')
        and not stripped.startswith('*')
    )


def write_cell(cell, text):
    """Replace all text in *cell* with *text*, preserving formatting.

    Each line of *text* becomes a separate paragraph.  The first existing
    paragraph's run formatting is used as the style donor for all new
    paragraphs.  Lines that look like sub-header labels (short, ending with
    ``:``, not bullets) are rendered bold.

    Inline images (digital signatures, decorations) are stripped before
    writing so they do not persist in the final document.
    """
    # Strip any inline images before rebuilding content.
    _clear_cell_images(cell)
    # Harvest donors from existing paragraphs/runs so cells with internal
    # list-like architecture keep their per-paragraph formatting.
    donor_rPr = None
    donor_pPr = None
    paragraph_donors = []
    for para in cell.paragraphs:
        pPr = _clone_paragraph_props(para)
        rPr = None
        for run in para.runs:
            rPr = _clone_run_props(run)
            if rPr is not None:
                break
        paragraph_donors.append((pPr, rPr))
        if donor_pPr is None:
            donor_pPr = pPr
        if donor_rPr is None:
            donor_rPr = rPr

    # Fallback font for cells with no existing runs.
    if donor_rPr is None and _fallback_rPr is not None:
        donor_rPr = copy.deepcopy(_fallback_rPr)

    lines = str(text).split('\n')

    existing = list(cell.paragraphs)
    while len(existing) < len(lines):
        existing.append(cell.add_paragraph())
    for para in existing[len(lines):]:
        p_elem = para._element
        p_elem.getparent().remove(p_elem)

    for idx, line in enumerate(lines):
        para = existing[idx]
        pPr, rPr = paragraph_donors[idx] if idx < len(paragraph_donors) else (donor_pPr, donor_rPr)
        para.clear()
        pPr_to_apply = pPr if pPr is not None else donor_pPr
        if pPr_to_apply is not None:
            numPr = pPr_to_apply.find(qn('w:numPr'))
            if numPr is not None:
                pPr_to_apply.remove(numPr)
        _apply_paragraph_props(para, pPr_to_apply)
        run = para.add_run(line)
        _apply_run_props(run, rPr if rPr is not None else donor_rPr)
        if _is_subheader_line(line):
            run.bold = True


def write_table_kv_row(row, mapping):
    """Write text into specific columns of a table *row*.

    Parameters
    ----------
    row : docx.table._Row
        The target row.
    mapping : dict[int, str]
        ``{column_index: text}`` pairs.  Column indices are 0-based.
    """
    cells = row.cells
    for col_idx, text in mapping.items():
        if 0 <= col_idx < len(cells):
            write_cell(cells[col_idx], str(text))


def _freeze_locator_value(value):
    if isinstance(value, dict):
        return tuple(sorted((key, _freeze_locator_value(val)) for key, val in value.items()))
    if isinstance(value, list):
        return tuple(_freeze_locator_value(item) for item in value)
    return value


def _locator_signature(locator):
    if not isinstance(locator, dict):
        return ()
    return _freeze_locator_value(locator)


def _metadata_fields_to_apply(profile):
    fields = [
        field
        for field in (profile.get('metadata_fields') or [])
        if isinstance(field, dict)
    ]
    prefix_fields = {
        field.get('field')
        for field in fields
        if isinstance(field.get('locator'), dict)
        and field.get('locator', {}).get('type') == 'paragraph_prefix'
    }
    selected = []
    seen = set()
    for field in fields:
        field_name = field.get('field')
        loc = field.get('locator') if isinstance(field.get('locator'), dict) else {}
        if not field_name or not loc:
            continue
        # Paragraph-prefix locators preserve labels such as "Course Number:".
        # Older plain paragraph locators for the same field would erase them.
        if loc.get('type') == 'paragraph' and field_name in prefix_fields:
            continue
        signature = (field_name, _locator_signature(loc))
        if signature in seen:
            continue
        seen.add(signature)
        selected.append(field)
    return selected


def _section_table_region(section):
    loc = section.get('locator') if isinstance(section, dict) else {}
    if not isinstance(loc, dict) or loc.get('type') != 'table_region':
        return None
    try:
        table_index = int(loc.get('table_index'))
        start_row = int(loc.get('start_row') or 0)
        end_row = int(loc.get('end_row') if loc.get('end_row') is not None else start_row)
    except (TypeError, ValueError):
        return None
    return table_index, start_row, end_row


def _key_value_section_is_metadata_backed(profile, section):
    region = _section_table_region(section)
    if not region:
        return False
    table_index, start_row, end_row = region
    for field in _metadata_fields_to_apply(profile):
        loc = field.get('locator') if isinstance(field.get('locator'), dict) else {}
        if loc.get('type') != 'table_cell':
            continue
        try:
            field_table = int(loc.get('table_index'))
            field_row = int(loc.get('row'))
        except (TypeError, ValueError):
            continue
        if field_table == table_index and start_row <= field_row <= end_row:
            return True
    return False


# ---------------------------------------------------------------------------
# Profile orchestrator (skeleton – Phase 2)
# ---------------------------------------------------------------------------

def apply_profile(doc, profile, content):
    """Write *content* into *doc* according to *profile* locators.

    Parameters
    ----------
    doc : docx.Document
        The opened DOCX document (in-memory).
    profile : dict
        A validated ``template_profile`` dict (see template_profile.py).
    content : dict
        Generated content keyed by section identifiers matching the
        profile's ``sections[].id``.

        Content value types supported per section ``content_type``:

        - ``table_cell`` / ``paragraph``: a plain string.
        - ``table_region``: a list of dicts, each mapping column index (int)
          to text — one dict per row.  e.g.
          ``[{0: "Week 1", 1: "Topic…"}, …]``
        - ``key_value``: a dict ``{field_name: text}`` matched against
          ``metadata_fields``.

    Returns
    -------
    doc : docx.Document
        The same document object, mutated in-place.
    results : dict
        ``{section_id: "ok"|<error string>}`` for each attempted section.
    """
    from app.services.template_profile import resolve_locator, LocatorResolutionError

    set_fallback_font(doc)

    results = {}

    # ── Handle metadata_fields (key_value writes) ──
    meta_content = content.get('_metadata', {})
    if meta_content and isinstance(meta_content, dict):
        for mf in _metadata_fields_to_apply(profile):
            field_name = mf.get('field')
            if field_name not in meta_content:
                continue
            result_key = f"meta:{field_name}"
            loc = mf.get('locator')
            if not loc:
                continue
            try:
                target = resolve_locator(doc, loc)
                if loc.get('type') == 'paragraph_prefix':
                    _write_paragraph_prefix(target, loc.get('prefix') or mf.get('label') or field_name, str(meta_content[field_name]))
                else:
                    target = _find_adjacent_metadata_value_cell(doc, target, loc, mf)
                    _write_to_target(target, str(meta_content[field_name]), loc.get('type'))
                results[result_key] = 'ok'
            except (LocatorResolutionError, Exception) as exc:
                if results.get(result_key) != 'ok':
                    results[result_key] = str(exc)
                logger.warning("apply_profile: metadata field '%s' failed: %s", field_name, exc)

    # ── Handle sections ──
    for section in profile.get('sections', []):
        sec_id = section.get('id')
        if sec_id not in content:
            continue
        loc = section.get('locator')
        if not loc:
            results[sec_id] = 'no locator'
            continue
        sec_content = content[sec_id]
        content_type = section.get('content_type', loc.get('type', ''))
        if content_type == 'key_value' and _key_value_section_is_metadata_backed(profile, section):
            results[sec_id] = 'ok'
            continue

        try:
            target = resolve_locator(doc, loc)
            _apply_section_content(target, sec_content, content_type, loc, section)
            results[sec_id] = 'ok'
        except (LocatorResolutionError, Exception) as exc:
            results[sec_id] = str(exc)
            logger.warning("apply_profile: section '%s' failed: %s", sec_id, exc)

    return doc, results


def _write_to_target(target, text, loc_type):
    """Write plain text into a resolved target element."""
    from docx.text.paragraph import Paragraph
    from docx.table import _Cell

    if isinstance(target, _Cell):
        write_cell(target, text)
    elif isinstance(target, Paragraph):
        write_paragraph(target, text)
    else:
        # List of rows/paragraphs — write into the first target as a safe fallback.
        if isinstance(target, list) and len(target) > 0:
            first = target[0]
            if isinstance(first, Paragraph):
                write_paragraph(first, text)
            elif hasattr(first, 'cells'):
                cells = first.cells
                if cells:
                    write_cell(cells[0], text)


def _write_paragraph_prefix(paragraph, prefix, value):
    """Write a prefix-style metadata paragraph while keeping the label."""
    prefix = str(prefix or "").strip()
    current = paragraph.text or ""
    pattern = re.compile(rf"^({re.escape(prefix)}\s*:\s*)", re.IGNORECASE) if prefix else None
    match = pattern.match(current) if pattern else None
    text = f"{match.group(1)}{value}" if match else (f"{prefix}: {value}" if prefix else str(value))
    write_paragraph(paragraph, text)


def _apply_section_content(target, sec_content, content_type, loc, section=None):
    """Dispatch section content to the appropriate writer."""
    from docx.text.paragraph import Paragraph
    from docx.table import _Cell

    if content_type in ('table_cell', 'paragraph'):
        # Simple text replacement.
        _write_to_target(target, _scalar_section_text(sec_content, section), content_type)

    elif content_type == 'paragraph_range':
        text = _scalar_section_text(sec_content, section)
        if isinstance(target, list) and target:
            write_paragraph(target[0], text)
            for paragraph in target[1:]:
                write_paragraph(paragraph, "")
        else:
            _write_to_target(target, text, content_type)

    elif content_type == 'table_region':
        # sec_content should be a list of row-dicts: [{col: text, …}, …]
        if not isinstance(sec_content, list):
            _write_to_target(target, _scalar_section_text(sec_content, section), 'table_cell')
            return
        all_rows = target if isinstance(target, list) else [target]
        _restore_static_skipped_rows(all_rows, section=section, loc=loc)
        rows = _select_data_rows(all_rows, sec_content, section=section, loc=loc)
        for row_idx, row_data in enumerate(sec_content):
            if row_idx >= len(rows):
                break
            if isinstance(row_data, dict):
                weekly_fields = row_data.get('__weekly_logical_fields__')
                if weekly_fields and _looks_like_weekly_section(section):
                    _write_weekly_row_by_segments(rows[row_idx], weekly_fields)
                    continue
                # Keys may be int col indices or string col indices.
                mapping = {int(k): str(v) for k, v in row_data.items() if str(k).lstrip('-').isdigit()}
                write_table_kv_row(rows[row_idx], mapping)
            elif isinstance(row_data, str):
                # Fallback: write into first cell.
                cells = rows[row_idx].cells
                if cells:
                    write_cell(cells[0], row_data)

    elif content_type == 'key_value':
        # sec_content is {field: text}, matched by metadata_fields.
        # Already handled above via _metadata, but allow section-level too.
        if isinstance(sec_content, dict):
            if isinstance(target, list):
                # table_region kv — write each field into rows by position.
                for row_idx, (field, text) in enumerate(sec_content.items()):
                    if row_idx >= len(target):
                        break
                    cells = target[row_idx].cells
                    if len(cells) > 1:
                        write_cell(cells[1], str(text))
            elif isinstance(target, _Cell):
                write_cell(target, '\n'.join(str(v) for v in sec_content.values()))
        else:
            _write_to_target(target, str(sec_content), 'table_cell')

    else:
        # Unknown content_type — best effort.
        _write_to_target(target, _scalar_section_text(sec_content, section), content_type)


def _scalar_section_text(sec_content, section=None):
    """Return a safe scalar string for single-cell/paragraph sections."""
    if isinstance(sec_content, dict):
        sec_id = str((section or {}).get('id') or '').lower()
        label = str((section or {}).get('label') or '').lower()
        haystack = f'{sec_id} {label}'
        for key, value in sec_content.items():
            key_l = str(key).lower()
            if key_l in haystack:
                return str(value)
        preferred = [
            'prepared_by_name', 'prepared_by_position', 'reviewed_by_name',
            'reviewed_by_position', 'endorsed_by_name', 'endorsed_by_position',
            'approved_by_name', 'approved_by_position', 'date_submitted',
            'date_reviewed',
        ]
        values = [str(sec_content.get(key) or '').strip() for key in preferred if sec_content.get(key)]
        return '\n'.join(values)
    return str(sec_content)


def _restore_static_skipped_rows(rows, section=None, loc=None):
    """Restore non-writable rows, such as exam separators, from profile scan data."""
    if not rows:
        return
    section = section if isinstance(section, dict) else {}
    loc = loc if isinstance(loc, dict) else {}
    skipped_rows = section.get('skipped_rows') if isinstance(section.get('skipped_rows'), list) else []
    if not skipped_rows:
        skipped_rows = loc.get('skipped_rows') if isinstance(loc.get('skipped_rows'), list) else []
    if not skipped_rows:
        return

    start_row = int(loc.get('start_row') or 0)
    for skipped in skipped_rows:
        if not isinstance(skipped, dict):
            continue
        try:
            source_row = int(skipped.get('row'))
        except (TypeError, ValueError):
            continue
        offset = source_row - start_row
        if offset < 0 or offset >= len(rows):
            continue
        source_preview = skipped.get('source_preview') if isinstance(skipped.get('source_preview'), list) else []
        mapping = {
            idx: str(value)
            for idx, value in enumerate(source_preview)
            if str(value or '').strip()
        }
        if not mapping and str(skipped.get('label') or '').strip():
            mapping = {0: str(skipped.get('label')).strip()}
        if mapping:
            write_table_kv_row(rows[offset], mapping)


def _select_data_rows(rows, sec_content, section=None, loc=None):
    """Filter header/separator rows out of a table_region before writing."""
    if not rows:
        return rows
    loc = loc or {}
    writable_rows = []
    if isinstance(section, dict):
        writable_rows = section.get('writable_rows') or section.get('row_offsets') or []
    if not writable_rows and isinstance(loc, dict):
        writable_rows = loc.get('writable_rows') or []
    if writable_rows:
        start_row = int(loc.get('start_row') or 0) if isinstance(loc, dict) else 0
        selected = []
        for item in writable_rows:
            try:
                row_number = int(item)
            except (TypeError, ValueError):
                continue
            offset = row_number - start_row
            if 0 <= offset < len(rows):
                selected.append(rows[offset])
        if selected:
            return selected
    if _looks_like_clo_content(sec_content):
        filtered = [row for row in rows if _is_clo_data_row(row)]
        return filtered or rows
    if _looks_like_weekly_content(sec_content):
        filtered = [row for row in rows if _is_weekly_data_row(row)]
        return filtered or rows
    return rows


def _looks_like_clo_content(sec_content):
    for row in sec_content or []:
        if not isinstance(row, dict):
            continue
        values = ' '.join(str(v) for v in row.values()).upper()
        if 'CLO' in values or 'PLO' in values or 'PQF' in values or 'AQRF' in values:
            return True
    return False


def _looks_like_weekly_content(sec_content):
    for row in sec_content or []:
        if not isinstance(row, dict):
            continue
        values = ' '.join(str(v) for v in row.values()).upper()
        if 'WEEK' in values or 'AT THE END OF THE WEEK' in values or 'LECTURE:' in values:
            return True
    return False


def _looks_like_weekly_section(section):
    if not isinstance(section, dict):
        return False
    text = f"{section.get('id', '')} {section.get('label', '')}".lower()
    return "weekly" in text or "course outline" in text or "learning plan" in text


def _row_cell_segments(row):
    """Return the leftmost cell for each contiguous merged-cell segment."""
    segments = []
    previous_tc = None
    for cell in row.cells:
        current_tc = cell._tc
        if current_tc is previous_tc:
            continue
        segments.append(cell)
        previous_tc = current_tc
    return segments


def _write_weekly_row_by_segments(row, values):
    """Write weekly outline fields by logical merged-cell segments.

    Some CLP templates change horizontal merge spans per weekly row, so a
    single fixed column index can miss Assessment/Resources cells.  The source
    row layout already carries the desired architecture; writing one value per
    contiguous cell segment preserves that architecture and clears stale text.
    """
    fields = [str(value or "") for value in values]
    segments = _row_cell_segments(row)
    if len(segments) < len(fields):
        for idx, value in enumerate(fields[:len(segments)]):
            write_cell(segments[idx], value)
        return
    for idx, value in enumerate(fields):
        write_cell(segments[idx], value)


def _row_texts(row):
    return [cell.text.strip() for cell in row.cells]


def _is_repeated_row(row):
    values = [' '.join(text.upper().replace('\xa0', ' ').split()) for text in _row_texts(row) if text]
    return bool(values) and len(set(values)) == 1


def _is_clo_data_row(row):
    values = _row_texts(row)
    if not values:
        return False
    first = ' '.join(values[0].upper().replace('\xa0', ' ').split())
    if _is_repeated_row(row) and first in {'COGNITIVE', 'AFFECTIVE', 'PSYCHOMOTOR'}:
        return False
    return 'CLO' in first or bool(values[1].strip() if len(values) > 1 else '')


def _is_weekly_data_row(row):
    values = _row_texts(row)
    if not values:
        return False
    text = ' '.join(values).upper().replace('\xa0', ' ')
    text = ' '.join(text.split())
    first = values[0].upper().replace('\xa0', ' ')
    first = ' '.join(first.split())
    if 'TIME / FRAME' in text or 'TIME FRAME' in text:
        return False
    if _is_repeated_row(row) and 'EXAMINATION' in first:
        return False
    return bool(first.strip())
