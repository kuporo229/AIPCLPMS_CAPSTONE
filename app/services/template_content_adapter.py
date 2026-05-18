"""
Content adapter: transforms beta CLP content into profile-shaped content
for ``apply_profile``.

The adapter reads the ``template_profile`` to understand what sections and
metadata fields the template expects, then maps the beta content (CLOs,
weekly outline, signatories, metadata) into the dict structure that
``apply_profile`` consumes.

This module has **no Flask or AI dependencies** so it can be tested in
isolation.

Public API
----------
build_profile_content(content, profile, user_profile=None)
    Returns ``{section_id: value, '_metadata': {field: value}}`` ready
    for ``apply_profile(doc, profile, content)``.
"""

import re
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Text formatting helpers (mirrors copilot_beta_service patterns)
# ---------------------------------------------------------------------------

def _csv_or_lines_to_list(value):
    """Normalize a value to a list of non-empty stripped strings."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [v.strip() for v in value.split('\n') if v.strip()]
    return []


def _list_lines(items, marker='dash'):
    cleaned = _csv_or_lines_to_list(items)
    if marker == 'plain':
        return '\n'.join(cleaned)
    return '\n'.join(f'- {item}' for item in cleaned)


def _bullet_lines(items):
    return _list_lines(items, marker='dash')


def _weekly_field_hint(format_hints, field):
    fields = format_hints.get('fields') if isinstance(format_hints, dict) else {}
    hint = fields.get(field) if isinstance(fields, dict) else {}
    return hint if isinstance(hint, dict) else {}


def _format_ilo_block(row, hint=None):
    """Format ILO block.  When the template uses a flat format (numbered items,
    lead-in + semicolon list, plain labels), the AI returns a single string
    for the whole field.  In that case output it directly.  Otherwise fall
    through to the categorized {Cognitive/Affective/Psychomotor} path.

    When format_hints has boilerplate_lead_in, use it instead of the AI-generated
    lead_in — the template's original boilerplate is authoritative."""
    ilo = row.get('intended_learning_outcomes', {})
    hint = hint or {}
    bp_lead_in = hint.get('boilerplate_lead_in')
    fallback_lead = bp_lead_in or 'At the end of the week, students should have the ability to:'

    # If the AI returned a flat string (or the hint says flat_lines),
    # use the value directly without category labels.
    if isinstance(ilo, str):
        return ilo.strip()
    if hint.get('output_style') == 'flat_lines' or isinstance(ilo, list):
        if isinstance(ilo, list):
            return _list_lines(ilo, marker=hint.get('list_marker') or 'dash')
        if isinstance(ilo, dict):
            # Dict arrived but template says flat — extract values from sub-fields.
            values = []
            for key, val in ilo.items():
                if key == 'lead_in':
                    continue
                values.extend(_csv_or_lines_to_list(val))
            if values:
                return _list_lines(values, marker=hint.get('list_marker') or 'plain')
            return ''
        return str(ilo).strip()

    # Categorized path — the AI returned a dict with lead_in + domain keys.
    if not isinstance(ilo, dict):
        return str(ilo).strip() if ilo else ''

    lines = [str(ilo.get('lead_in') or fallback_lead).strip()]
    # Check if the dict has domain-specific keys
    categorized_keys = [k for k in ilo if k.lower() in ('cognitive', 'affective', 'psychomotor')]
    if categorized_keys:
        for key_display in ('Cognitive', 'Affective', 'Psychomotor'):
            key_lower = key_display.lower()
            values = _csv_or_lines_to_list(ilo.get(key_lower, []))
            if values:
                lines.append(f'{key_display}:')
                lines.extend(f'- {item}' for item in values)
    else:
        # Categorized but with custom category labels — just flatten
        for key, values in ilo.items():
            if key == 'lead_in':
                continue
            vals = _csv_or_lines_to_list(values)
            if vals:
                lines.append(f'{key}:')
                lines.extend(f'- {item}' for item in vals)
    return '\n'.join(lines)


def _format_tla_block(row, hint=None):
    tla = row.get('teaching_learning_activities', {})
    hint = hint or {}
    marker = hint.get('list_marker') or 'dash'

    # The AI may return a flat string or array when the template uses
    # flat labels (e.g. "Effective Questioning", "Journal Making").
    if isinstance(tla, str):
        return tla.strip()
    if isinstance(tla, list):
        return _list_lines(tla, marker=marker)

    # Categorized path — the AI returned a dict with activity type keys.
    if not isinstance(tla, dict):
        return str(tla).strip() if tla else ''

    if hint.get('output_style') == 'flat_lines':
        values = []
        for key in tla:
            values.extend(_csv_or_lines_to_list(tla.get(key, [])))
        return _list_lines(values, marker=marker)
    lines = []
    for label, key in [('Lecture', 'lecture'), ('Practical session', 'practical_session'), ('Other', 'other')]:
        values = _csv_or_lines_to_list(tla.get(key, []))
        if not values:
            continue
        lines.append(f'{label}:')
        lines.extend(f'- {item}' for item in values)
    return '\n'.join(lines)


def _format_resources_block(row, hint=None):
    resources = row.get('learning_resources', {})
    hint = hint or {}
    marker = hint.get('list_marker') or 'dash'

    # The AI may return a flat string or array when the template uses
    # flat resource references (e.g. "Title. URL" on each line).
    if isinstance(resources, str):
        return resources.strip()
    if isinstance(resources, list):
        return _list_lines(resources, marker=marker)

    # Categorized path — the AI returned a dict with type keys.
    if not isinstance(resources, dict):
        return str(resources).strip() if resources else ''

    if hint.get('output_style') == 'flat_lines':
        values = []
        for key in resources:
            values.extend(_csv_or_lines_to_list(resources.get(key, [])))
        return _list_lines(values, marker=marker)
    lines = []
    for label, key in [('CLMS', 'clms'), ('Textbook', 'textbook'), ('Website', 'website'), ('Journal', 'journal'), ('Other', 'other')]:
        values = _csv_or_lines_to_list(resources.get(key, []))
        if not values:
            continue
        lines.append(f'{label}:')
        lines.extend(f'- {item}' for item in values)
    return '\n'.join(lines)


def _format_topics_block(row, hint=None):
    """Format topics for DOCX insertion. Handles both flat text (string/list)
    and the legacy categorized-dict format that AI might still produce."""
    topics = row.get('topics', [])
    hint = hint or {}
    marker = hint.get('list_marker') or 'dash'

    if isinstance(topics, str):
        return topics.strip()
    if isinstance(topics, list):
        return _list_lines(topics, marker=marker)
    if isinstance(topics, dict):
        # Legacy categorized dict — flatten all values to text.
        # Preserve key names as headings for structural clarity.
        lines = []
        for key in topics:
            values = _csv_or_lines_to_list(topics.get(key, []))
            if values:
                if len(topics) > 1:
                    lines.append(f"{key}:")
                for v in values:
                    lines.append(f"- {v}")
        return '\n'.join(lines)
    return str(topics).strip() if topics else ''


def _unique_keep_order(values):
    seen = set()
    output = []
    for v in values:
        v_str = str(v).strip()
        if v_str and v_str not in seen:
            seen.add(v_str)
            output.append(v_str)
    return output


def _alignment_label_matches(values, label, aliases=None):
    """Return True when a row's alignment values include a column label."""
    label_keys = {
        str(item or '').replace(' ', '').replace('-', '').lower()
        for item in [label] + (aliases or [])
        if str(item or '').strip()
    }
    if not label_keys or values in (None, ''):
        return False
    if isinstance(values, str):
        values = _csv_or_lines_to_list(values)
    elif not isinstance(values, list):
        values = [values]
    for value in values:
        if isinstance(value, dict):
            candidates = [value.get('code'), value.get('title'), value.get('label'), value.get('short_code')]
        else:
            candidates = [value]
        for candidate in candidates:
            value_key = str(candidate or '').replace(' ', '').replace('-', '').lower()
            if value_key and value_key in label_keys:
                return True
    return False


# ---------------------------------------------------------------------------
# Section-type builders
# ---------------------------------------------------------------------------

# Canonical column id (from generation_spec) → list of acceptable content row keys.
# The alignment_columns produced by template_generation_spec._alignment_columns()
# already uses canonical keys, but a small amount of legacy/alias tolerance makes
# the adapter robust against older content shapes.
_CLO_COLUMN_TO_CONTENT_KEYS = {
    'clo_code':                ('clo_code',),
    'clo_statement':           ('clo_statement', 'statement'),
    'domain':                  ('domain',),
    'aligned_plos':            ('aligned_plos',),
    'graduate_attributes':     ('graduate_attributes', 'sga'),
    'core_values':             ('core_values',),
    'pqf_level_6_alignment':   ('pqf_level_6_alignment', 'pqf'),
    'aqrf_level_6_alignment':  ('aqrf_level_6_alignment', 'aqrf'),
    'relevant_sdgs':           ('relevant_sdgs', 'sdg'),
}


def _build_clo_table_rows(content: Dict, profile: Dict, section: Optional[Dict] = None) -> List[Dict[int, str]]:
    """Build row-dicts for a CLO alignment table region.

    Each row maps column index → text.  The column mapping is driven by
    ``profile['alignment_columns']`` (or the section's ``columns``) so the
    content lands in the right physical columns of the template.

    The column ids produced by ``template_generation_spec._alignment_columns``
    are already canonical and match the content row keys; we look them up
    directly with a small alias fallback for legacy content shapes.
    """
    section = section or {}
    clo_rows = content.get('clo_alignment_table', [])
    group_id = section.get('id')
    if group_id and isinstance(content.get('clo_alignment_groups'), list):
        for group in content.get('clo_alignment_groups', []):
            if not isinstance(group, dict):
                continue
            if group.get('group_id') == group_id or group.get('id') == group_id:
                clo_rows = group.get('clo_alignment_table') or clo_rows
                break
    alignment_cols = section.get('columns') or profile.get('alignment_columns', {})

    # Build (col_id → col_index) preserving the canonical id from the spec.
    col_map: Dict[str, int] = {}
    for col_id, col_info in alignment_cols.items():
        if isinstance(col_info, dict):
            idx = col_info.get('col_index')
        elif isinstance(col_info, int):
            idx = col_info
        else:
            idx = None
        if isinstance(idx, int) and idx >= 0:
            col_map[col_id] = idx

    rows = []
    for row in clo_rows:
        row_data: Dict[int, str] = {}
        for col_id, col_idx in col_map.items():
            value = ''
            col_info = alignment_cols.get(col_id) if isinstance(alignment_cols, dict) else {}
            col_label = col_info.get('label') if isinstance(col_info, dict) else ''
            col_aliases = col_info.get('aliases') if isinstance(col_info, dict) else []
            # 1) Direct canonical match against the content row keys.
            content_keys = _CLO_COLUMN_TO_CONTENT_KEYS.get(col_id)
            if content_keys:
                for key in content_keys:
                    if key in row and row.get(key) not in (None, ''):
                        value = row.get(key)
                        break
            # 2) Checkbox-style alignment grids use one physical column per
            # outcome/framework label. For checkmark-style content, read from
            # the structured checkmark_alignments dict; for clo_based content,
            # heuristically match against text alignment lists.
            if value == '' and (
                col_id.startswith('program_outcome_')
                or col_id.startswith('institutional_outcome_')
                or col_id.startswith('core_value_')
            ):
                checkmark_data = row.get('checkmark_alignments') if isinstance(row.get('checkmark_alignments'), dict) else None
                if checkmark_data is not None:
                    # Checkmark-style: direct boolean lookup by column label.
                    if checkmark_data.get(col_label, False):
                        value = '✓'
                else:
                    # CLO-based style: heuristic matching from text alignment fields.
                    for source_key in (
                        'aligned_plos',
                        'program_outcomes',
                        'institutional_outcomes',
                        'core_values',
                    ):
                        if _alignment_label_matches(row.get(source_key), col_label, col_aliases):
                            value = '✓'
                            break
            # 3) Last-resort: identity lookup (handles future custom column ids).
            if value == '':
                value = row.get(col_id, '')
            if isinstance(value, list):
                value = _bullet_lines(value)
            row_data[col_idx] = str(value)
        rows.append(row_data)
    return rows


def _build_weekly_table_rows(content: Dict, profile: Dict, section: Optional[Dict] = None) -> List[Dict[int, str]]:
    """Build row-dicts for a weekly outline table region.

    The column mapping is inferred from the profile's section notes or
    alignment_columns, falling back to a sensible default column order.
    """
    weekly_rows = content.get('weekly_course_outline', [])
    section = section or {}
    generation_spec = profile.get('generation_spec') or profile.get('template_generation_spec') or {}
    weekly_spec = generation_spec.get('weekly_outline') if isinstance(generation_spec, dict) else {}
    format_hints = section.get('format_hints') if isinstance(section.get('format_hints'), dict) else {}
    if not format_hints and isinstance(weekly_spec, dict):
        format_hints = weekly_spec.get('format_hints') if isinstance(weekly_spec.get('format_hints'), dict) else {}
    columns = section.get('columns') if isinstance(section.get('columns'), dict) else {}
    col_map = {
        'time_frame_label': 0,
        'intended_learning_outcomes': 1,
        'topics': 2,
        'teaching_learning_activities': 3,
        'assessment': 4,
        'learning_resources': 5,
    }
    for field, info in columns.items():
        if isinstance(info, dict) and isinstance(info.get('col_index'), int):
            col_map[field] = info['col_index']
    rows = []
    for row in weekly_rows:
        logical_fields = [
            str(row.get('time_frame_label', '')),
            _format_ilo_block(row, _weekly_field_hint(format_hints, 'intended_learning_outcomes')),
            _format_topics_block(row, _weekly_field_hint(format_hints, 'topics')),
            _format_tla_block(row, _weekly_field_hint(format_hints, 'teaching_learning_activities')),
            _list_lines(row.get('assessment', []), marker=_weekly_field_hint(format_hints, 'assessment').get('list_marker') or 'dash'),
            _format_resources_block(row, _weekly_field_hint(format_hints, 'learning_resources')),
        ]
        row_data: Dict[int, str] = {
            col_map['time_frame_label']: logical_fields[0],
            col_map['intended_learning_outcomes']: logical_fields[1],
            col_map['topics']: logical_fields[2],
            col_map['teaching_learning_activities']: logical_fields[3],
            col_map['assessment']: logical_fields[4],
            col_map['learning_resources']: logical_fields[5],
        }
        row_data['__weekly_logical_fields__'] = logical_fields
        rows.append(row_data)
    return rows


def _normalize_alignment_key(value: Any) -> str:
    return re.sub(r'[^A-Z0-9]+', '', str(value or '').strip().upper())


def _build_program_institutional_rows(content: Dict, section: Dict) -> List[Dict[int, str]]:
    section = section or {}
    section_id = section.get('id')
    matrices = content.get('program_institutional_alignments') if isinstance(content.get('program_institutional_alignments'), dict) else {}
    matrix = matrices.get(section_id) if section_id else None
    if not isinstance(matrix, dict):
        return []
    row_keys = [
        _normalize_alignment_key(item)
        for item in (section.get('row_labels_normalized') or [])
        if _normalize_alignment_key(item)
    ]
    col_keys = [
        _normalize_alignment_key(item)
        for item in (section.get('column_labels_normalized') or [])
        if _normalize_alignment_key(item)
    ]
    col_positions = {}
    if isinstance(section.get('column_index_by_normalized'), dict):
        for key, value in section['column_index_by_normalized'].items():
            try:
                col_positions[_normalize_alignment_key(key)] = int(value)
            except (TypeError, ValueError):
                continue
    for col_key, col_index in zip(col_keys, section.get('alignment_column_indices') or []):
        try:
            col_positions.setdefault(col_key, int(col_index))
        except (TypeError, ValueError):
            continue

    rows = []
    for row_key in row_keys:
        source_row = matrix.get(row_key) if isinstance(matrix.get(row_key), dict) else {}
        row_data: Dict[int, str] = {}
        for col_key in col_keys:
            if col_key not in col_positions:
                continue
            value = source_row.get(col_key, '')
            row_data[col_positions[col_key]] = '✔' if value == '✔' else ''
        rows.append(row_data)
    return rows


def _build_references_text(content: Dict) -> str:
    """Collect all references from weekly outline into a single block."""
    all_websites = []
    all_textbooks = []
    all_journals = []
    all_other = []

    def extend_from_bucket(bucket):
        if not isinstance(bucket, dict):
            all_other.extend(_csv_or_lines_to_list(bucket))
            return
        for raw_key, raw_value in bucket.items():
            key = re.sub(r'[^a-z0-9]+', '_', str(raw_key or '').lower()).strip('_')
            values = _csv_or_lines_to_list(raw_value)
            if not values:
                continue
            if key in {'website', 'websites', 'web', 'online', 'online_resources', 'url', 'urls', 'link', 'links'}:
                all_websites.extend(values)
            elif key in {'textbook', 'textbooks', 'book', 'books', 'text'}:
                all_textbooks.extend(values)
            elif key in {'journal', 'journals', 'article', 'articles', 'research', 'research_articles'}:
                all_journals.extend(values)
            elif key not in {'clms', 'lms', 'module', 'modules'}:
                all_other.extend(values)

    extend_from_bucket(content.get('references', {}))
    for row in content.get('weekly_course_outline', []):
        extend_from_bucket(row.get('learning_resources', {}))

    sections = []
    if all_websites:
        sections.append('Websites:\n' + _bullet_lines(_unique_keep_order(all_websites)[:10]))
    if all_textbooks:
        sections.append('Textbooks:\n' + _bullet_lines(_unique_keep_order(all_textbooks)[:10]))
    if all_journals:
        sections.append('Journals:\n' + _bullet_lines(_unique_keep_order(all_journals)[:10]))
    if all_other:
        sections.append('Other:\n' + _bullet_lines(_unique_keep_order(all_other)[:10]))
    return '\n\n'.join(sections)


def _build_signatories_text(content: Dict, user_profile: Optional[Dict]) -> Dict[str, str]:
    """Build signatory key-value pairs."""
    signatories = content.get('signatories', {}) if isinstance(content.get('signatories'), dict) else {}
    up = user_profile or {}
    today = datetime.now().strftime('%B %d, %Y')

    prepared_name = signatories.get('prepared_by_name') or (
        f"{up.get('first_name', '')} {up.get('last_name', '')}" .strip()
    ) or 'Instructor'
    prepared_pos = signatories.get('prepared_by_position') or up.get('title') or 'Instructor'
    reviewed_name = signatories.get('reviewed_by_name', '')
    reviewed_pos = signatories.get('reviewed_by_position', 'Program Coordinator')
    endorsed_name = signatories.get('endorsed_by_name', '')
    endorsed_pos = signatories.get('endorsed_by_position', 'Dean')
    approved_name = signatories.get('approved_by_name', '')
    approved_pos = signatories.get('approved_by_position', 'Vice President')

    return {
        'prepared_by_name': prepared_name,
        'prepared_by_position': prepared_pos,
        'reviewed_by_name': reviewed_name,
        'reviewed_by_position': reviewed_pos,
        'endorsed_by_name': endorsed_name,
        'endorsed_by_position': endorsed_pos,
        'approved_by_name': approved_name,
        'approved_by_position': approved_pos,
        'date_submitted': today,
        'date_reviewed': today,
        # Revision/approval notation aliases used in the bottom signatory table.
        'last_revised_by_name': prepared_name,
        'last_revised_by_position': prepared_pos,
        'last_updated_by_name': prepared_name,
        'last_updated_by_position': prepared_pos,
        'final_reviewed_by_name': reviewed_name,
        'final_reviewed_by_position': reviewed_pos,
        'final_endorsed_by_name': endorsed_name,
        'final_endorsed_by_position': endorsed_pos,
        'final_approved_by_name': approved_name,
        'final_approved_by_position': approved_pos,
    }


# Keyword groups for detecting signatory row roles from label text.
# Checked in order; first match wins.  More specific phrases must come first.
_SIGNATORY_LABEL_TO_ROLE = [
    (('last revised', 'last revise'), 'last_revised_by_name', 'last_revised_by_position'),
    (('last updated', 'last update'), 'last_updated_by_name', 'last_updated_by_position'),
    (('final review', 'final reviewed'), 'final_reviewed_by_name', 'final_reviewed_by_position'),
    (('final endorse', 'final endorsed'), 'final_endorsed_by_name', 'final_endorsed_by_position'),
    (('final approved', 'final approve'), 'final_approved_by_name', 'final_approved_by_position'),
    (('prepared', 'instructor', 'teacher', 'faculty member'), 'prepared_by_name', 'prepared_by_position'),
    (('reviewed', 'coordinator', 'programme'), 'reviewed_by_name', 'reviewed_by_position'),
    (('endorsed', 'dean', 'chairperson', 'department head'), 'endorsed_by_name', 'endorsed_by_position'),
    (('approved', 'vice president', 'vp '), 'approved_by_name', 'approved_by_position'),
]


def _signatory_role_for_label(label_text: str) -> Optional[tuple]:
    """Return (name_field, position_field) for a row label, or None if unrecognised."""
    label_lower = label_text.lower().strip()
    for keywords, name_field, pos_field in _SIGNATORY_LABEL_TO_ROLE:
        if any(kw in label_lower for kw in keywords):
            return name_field, pos_field
    return None


def _build_signatory_region_rows(section: Dict, signatories: Dict[str, str]) -> List[Dict[int, str]]:
    """Build table-region row mappings for common CLP signatory tables.

    Uses detected column indices from the profile (name_col, pos_col, date_col)
    instead of hardcoded positions.
    """
    loc = section.get('locator') or {}
    labels = section.get('labels') or []
    columns = section.get('columns') or {}
    name_col = columns.get('name', 1)
    pos_col = columns.get('position', 2)
    date_col = columns.get('date')

    if labels:
        rows = []
        for label in labels:
            role = _signatory_role_for_label(label)
            if role is None:
                rows.append({})
                continue
            name_field, pos_field = role
            row = {
                name_col: signatories.get(name_field, ''),
                pos_col: signatories.get(pos_field, ''),
            }
            if date_col is not None:
                row[date_col] = signatories.get('date_submitted', '')
            rows.append(row)
        return ([{}] + rows) if loc.get('start_row') == 0 else rows

    # Fallback: standard four-role layout when no spec labels are available.
    rows = []
    for name_field, pos_field in [
        ('prepared_by_name', 'prepared_by_position'),
        ('reviewed_by_name', 'reviewed_by_position'),
        ('endorsed_by_name', 'endorsed_by_position'),
        ('approved_by_name', 'approved_by_position'),
    ]:
        row = {
            name_col: signatories.get(name_field, ''),
            pos_col: signatories.get(pos_field, ''),
        }
        if date_col is not None:
            row[date_col] = signatories.get('date_submitted', '')
        rows.append(row)
    return ([{}] + rows) if loc.get('start_row') == 0 else rows


def _build_consultation_rows(user_profile: Optional[Dict], section: Optional[Dict] = None) -> List[Dict[int, str]]:
    """Format consultation hours according to detected consultation columns."""
    if not user_profile:
        return []
    consultation = user_profile.get('consultation_hours', {})
    if not isinstance(consultation, dict):
        return []
    section = section or {}
    columns = section.get('columns') if isinstance(section.get('columns'), dict) else {}
    name_col = columns.get('name')
    day_col = columns.get('days')
    time_col = columns.get('time', 1)
    room_col = columns.get('room', 2)
    instructor_name = (
        f"{user_profile.get('first_name', '')} {user_profile.get('last_name', '')}".strip()
        or user_profile.get('name')
        or user_profile.get('username')
        or 'Instructor'
    )
    day_order = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday']
    rows = []
    for day in day_order:
        data = consultation.get(day, {})
        if isinstance(data, dict) and (data.get('time') or data.get('room')):
            row: Dict[int, str] = {}
            if isinstance(name_col, int):
                row[name_col] = instructor_name
                time_value = data.get('time', '')
                row[time_col] = f"{day.capitalize()} {time_value}".strip() if not isinstance(day_col, int) else time_value
            if isinstance(day_col, int):
                row[day_col] = day.capitalize()
            if not isinstance(name_col, int) and not isinstance(day_col, int):
                row[0] = day.capitalize()
            row[time_col] = row.get(time_col, data.get('time', ''))
            row[room_col] = data.get('room', '')
            rows.append(row)
    loc = section.get('locator') or {}
    return ([{}] + rows) if loc.get('start_row') == 0 else rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_profile_content(
    content: Dict[str, Any],
    profile: Dict[str, Any],
    user_profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Transform beta CLP content into a profile-shaped dict for ``apply_profile``.

    Parameters
    ----------
    content : dict
        The normalized beta CLP content (as stored in ``course_learning_plans.content``).
    profile : dict
        The validated ``template_profile`` dict from the teacher's profile.
    user_profile : dict, optional
        The teacher's user profile (for signatory defaults).

    Returns
    -------
    dict
        Keys are section IDs from the profile (matching ``sections[].id``)
        plus a special ``_metadata`` key for metadata field values.
    """
    result: Dict[str, Any] = {}
    metadata = content.get('metadata', {}) if isinstance(content.get('metadata'), dict) else {}
    signatories_map = _build_signatories_text(content, user_profile)

    # ── Build _metadata from metadata_fields in profile ──
    meta_out: Dict[str, str] = {}
    for mf in profile.get('metadata_fields', []):
        field = mf.get('field', '')
        # Try metadata first, then signatories.
        value = metadata.get(field) or signatories_map.get(field, '')
        meta_out[field] = str(value)
    result['_metadata'] = meta_out

    # ── Build section content ──
    for section in profile.get('sections', []):
        sec_id = section.get('id', '')
        content_type = section.get('content_type', '')
        sec_lower = sec_id.lower()

        # Match known section types by ID convention.
        if section.get('alignment_format') == 'program_institutional_checkmark' or _matches_any(sec_lower, ['program_institutional_alignment']):
            result[sec_id] = _build_program_institutional_rows(content, section)

        elif _matches_any(sec_lower, ['clo_table', 'clo_alignment', 'course_learning_outcomes', 'clo_group']):
            result[sec_id] = _build_clo_table_rows(content, profile, section)

        elif _matches_any(sec_lower, ['weekly', 'course_outline', 'weekly_outline', 'weekly_course_outline']):
            result[sec_id] = _build_weekly_table_rows(content, profile, section)

        elif _matches_any(sec_lower, ['reference', 'references', 'bibliography']):
            result[sec_id] = _build_references_text(content)

        elif _matches_any(sec_lower, ['signator', 'approval', 'prepared_by', 'reviewed_by', 'endorsed_by', 'approved_by', 'endorsement']):
            if sec_id in signatories_map:
                result[sec_id] = signatories_map.get(sec_id, '')
            elif content_type == 'table_region' or (section.get('locator') or {}).get('type') == 'table_region':
                result[sec_id] = _build_signatory_region_rows(section, signatories_map)
            else:
                result[sec_id] = signatories_map

        elif _matches_any(sec_lower, ['course_requirements', 'requirements']):
            result[sec_id] = (
                '- Complete all required lecture, laboratory, and guided learning activities\n'
                '- Submit the expected weekly outputs, assessments, and applied tasks\n'
                '- Participate in quizzes, consultations, and major examinations\n'
                '- Demonstrate course competencies through the final integrative outputs'
            )

        elif _matches_any(sec_lower, ['consultation', 'consultation_hours']):
            if content_type == 'table_region' or (section.get('locator') or {}).get('type') == 'table_region':
                result[sec_id] = _build_consultation_rows(user_profile, section)
            else:
                result[sec_id] = _build_consultation_text(user_profile)

        elif _matches_any(sec_lower, ['course_description', 'description']):
            result[sec_id] = metadata.get('course_description', '')

        elif _matches_any(sec_lower, ['target_sdg', 'sdg_display']):
            result[sec_id] = metadata.get('target_sdgs_display', '')

        elif content_type == 'key_value':
            # Generic key_value — populate from metadata + signatories.
            kv: Dict[str, str] = {}
            for key in list(metadata.keys()) + list(signatories_map.keys()):
                kv[key] = metadata.get(key) or signatories_map.get(key, '')
            result[sec_id] = kv

        # If nothing matched, leave the section out — apply_profile will skip it.

    return result


def _matches_any(text: str, patterns: List[str]) -> bool:
    """Check if text contains any of the patterns."""
    return any(p in text for p in patterns)


def validate_profile_coverage(
    content: Dict[str, Any],
    profile: Dict[str, Any],
    user_profile: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Check how well the beta content covers the template profile.

    Returns a list of human-readable gap warnings.  An empty list means
    full coverage.

    Parameters
    ----------
    content : dict
        Normalized beta CLP content.
    profile : dict
        Template profile dict (``profile_data['profile']``).
    user_profile : dict, optional
        Teacher's user profile.

    Returns
    -------
    list[str]
        Gap/warning messages.
    """
    built = build_profile_content(content, profile, user_profile=user_profile)
    warnings: List[str] = []
    metadata = content.get('metadata', {}) if isinstance(content.get('metadata'), dict) else {}

    # Check sections.
    for section in profile.get('sections', []):
        sec_id = section.get('id', '')
        label = section.get('label', sec_id)
        value = built.get(sec_id)
        if value is None:
            warnings.append(f'Section "{label}" ({sec_id}) has no matching content and will be skipped.')
        elif isinstance(value, str) and not value.strip():
            warnings.append(f'Section "{label}" ({sec_id}) is empty.')
        elif isinstance(value, list) and len(value) == 0:
            warnings.append(f'Section "{label}" ({sec_id}) has no rows.')
        elif isinstance(value, list):
            capacity = section.get('row_count')
            if not capacity and isinstance(section.get('locator'), dict):
                capacity = len(section.get('locator', {}).get('writable_rows') or [])
            try:
                capacity_int = int(capacity or 0)
            except (TypeError, ValueError):
                capacity_int = 0
            if capacity_int and len(value) > capacity_int:
                warnings.append(f'Section "{label}" ({sec_id}) has {len(value)} generated rows but only {capacity_int} detected writable template rows.')

    # Check metadata fields.
    meta = built.get('_metadata', {})
    for mf in profile.get('metadata_fields', []):
        field = mf.get('field', '')
        field_label = mf.get('label', field)
        value = meta.get(field, '')
        if not str(value).strip():
            warnings.append(f'Metadata field "{field_label}" ({field}) is empty.')

    # Check for multi-CLO-group fan-out.
    clo_group_sections = [
        s for s in profile.get('sections', [])
        if _matches_any(s.get('id', '').lower(), ['clo_table', 'clo_alignment', 'course_learning_outcomes', 'clo_group'])
    ]
    if len(clo_group_sections) > 1 and not isinstance(content.get('clo_alignment_groups'), list):
        warnings.append(
            f'{len(clo_group_sections)} CLO alignment sections detected in the profile but the content '
            f'only has a flat clo_alignment_table. All program groups will receive identical CLO rows.'
        )

    # Check CLO table quality.
    clo_rows = content.get('clo_alignment_table', [])
    filled_clos = [r for r in clo_rows if isinstance(r, dict) and str(r.get('clo_statement', '')).strip()]
    total_clos = len(clo_rows)
    if total_clos > 0 and len(filled_clos) < total_clos:
        warnings.append(f'Only {len(filled_clos)} of {total_clos} CLO rows have statements filled in.')

    # Check weekly outline quality.
    weekly = content.get('weekly_course_outline', [])
    filled_weeks = sum(
        1 for w in weekly
        if isinstance(w, dict) and (
            _csv_or_lines_to_list((w.get('intended_learning_outcomes') or {}).get('cognitive', []))
            or _csv_or_lines_to_list(w.get('topics', []))
        )
    )
    total_weeks = len(weekly)
    if total_weeks > 0 and filled_weeks < total_weeks:
        warnings.append(f'Only {filled_weeks} of {total_weeks} weekly outline rows have content.')

    return warnings


def _build_consultation_text(user_profile: Optional[Dict]) -> str:
    """Format consultation hours from user profile."""
    if not user_profile:
        return ''
    consultation = user_profile.get('consultation_hours', {})
    if not isinstance(consultation, dict):
        return ''
    day_order = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday']
    lines = []
    for day in day_order:
        data = consultation.get(day, {})
        if isinstance(data, dict) and (data.get('time') or data.get('room')):
            lines.append(f"{day.capitalize()}: {data.get('time', '')} — Room {data.get('room', '')}")
    return '\n'.join(lines)
