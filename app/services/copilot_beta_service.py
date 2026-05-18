import ast
import json
import hashlib
import re
import threading
import time as _time
from copy import deepcopy
from datetime import datetime

from flask import current_app

from app import supabase
from app.services.ai_client import AIClient
from app.services.template_context_extractor import (
    build_alignment_bundle_from_template_context,
)
from app.services.template_generation_spec import (
    default_generation_spec,
    normalize_alignment_column_label,
    normalize_alignment_row_label,
    normalize_generation_spec,
)
from app.utils import (
    get_department_id_by_name,
    get_department_signatory_settings,
    get_server_supabase_client,
    get_system_prompt,
    get_system_settings_map,
)

from app.services.copilot_beta_prompts import (
    ALIGNMENT_MAX_ITEMS,
    ALIGNMENT_MIN_ITEMS,
    ALIGNMENT_PREFERRED_MIN_ITEMS,
    AQRF_LEVEL_6_OPTIONS,
    BETA_ALIGNMENT_CHECKMARK_PROMPT,
    BETA_ALIGNMENT_DEFAULT_PROMPT,
    BETA_ALIGNMENT_REPAIR_PROMPT,
    BETA_CLO_DEFAULT_PROMPT,
    BETA_FINAL_FIX_PROMPT,
    BETA_FINAL_REVIEW_PROMPT,
    BETA_WEEKLY_DEFAULT_PROMPT,
    COPILOT_AQRF_OPTIONS_KEY,
    COPILOT_CORE_VALUES_KEY,
    COPILOT_DEFAULT_TEMPLATE_FILENAME_KEY,
    COPILOT_DEFAULT_TEMPLATE_NAME_KEY,
    COPILOT_GRADUATE_ATTRIBUTES_KEY,
    COPILOT_PQF_OPTIONS_KEY,
    COPILOT_PROGRAM_OUTCOMES_KEY,
    COPILOT_SDG_CONTEXT_KEY,
    COPILOT_SDG_OPTIONS_KEY,
    CORE_VALUE_OPTIONS,
    DEFAULT_CLO_ROWS,
    PQF_LEVEL_6_OPTIONS,
    PRE_EXAM_REVIEW_TOKENS,
    SDG_CONTEXT,
    SDG_OPTIONS,
    SGA_OPTIONS,
    WEEK_ROW_LABELS,
    WEEKLY_PROGRESS_STOPWORDS,
    advance_stage,
)

# Local alias for backward compatibility within this file.
_advance_stage = advance_stage

_BETA_AI_CALL_CONTEXT = threading.local()


class beta_ai_call_context:
    """Attach plan/user metadata to beta AI calls made on this thread."""

    def __init__(self, plan_id=None, user_id=None, preview_callback=None):
        self.plan_id = plan_id
        self.user_id = user_id
        self.preview_callback = preview_callback
        self._previous = None

    def __enter__(self):
        self._previous = (
            getattr(_BETA_AI_CALL_CONTEXT, "plan_id", None),
            getattr(_BETA_AI_CALL_CONTEXT, "user_id", None),
            getattr(_BETA_AI_CALL_CONTEXT, "preview_callback", None),
        )
        _BETA_AI_CALL_CONTEXT.plan_id = self.plan_id
        _BETA_AI_CALL_CONTEXT.user_id = self.user_id
        _BETA_AI_CALL_CONTEXT.preview_callback = self.preview_callback
        return self

    def __exit__(self, exc_type, exc, tb):
        previous_plan_id, previous_user_id, previous_preview_callback = self._previous or (None, None, None)
        _BETA_AI_CALL_CONTEXT.plan_id = previous_plan_id
        _BETA_AI_CALL_CONTEXT.user_id = previous_user_id
        _BETA_AI_CALL_CONTEXT.preview_callback = previous_preview_callback
        return False


def _publish_beta_ai_preview(task_key, text="", force=False):
    callback = getattr(_BETA_AI_CALL_CONTEXT, "preview_callback", None)
    if not callable(callback):
        return
    try:
        callback(task_key, text or "", force=force)
    except TypeError:
        callback(task_key, text or "")
    except Exception as exc:
        current_app.logger.debug("Beta AI live preview callback failed for %s: %s", task_key, exc)


def _dynamic_prompt_mode():
    raw = get_system_prompt(supabase, "use_dynamic_copilot_prompts", "force_on")
    value = str(raw or "profile_only").strip().lower().replace("-", "_")
    if value in {"off", "disable", "disabled"}:
        return "off"
    if value in {"force_on", "force", "on", "true", "1", "yes"}:
        return "force_on"
    return "profile_only"


def _has_profile_generation_spec(content, task_type):
    if not isinstance(content, dict):
        return False
    spec = _get_generation_spec(content)
    if not isinstance(spec, dict) or spec == default_generation_spec():
        return False
    if task_type in {"clo", "alignment_checkmark", "alignment_clo_based"}:
        groups = spec.get("clo_groups") if isinstance(spec.get("clo_groups"), list) else []
        return any(isinstance(group, dict) and group.get("rows") for group in groups)
    if task_type == "weekly":
        weekly = spec.get("weekly_outline") if isinstance(spec.get("weekly_outline"), dict) else {}
        return bool(weekly.get("rows"))
    if task_type == "po_io":
        return bool(spec.get("program_institutional_alignments"))
    return False


def _record_dynamic_prompt_mode(content, task_type, mode):
    if isinstance(content, dict):
        content.setdefault("_copilot_prompt_modes", {})[task_type] = mode


def _get_dynamic_prompt(content, task_type):
    """Always build a dynamic prompt for *task_type*.

    When the template profile has enough data a richly tailored prompt is
    returned.  Otherwise a seed-based prompt built from the available
    content (metadata, CLOs, template seed context) is returned so the AI
    always receives context-aware instructions.

    Returns ``None`` ONLY when the feature flag is explicitly "off".
    """
    mode = _dynamic_prompt_mode()
    if mode == "off":
        _record_dynamic_prompt_mode(content, task_type, "legacy_off")
        return None
    try:
        from app.services.copilot_beta_dynamic_prompts import (
            build_dynamic_alignment_checkmark_prompt,
            build_dynamic_alignment_clo_based_prompt,
            build_dynamic_clo_prompt,
            build_dynamic_po_io_prompt,
            build_dynamic_weekly_prompt,
        )
    except ImportError:
        return None

    builders = {
        "clo": build_dynamic_clo_prompt,
        "alignment_checkmark": build_dynamic_alignment_checkmark_prompt,
        "alignment_clo_based": build_dynamic_alignment_clo_based_prompt,
        "weekly": build_dynamic_weekly_prompt,
        "po_io": build_dynamic_po_io_prompt,
    }
    builder = builders.get(task_type)
    if not builder:
        _record_dynamic_prompt_mode(content, task_type, "fallback_default")
        return None
    try:
        prompt = builder(content)
        if prompt:
            _record_dynamic_prompt_mode(content, task_type, "forced_dynamic" if mode == "force_on" else "dynamic")
            return prompt
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "Dynamic prompt builder for %s failed, trying seed fallback.", task_type,
            exc_info=True,
        )
    # Never fall back to hardcoded — the builder now handles seed-based fallback internally.
    # If we reach here the builder returned None (truly no data), which shouldn't happen.
    _record_dynamic_prompt_mode(content, task_type, "fallback_default")
    return None


def _parse_current_semester(raw_value):
    text = str(raw_value or "").strip()
    if not text:
        return "", ""
    match = re.match(r"^(.*?)(\d{4}-\d{4})$", text)
    if not match:
        return text, ""
    return match.group(1).strip(" ,-"), match.group(2).strip()


def _csv_or_lines_to_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).replace("\r", "\n")
    parts = []
    for chunk in text.split("\n"):
        for item in chunk.split(","):
            cleaned = item.strip(" \t-•")
            if cleaned:
                parts.append(cleaned)
    return parts


def _normalize_mapped_clos(value):
    if value is None:
        return []
    if isinstance(value, list):
        raw_items = [str(item).strip() for item in value if str(item).strip()]
    else:
        raw_items = _csv_or_lines_to_list(value)

    normalized = []
    seen = set()
    for item in raw_items:
        matches = re.findall(r"CLO\s*\d+", str(item), flags=re.I)
        if matches:
            candidates = matches
        else:
            candidates = [item]
        for candidate in candidates:
            cleaned = re.sub(r"\s+", " ", str(candidate).upper()).strip()
            cleaned = re.sub(r"^CLO\s*(\d+)$", r"CLO \1", cleaned)
            if re.fullmatch(r"CLO \d+", cleaned) and cleaned not in seen:
                seen.add(cleaned)
                normalized.append(cleaned)
    return normalized


def _list_to_text(values):
    return "\n".join(values or [])


def _slugify_week_label(label):
    cleaned = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return cleaned


def _checkmark_po_key(po_code):
    """Stable key fragment for checkmark PO form fields and placeholders."""
    return re.sub(r"[^a-z0-9]+", "_", str(po_code or "").lower()).strip("_")


def _checkmark_form_key(row_index, po_code):
    return f"clo_{row_index}_check_{_checkmark_po_key(po_code)}"


def _checkmark_present_form_key(row_index):
    return f"clo_{row_index}_checkmarks_present"


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


def _compact_clo_checkmark_key(group_index, row_index, col_index):
    return f"x{_base36(group_index)}{_base36(row_index)}{_base36(col_index)}"


def _program_inst_key(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def _program_inst_form_key(alignment_id, row_key, col_key):
    return (
        f"pi_{_program_inst_key(alignment_id)}_"
        f"{_program_inst_key(row_key)}_{_program_inst_key(col_key)}"
    )


def _program_inst_present_form_key(alignment_id, row_key):
    return f"pi_{_program_inst_key(alignment_id)}_{_program_inst_key(row_key)}_present"


def _form_has_key(form_data, key):
    try:
        return key in form_data
    except TypeError:
        return False


def _week_prefix(label):
    mapping = {
        "Week 8 & 9": "week_8_9",
        "Week 10 & 11": "week_10_11",
        "Week 14 & 15": "week_14_15",
        "Week 16-17": "week_16_17",
    }
    if label in mapping:
        return mapping[label]
    label_str = str(label or "")
    # Paired weeks: "Week 8-9", "WEEK 8 & 9", "Week 8–9", "Week 8 and 9"
    m = re.search(r"weeks?\s*(\d+)\s*(?:[&–—-]|and|to)\s*(\d+)", label_str, re.IGNORECASE)
    if m:
        return f"week_{m.group(1)}_{m.group(2)}"
    # Individual week: "WEEK 1", "Week 5 (3 hours)", "WEEK 12" — only the week number
    m = re.search(r"weeks?\s*(\d+)", label_str, re.IGNORECASE)
    if m:
        return f"week_{m.group(1)}"
    return "week_1"


def _normalize_ai_row_list(data, preferred_keys):
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in preferred_keys:
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _record_validation_warning(content, message):
    validation = content.setdefault("validation", {})
    warnings = validation.setdefault("warnings", [])
    if message not in warnings:
        warnings.append(message)


def _remove_validation_warnings_matching(content, prefixes):
    validation = content.get("validation")
    if not isinstance(validation, dict):
        return False
    warnings = validation.get("warnings")
    if not isinstance(warnings, list):
        return False
    cleaned_prefixes = tuple(str(prefix).strip() for prefix in (prefixes or []) if str(prefix).strip())
    if not cleaned_prefixes:
        return False
    filtered_warnings = [
        item for item in warnings
        if not any(prefix in str(item or "").strip() for prefix in cleaned_prefixes)
    ]
    changed = filtered_warnings != warnings
    validation["warnings"] = filtered_warnings
    return changed


def _weekly_row_has_meaningful_content(row):
    if not isinstance(row, dict):
        return False
    ilo = row.get("intended_learning_outcomes", {})
    tla = row.get("teaching_learning_activities", {})
    resources = row.get("learning_resources", {})
    # Handle flat string/array output from AI
    if isinstance(ilo, (str, list)) and str(ilo).strip():
        return True
    if isinstance(tla, (str, list)) and str(tla).strip():
        return True
    if isinstance(resources, (str, list)) and str(resources).strip():
        return True
    return any([
        _normalize_mapped_clos(row.get("mapped_clos", [])),
        _csv_or_lines_to_list(ilo.get("cognitive", [])) if isinstance(ilo, dict) else [],
        _csv_or_lines_to_list(ilo.get("affective", [])) if isinstance(ilo, dict) else [],
        _csv_or_lines_to_list(ilo.get("psychomotor", [])) if isinstance(ilo, dict) else [],
        _csv_or_lines_to_list(row.get("topics", [])),
        _csv_or_lines_to_list(tla.get("lecture", [])) if isinstance(tla, dict) else [],
        _csv_or_lines_to_list(tla.get("practical_session", [])) if isinstance(tla, dict) else [],
        _csv_or_lines_to_list(tla.get("other", [])) if isinstance(tla, dict) else [],
        _csv_or_lines_to_list(row.get("assessment", [])),
        _csv_or_lines_to_list(resources.get("clms", [])) if isinstance(resources, dict) else [],
        _csv_or_lines_to_list(resources.get("textbook", [])) if isinstance(resources, dict) else [],
        _csv_or_lines_to_list(resources.get("website", [])) if isinstance(resources, dict) else [],
        _csv_or_lines_to_list(resources.get("journal", [])) if isinstance(resources, dict) else [],
        _csv_or_lines_to_list(resources.get("other", [])) if isinstance(resources, dict) else [],
    ])


def _summarize_week_labels(labels):
    cleaned = [str(label).strip() for label in labels if str(label).strip()]
    if not cleaned:
        return ""
    if len(cleaned) <= 3:
        return ", ".join(cleaned)
    return f"{cleaned[0]}, {cleaned[1]}, {cleaned[2]}, and {len(cleaned) - 3} more"


def _format_topics_for_docx(topics):
    """Format topics for DOCX placeholder replacement.
    Handles both flat text (string/list) and legacy categorized dict."""
    if isinstance(topics, str):
        return topics.strip()
    if isinstance(topics, list):
        return _bullet_lines(topics)
    if isinstance(topics, dict):
        # Legacy categorized dict — flatten all values with key labels.
        lines = []
        for key in topics:
            values = _csv_or_lines_to_list(topics.get(key, []))
            if values:
                if len(topics) > 1:
                    lines.append(f"{key}:")
                for v in values:
                    lines.append(f"- {v}")
        return '\n'.join(lines) if lines else ''
    return str(topics) if topics else ''


def _bullet_lines(items):
    cleaned = [str(item).strip() for item in (items or []) if str(item).strip()]
    return "\n".join(f"- {item}" for item in cleaned)


def _format_ilo_block(row):
    ilo = row.get("intended_learning_outcomes", {})
    if isinstance(ilo, str):
        return ilo.strip()
    if isinstance(ilo, list):
        return _bullet_lines(ilo)
    if not isinstance(ilo, dict):
        return str(ilo) if ilo else ""
    fallback_lead = row.get("_boilerplate_lead_in") or "At the end of the week, students should have the ability to:"
    lines = [str(ilo.get("lead_in") or fallback_lead).strip()]
    categorized_keys = [k for k in ilo if k.lower() in ("cognitive", "affective", "psychomotor")]
    if categorized_keys:
        for key_display in ("Cognitive", "Affective", "Psychomotor"):
            key_lower = key_display.lower()
            values = _csv_or_lines_to_list(ilo.get(key_lower, []))
            if values:
                lines.append(f"{key_display}:")
                lines.extend(f"- {item}" for item in values)
    else:
        for key, values in ilo.items():
            if key == "lead_in":
                continue
            vals = _csv_or_lines_to_list(values)
            if vals:
                lines.append(f"{key}:")
                lines.extend(f"- {item}" for item in vals)
    return "\n".join(lines)


def _format_tla_block(row):
    tla = row.get("teaching_learning_activities", {})
    if isinstance(tla, str):
        return tla.strip()
    if isinstance(tla, list):
        return _bullet_lines(tla)
    if not isinstance(tla, dict):
        return str(tla) if tla else ""
    lines = []
    for label, key in [("Lecture", "lecture"), ("Practical session", "practical_session"), ("Other", "other")]:
        values = _csv_or_lines_to_list(tla.get(key, []))
        if not values:
            continue
        lines.append(f"{label}:")
        lines.extend(f"- {item}" for item in values)
    return "\n".join(lines)


def _format_resources_block(row):
    resources = row.get("learning_resources", {})
    if isinstance(resources, str):
        return resources.strip()
    if isinstance(resources, list):
        return _bullet_lines(resources)
    if not isinstance(resources, dict):
        return str(resources) if resources else ""
    lines = []
    for label, key in [("CLMS", "clms"), ("Textbook", "textbook"), ("Website", "website"), ("Journal", "journal"), ("Other", "other")]:
        values = _csv_or_lines_to_list(resources.get(key, []))
        if not values:
            continue
        lines.append(f"{label}:")
        lines.extend(f"- {item}" for item in values)
    return "\n".join(lines)


def _unique_keep_order(values):
    seen = set()
    output = []
    for item in values:
        cleaned = str(item).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        output.append(cleaned)
    return output


def _to_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if value is None:
        return False
    return bool(value)


def _to_checkmark_value(value):
    if value == "✔":
        return "✔"
    if value in ("", None, False):
        return ""
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned in {"", "0", "false", "False", "no", "No"}:
            return ""
        if cleaned in {"✔", "✓", "x", "X", "1", "true", "True", "yes", "Yes", "on"}:
            return "✔"
    return "✔" if _to_bool(value) else ""


def _get_generation_spec(content=None, course_data=None):
    source = None
    if isinstance(content, dict):
        source = content.get("template_generation_spec")
    if not source and isinstance(course_data, dict):
        source = course_data.get("template_generation_spec") or course_data.get("generation_spec")

    # Return cached normalized spec if raw source hasn't changed.
    if isinstance(content, dict):
        cache = content.get("_cached_normalized_spec")
        if isinstance(cache, dict):
            source_raw = json.dumps(source, sort_keys=True, default=str) if source else ""
            cached_raw = cache.get("_cache_source_raw", "")
            if source_raw == cached_raw:
                return cache

    normalized = normalize_generation_spec(source)

    # Cache the result in content for the lifetime of this content object.
    if isinstance(content, dict):
        normalized_cache = dict(normalized)
        normalized_cache["_cache_source_raw"] = json.dumps(source, sort_keys=True, default=str) if source else ""
        content["_cached_normalized_spec"] = normalized_cache

    return normalized


def _clo_blueprints_from_spec(spec, group=None):
    group = group or ((spec.get("clo_groups") or [{}])[0] if isinstance(spec, dict) else {})
    rows = group.get("rows") if isinstance(group.get("rows"), list) else []
    if rows:
        return rows
    return [
        {"index": idx, "code": code, "label": code, "domain": domain}
        for idx, (code, domain) in enumerate(DEFAULT_CLO_ROWS, start=1)
    ]


def _weekly_blueprints_from_spec(spec):
    weekly = spec.get("weekly_outline") if isinstance(spec, dict) else {}
    rows = weekly.get("rows") if isinstance(weekly, dict) and isinstance(weekly.get("rows"), list) else []
    if rows:
        return rows
    return [{"index": idx, "label": label} for idx, label in enumerate(WEEK_ROW_LABELS, start=1)]


def _generation_shape_contract(content):
    content = content if isinstance(content, dict) else {}
    spec = _get_generation_spec(content)
    clo_rows = content.get("clo_alignment_table") if isinstance(content.get("clo_alignment_table"), list) else []
    weekly_rows = content.get("weekly_course_outline") if isinstance(content.get("weekly_course_outline"), list) else []
    groups = content.get("clo_alignment_groups") if isinstance(content.get("clo_alignment_groups"), list) else []
    program_inst_specs = _program_institutional_specs(content)

    # Extract typical item counts per weekly field from format hints.
    format_hints = (
        (spec.get("weekly_outline") or {}).get("format_hints")
        if isinstance((spec.get("weekly_outline") or {}).get("format_hints"), dict)
        else {}
    )
    weekly_field_capacities = {}
    for field, hint in format_hints.get("fields", {}).items():
        if isinstance(hint, dict):
            capacity = {
                "typical_item_count": hint.get("typical_item_count", 0),
                "output_style": hint.get("output_style", "flat_lines"),
                "list_marker": hint.get("list_marker", "dash"),
            }
            weekly_field_capacities[field] = capacity

    # Template-level capacities from the spec
    refs_spec = spec.get("references") if isinstance(spec.get("references"), dict) else {}
    consult_spec = spec.get("consultation") if isinstance(spec.get("consultation"), dict) else {}
    sig_spec = spec.get("signatories") if isinstance(spec.get("signatories"), dict) else {}

    return {
        "source": "template_profile" if spec and spec != default_generation_spec() else "default_fallback",
        "generation_mode": content.get("generation_mode", "template_matched"),
        "clo_row_count": len(clo_rows),
        "clo_codes": [row.get("clo_code") for row in clo_rows if isinstance(row, dict) and row.get("clo_code")],
        "clo_groups": [
            {
                "label": group.get("label") or group.get("id") or group.get("group_id") or "CLO Alignment",
                "row_count": len(group.get("clo_alignment_table") or []),
                "program_scope": group.get("program_scope", {}),
            }
            for group in groups
            if isinstance(group, dict)
        ],
        "weekly_row_count": len(weekly_rows),
        "weekly_labels": [
            row.get("time_frame_label")
            for row in weekly_rows
            if isinstance(row, dict) and row.get("time_frame_label")
        ],
        "weekly_field_capacities": weekly_field_capacities,
        "weekly_format_hints": format_hints,
        "template_capacities": {
            "reference_capacity": refs_spec.get("row_capacity") or 0,
            "consultation_capacity": consult_spec.get("row_capacity") or 0,
            "signatory_capacity": sig_spec.get("row_capacity") or 0,
        },
        "program_institutional_alignments": [
            {
                "id": item.get("id"),
                "alignment_format": item.get("alignment_format"),
                "row_labels_normalized": item.get("row_labels_normalized") or [],
                "column_labels_normalized": item.get("column_labels_normalized") or [],
                "required_alignment_keys": item.get("required_alignment_keys") or [],
            }
            for item in program_inst_specs
        ],
    }


def _generation_shape_contract_text(content):
    # Check cache — invalidate when content or generation mode changes.
    cache_key = content.get("_cached_shape_text_key")
    spec_hash = _compute_spec_hash(content)
    mode = content.get("generation_mode", "template_matched")
    current_key = f"{spec_hash}:{mode}"
    if cache_key == current_key:
        cached = content.get("_cached_shape_text")
        if cached is not None:
            return cached

    contract = _generation_shape_contract(content)
    if mode == "unconstrained":
        result = (
            "TEMPLATE PROFILE SHAPE (for reference only — counts are not restrictions):\n"
            "The following shape shows the template's structure, but you are NOT limited to these counts. "
            "Produce as many items as makes academic sense for the course — no upper limit on topics, "
            "resources, ILO items, or references. Only the row/group structure must be preserved.\n"
            f"{json.dumps(contract)}"
        )
    else:
        result = (
            "NON-NEGOTIABLE TEMPLATE PROFILE SHAPE:\n"
            "The following shape overrides any earlier or admin-configured prompt text that mentions fixed counts "
            "such as 8 CLO rows or a 14-week outline. Use these counts, codes, groups, and labels exactly.\n"
            f"{json.dumps(contract)}"
        )
    content["_cached_shape_text_key"] = current_key
    content["_cached_shape_text"] = result
    return result


def _template_seed_context(content):
    spec = _get_generation_spec(content)
    metadata_fields = spec.get("metadata_fields") if isinstance(spec.get("metadata_fields"), list) else []
    clo_groups = spec.get("clo_groups") if isinstance(spec.get("clo_groups"), list) else []
    weekly = spec.get("weekly_outline") if isinstance(spec.get("weekly_outline"), dict) else {}
    metadata_seeds = [
        {
            "field": field.get("field"),
            "label": field.get("label"),
            "detected_value": field.get("detected_value"),
        }
        for field in metadata_fields
        if isinstance(field, dict) and str(field.get("detected_value") or "").strip()
    ]
    clo_seed_rows = []
    first_group = clo_groups[0] if clo_groups and isinstance(clo_groups[0], dict) else {}
    for row in first_group.get("rows", []) if isinstance(first_group.get("rows"), list) else []:
        if not isinstance(row, dict):
            continue
        statement = str(row.get("source_statement") or "").strip()
        if statement:
            clo_seed_rows.append({
                "code": row.get("code"),
                "label": row.get("label"),
                "domain": row.get("domain"),
                "source_statement": statement,
            })
    weekly_seed_rows = []
    for row in weekly.get("rows", []) if isinstance(weekly.get("rows"), list) else []:
        if not isinstance(row, dict):
            continue
        preview = row.get("source_preview") if isinstance(row.get("source_preview"), list) else []
        if any(str(item or "").strip() for item in preview):
            weekly_seed_rows.append({
                "label": row.get("label"),
                "row_kind": row.get("row_kind"),
                # Truncate each cell to 80 chars to keep prompt size manageable
                "source_preview": [str(p)[:80] for p in preview[:6] if p],
            })
    return {
        "metadata": metadata_seeds[:16],
        "clo_rows": clo_seed_rows,
        "weekly_rows": weekly_seed_rows[:24],
    }



def _template_seed_context_text(content):
    # Check cache — invalidate when spec hash changes (since seed context depends on spec).
    spec_hash = _compute_spec_hash(content)
    cache_key = content.get("_cached_seed_text_key")
    if cache_key == spec_hash:
        cached = content.get("_cached_seed_text")
        if cached is not None:
            return cached
    seeds = _template_seed_context(content)
    if not any(seeds.values()):
        result = ""
    else:
        result = (
            "TEMPLATE-EXTRACTED SOURCE CONTENT:\n"
            "Use this as the strongest content anchor when it matches the selected course. "
            "Preserve the course identity, CLO intent, weekly topic progression, and source-template discipline. "
            "Do not introduce unrelated department defaults, IT/computing themes, mathematical modeling, or digital-system framing unless the template/course itself contains them.\n"
            f"{json.dumps(seeds)}"
        )
    content["_cached_seed_text_key"] = spec_hash
    content["_cached_seed_text"] = result
    return result


def _build_weekly_strict_count_block(hints):
    """Build a PER-COLUMN STRICT ITEM COUNT block from format_hints fields."""
    if not hints:
        return ""
    field_labels = {
        "time_frame_label": "time_frame_label / Schedule",
        "intended_learning_outcomes": "intended_learning_outcomes / ILO",
        "topics": "topics",
        "teaching_learning_activities": "teaching_learning_activities / TLA",
        "assessment": "assessment",
        "learning_resources": "learning_resources / References",
    }
    lines = []
    for field, info in hints.items():
        label = field_labels.get(field, field.replace("_", " ").title())
        cap = info.get("typical_item_count", 0)
        if cap <= 0:
            continue
        item_fmt = info.get("item_format", "inline")
        flat_short = info.get("is_flat_short_labels", False)

        style_desc = ""
        if info.get("output_style") == "categorized" and info.get("category_labels"):
            cats = info["category_labels"]
            style_desc = f" ({', '.join(cats[:4])})"
        elif item_fmt == "complete_sentences":
            style_desc = ", complete sentences"
        elif item_fmt == "semicolon_list":
            style_desc = ", semicolon list"
        elif item_fmt == "label_fragments":
            style_desc = ", label fragments"
        if flat_short:
            style_desc += ", short labels only"

        lines.append(f"  {label}: EXACTLY {cap} items{style_desc}")
    if not lines:
        return ""
    return "\n".join(lines)


def _build_weekly_format_instructions(content):
    """Build a concise, column-by-column format instruction block from the
    template profile's weekly outline format hints, describing the exact
    formatting conventions the AI must follow for every weekly row."""
    spec = _get_generation_spec(content)
    weekly_spec = spec.get("weekly_outline", {}) if isinstance(spec, dict) else {}
    hints = (weekly_spec.get("format_hints") or {}).get("fields") if isinstance(weekly_spec, dict) else None
    if not hints:
        return ""

    field_labels = {
        "time_frame_label": "Schedule / Time Frame",
        "intended_learning_outcomes": "ILO / Learning Outcomes",
        "topics": "Topics / Topic Outline",
        "teaching_learning_activities": "Teaching-Learning Activities (TLA / Methodology)",
        "assessment": "Assessment",
        "learning_resources": "Learning Resources / References",
    }

    lines = []
    for field, info in hints.items():
        label = field_labels.get(field, field.replace("_", " ").title())
        style = info.get("output_style", "flat_lines")
        categories = info.get("category_labels", [])
        numbered = info.get("uses_numbered_items", False)
        dashes = info.get("uses_dash_bullets", False)
        item_count = info.get("typical_item_count", 0)
        sub = info.get("has_sub_bullets", False)
        samples = info.get("samples", [])
        lead_in = info.get("lead_in")
        item_format = info.get("item_format", "inline")
        hierarchy = info.get("has_hierarchy", False)

        desc = f"**{label}**: "
        rules = []

        # ── category-based structure ──
        if style == "categorized" and categories:
            cat_list = ", ".join(categories[:6])
            rules.append(f"use these exact category labels: {cat_list}")
        elif style == "flat_lines" and numbered:
            rules.append("use numbered items (1., 2., 3.)")
        elif style == "flat_lines" and dashes:
            rules.append("use dash-bullet items (-, •)")
        elif style == "flat_lines" and item_count > 1:
            # items exist but no detectable markers — they are plain newline-separated
            rules.append("each item on its own line; no bullet or number prefix")

        # ── hierarchy ──
        if hierarchy:
            # Map to generic terms rather than template-specific labels
            rules.append("use heading lines (e.g. Unit, Lesson, Module, Section) for structure")

        # ── lead-in sentence ──
        if lead_in:
            # Strip template-specific content words when building instruction
            lead_clean = re.sub(r'(?:explain|describe|identify|analyze|evaluate|create|apply|understand|demonstrate|discuss|define|compare|contrast|illustrate|develop|design|implement|assess|reflect).*', '...', lead_in[:150], flags=re.IGNORECASE)
            lead_preview = lead_clean if len(lead_clean) > 20 else lead_in[:120]
            rules.append(f'MUST start with a lead-in sentence like: "{lead_preview}"')

        # ── item formatting style ──
        if item_format == "complete_sentences":
            rules.append("each item must be a complete sentence ending with period (.)")
        elif item_format == "semicolon_list":
            rules.append("items end with semicolon (;) except the last item which ends with period (.)")
        elif item_format == "label_fragments":
            rules.append("items are short labels or phrases (NOT full sentences)")
        if info.get("is_flat_short_labels"):
            rules.append("keep each item brief — match the template's terse style; do not pad with extra context")

        # ── item count (hard cap, not soft target) ──
        if item_count > 0:
            rules.append(f"produce EXACTLY {item_count} items per row (template cap, do not exceed)")
        if sub:
            rules.append("include sub-bullets under top-level items")
        if not rules:
            rules.append("plain paragraph text")

        desc += "; ".join(rules) + "."

        # ── sample ──
        if samples:
            preview = samples[0]
            if len(preview) > 250:
                preview = preview[:250] + "..."
            desc += f"\n  Template sample: {preview}"

        lines.append(desc)

    if not lines:
        return ""

    return (
        "TEMPLATE WEEKLY FORMAT INSTRUCTIONS — follow these conventions for every generated row:\n\n"
        + "\n\n".join(lines)
    )


def _build_weekly_output_shape(content):
    """Build a template-aware replacement for BETA_WEEKLY_DEFAULT_PROMPT
    that describes the EXACT output JSON shape per column based on the
    template profile's format hints.

    When the template uses flat lists (numbered items, dash bullets, plain
    newlines) instead of categorized dicts, this prompt instructs the AI
    to produce matching flat output.  When the template uses categorized
    sections (Cognitive/Affective/Psychomotor, Lecture/Practical/Other,
    CLMS/Textbook/Website...), it preserves that shape."""
    spec = _get_generation_spec(content)
    weekly_spec = spec.get("weekly_outline", {}) if isinstance(spec, dict) else {}
    hints = (weekly_spec.get("format_hints") or {}).get("fields") if isinstance(weekly_spec, dict) else None
    if not hints:
        return ""

    # Decide output shape per field
    ilo_hint = hints.get("intended_learning_outcomes", {})
    topics_hint = hints.get("topics", {})
    tla_hint = hints.get("teaching_learning_activities", {})
    assessment_hint = hints.get("assessment", {})
    resources_hint = hints.get("learning_resources", {})

    # ── ILO shape ──
    if ilo_hint.get("output_style") == "categorized" and ilo_hint.get("category_labels"):
        cats = ilo_hint["category_labels"]
        cat_parts = ', '.join(f'"{c.lower()}": ["...", "..."]' for c in cats)
        ilo_shape = f'{{"lead_in": "string", {cat_parts}}}  // categorized dict with these exact keys'
    elif ilo_hint.get("uses_numbered_items"):
        ilo_shape = '"a single multi-line string — start with a lead-in sentence, then numbered items (1., 2., 3.) each on a new line"'
    elif ilo_hint.get("uses_dash_bullets"):
        ilo_shape = '"a single multi-line string with dash-bullet items"'
    else:
        ilo_shape = '"plain text"'

    # ── Topics shape ──
    if topics_hint.get("has_hierarchy"):
        topics_shape = '["array of strings — include heading lines (e.g. Unit 1, Lesson 1) and sub-items each as a separate array element"]'
    elif topics_hint.get("uses_numbered_items"):
        topics_shape = '["array of numbered strings"]'
    elif topics_hint.get("uses_dash_bullets"):
        topics_shape = '["array of dash-bullet strings"]'
    else:
        topics_shape = '["array of plain strings"]'

    # ── TLA shape ──
    if tla_hint.get("output_style") == "categorized" and tla_hint.get("category_labels"):
        cats = tla_hint["category_labels"]
        cat_parts = ', '.join(f'"{c.lower()}": ["...", "..."]' for c in cats)
        tla_shape = f'{{{cat_parts}}}  // categorized dict with these exact keys'
    elif tla_hint.get("uses_dash_bullets"):
        tla_shape = '"a single multi-line string with dash-bullet items"'
    elif tla_hint.get("uses_numbered_items"):
        tla_shape = '"a single multi-line string with numbered items"'
    else:
        tla_shape = '["array of plain strings — each teaching method on its own line"]'

    # ── Assessment shape ──
    if assessment_hint.get("item_format") == "complete_sentences":
        if assessment_hint.get("uses_numbered_items"):
            assessment_shape = '["array of strings — each item a complete sentence ending with period (.), numbered"]'
        elif assessment_hint.get("uses_dash_bullets"):
            assessment_shape = '["array of strings — each item a complete sentence ending with period (.), dash-bulleted"]'
        else:
            assessment_shape = '["array of strings — each item a complete sentence ending with period (.)"]'
    elif assessment_hint.get("item_format") == "semicolon_list":
        assessment_shape = '["array of strings — items end with semicolon (;) except the last which ends with period (.)"]'
    elif assessment_hint.get("uses_numbered_items"):
        assessment_shape = '["array of strings — numbered items"]'
    elif assessment_hint.get("uses_dash_bullets"):
        assessment_shape = '["array of strings — dash-bullet items"]'
    else:
        assessment_shape = '["array of plain strings"]'

    # ── Resources shape ──
    if resources_hint.get("output_style") == "categorized" and resources_hint.get("category_labels"):
        cats = resources_hint["category_labels"]
        cat_parts = ', '.join(f'"{c.lower()}": ["...", "..."]' for c in cats)
        resources_shape = f'{{{cat_parts}}}  // categorized dict with these exact keys'
    elif resources_hint.get("uses_dash_bullets"):
        resources_shape = '"a single multi-line string with dash-bullet items"'
    elif resources_hint.get("uses_numbered_items"):
        resources_shape = '"a single multi-line string with numbered items"'
    else:
        resources_shape = '["array of plain strings — each resource on its own line"]'

    item_format = assessment_hint.get("item_format", "inline")
    assessment_format_note = ""
    if item_format == "complete_sentences":
        assessment_format_note = "\n- Assessment items must be complete sentences ending with period (.)"
    elif item_format == "semicolon_list":
        assessment_format_note = "\n- Assessment items end with semicolon (;) except the last which ends with period (.)"

    return f"""You are generating the weekly course outline for a beta CLP workflow.
Return JSON only with one top-level key: `weekly_course_outline`. No markdown, prose, or extra keys.

CRITICAL — OUTPUT FORMAT: The template profile has detected the following per-column output shapes. You MUST follow these shapes exactly:

- intended_learning_outcomes: {ilo_shape}
- topics: {topics_shape}
- teaching_learning_activities: {tla_shape}
- assessment: {assessment_shape}
- learning_resources: {resources_shape}

PER-COLUMN STRICT ITEM COUNT — never exceed:
{_build_weekly_strict_count_block(hints)}

CONTENT QUALITY RULES:
- Every week fully populated — no empty arrays, no placeholder/generic/repeated filler
- All wording must be faculty-ready, measurable, course-specific, and aligned with mapped CLOs
- Build each week: pick concrete resources first -> derive topics -> TLAs -> assessments from those resources
- Consecutive weeks must not reuse the same topic/resource cluster unless clearly deepening the material
- Do not mention week labels/numbers inside content fields; temporal info belongs only in `time_frame_label`

RESOURCE RULES:
- Resources must be week-specific; never copy-paste boilerplate across rows
- When the template uses flat lists, embed newlines (\\n) inside string values for multi-line output
- When the template uses categorized dicts, use the exact category keys shown

TIME-FRAME RULES:
- Use the supplied dynamic labels exactly
- ALL weeks except strictly orientation ones MUST have at least 1-2 mapped_clos

{assessment_format_note}

Return this exact JSON shape with `weekly_course_outline` as the only top-level key."""


def _get_alignment_style(content):
    """Return 'checkmark' or 'clo_based' from content or its generation spec.

    Checks in order: explicit content field, spec top-level, then
    individual CLO groups.  If any group declares checkmark style the
    whole alignment is treated as checkmark (per-group checking happens
    inside ``_generate_checkmark_alignment``).
    """
    if isinstance(content, dict):
        explicit = content.get("alignment_style")
        if explicit in ("checkmark", "clo_based"):
            return explicit
    spec = _get_generation_spec(content)
    style = spec.get("alignment_style") if isinstance(spec, dict) else None
    if style in ("checkmark", "clo_based"):
        return style
    # Fallback: check individual group styles
    groups = spec.get("clo_groups") if isinstance(spec, dict) else []
    if isinstance(groups, list):
        for g in groups:
            if isinstance(g, dict) and g.get("alignment_style") == "checkmark":
                return "checkmark"
        for g in groups:
            if isinstance(g, dict) and g.get("checkmark_po_codes"):
                return "checkmark"
    return "clo_based"


def _should_disable_po_matrix(content):
    """
    Returns True when the template has no CLO→PO matrix to generate.
    """
    gen_spec = content.get("template_generation_spec") or {}
    clo_groups = gen_spec.get("clo_groups") if isinstance(gen_spec.get("clo_groups"), list) else []
    if clo_groups:
        return False
    groups = content.get("clo_alignment_groups") if isinstance(content.get("clo_alignment_groups"), list) else []
    if groups:
        return False
    return bool(gen_spec.get("program_institutional_alignments"))


def _get_checkmark_po_codes(content):
    """Return the ordered list of PO column codes for checkmark templates."""
    spec = _get_generation_spec(content)
    groups = spec.get("clo_groups") if isinstance(spec, dict) and isinstance(spec.get("clo_groups"), list) else []
    for group in groups:
        codes = group.get("checkmark_po_codes") if isinstance(group, dict) else None
        if isinstance(codes, list) and codes:
            return codes
    return []


def _group_checkmark_po_codes(group, fallback_codes=None):
    codes = group.get("checkmark_po_codes") if isinstance(group, dict) and isinstance(group.get("checkmark_po_codes"), list) else []
    return [str(code).strip() for code in (codes or fallback_codes or []) if str(code).strip()]


def _group_program_outcomes(group, fallback_outcomes=None):
    scope = group.get("program_scope") if isinstance(group, dict) and isinstance(group.get("program_scope"), dict) else {}
    items = scope.get("items") if isinstance(scope.get("items"), list) else []
    scoped = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if code:
            scoped.append({"code": code, "description": str(item.get("description") or item.get("title") or "").strip()})
    return scoped or (fallback_outcomes or [])


def _program_institutional_specs(content):
    spec = _get_generation_spec(content)
    alignments = spec.get("program_institutional_alignments") if isinstance(spec, dict) else []
    return [item for item in alignments if isinstance(item, dict)]


def _slugify(value):
    """Lowercase, collapse non-alnum to underscore, strip leading/trailing _."""
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def _weekly_field_spec(hints, field):
    """Return a dict describing how to display/form a weekly column:
    ``{"field": field_name, "sub_fields": [{"key": cat, "label": cat}, ...]}``
    When the template is flat, ``sub_fields`` is empty and the field is shown
    as a single textarea.  When categorized, each sub-field gets its own
    textarea.
    """
    hint = hints.get(field, {}) if hints else {}
    categories = hint.get("category_labels", []) if isinstance(hint, dict) else []
    style = hint.get("output_style", "flat_lines") if isinstance(hint, dict) else "flat_lines"
    # topics/topic outline is hierarchical text, NEVER a categorized dict —
    # Unit/Lesson patterns are structural headings, not domain categories.
    if field == "topics":
        sub_fields = []
    elif style == "categorized" and categories:
        sub_fields = [{"key": _slugify(c), "label": c} for c in categories]
    else:
        sub_fields = []
    return {"field": field, "sub_fields": sub_fields}


_WEEKLY_FIELDS = ["intended_learning_outcomes", "topics", "teaching_learning_activities",
                   "assessment", "learning_resources"]

_WEEKLY_FIELD_LABELS = {
    "intended_learning_outcomes": "Learning Outcomes",
    "topics": "Topics",
    "teaching_learning_activities": "Methodology",
    "assessment": "Assessment",
    "learning_resources": "Learning Resources",
}


def _empty_program_institutional_alignments(spec, existing=None):
    existing = existing if isinstance(existing, dict) else {}
    output = {}
    for alignment in (spec.get("program_institutional_alignments") or []):
        if not isinstance(alignment, dict):
            continue
        alignment_id = alignment.get("id") or f"program_institutional_alignment_{len(output) + 1}"
        stored_matrix = existing.get(alignment_id) if isinstance(existing.get(alignment_id), dict) else {}
        normalized_stored = {}
        for stored_row_key, stored_row_values in stored_matrix.items():
            row_key = normalize_alignment_row_label(stored_row_key)
            if not row_key or not isinstance(stored_row_values, dict):
                continue
            normalized_stored.setdefault(row_key, {})
            for stored_col_key, stored_value in stored_row_values.items():
                col_key = normalize_alignment_column_label(stored_col_key)
                if col_key:
                    normalized_stored[row_key][col_key] = stored_value
        matrix = {}
        rows = alignment.get("row_labels_normalized") if isinstance(alignment.get("row_labels_normalized"), list) else []
        cols = alignment.get("column_labels_normalized") if isinstance(alignment.get("column_labels_normalized"), list) else []
        for row_key in rows:
            row_key = normalize_alignment_row_label(row_key)
            if not row_key:
                continue
            stored_row = normalized_stored.get(row_key) if isinstance(normalized_stored.get(row_key), dict) else {}
            matrix[row_key] = {
                normalize_alignment_column_label(col_key): _to_checkmark_value(stored_row.get(normalize_alignment_column_label(col_key), ""))
                for col_key in cols
                if normalize_alignment_column_label(col_key)
            }
        output[alignment_id] = matrix
    return output


def _empty_clo_row(blueprint, existing=None, alignment_style="clo_based", checkmark_po_codes=None):
    existing = existing if isinstance(existing, dict) else {}
    code = str(blueprint.get("code") or existing.get("clo_code") or f"CLO {blueprint.get('index') or 1}").strip()
    domain = str(blueprint.get("domain") or existing.get("domain") or "cognitive").strip().lower()
    if domain not in {"cognitive", "affective", "psychomotor"}:
        domain = "cognitive"
    row = {
        "domain": domain,
        "clo_code": code,
        "clo_statement": str(existing.get("clo_statement") or "").strip(),
        "teacher_locked": _to_bool(existing.get("teacher_locked", False)),
        "review_status": existing.get("review_status", "draft"),
    }
    if alignment_style == "checkmark":
        existing_checks = existing.get("checkmark_alignments") if isinstance(existing.get("checkmark_alignments"), dict) else {}
        po_codes = checkmark_po_codes or []
        row["checkmark_alignments"] = {
            po: _to_bool(existing_checks.get(po, False))
            for po in po_codes
        }
    else:
        row["aligned_plos"] = _csv_or_lines_to_list(existing.get("aligned_plos", []))
        row["graduate_attributes"] = _csv_or_lines_to_list(existing.get("graduate_attributes", []))
        row["core_values"] = _csv_or_lines_to_list(existing.get("core_values", []))
        row["pqf_level_6_alignment"] = _csv_or_lines_to_list(existing.get("pqf_level_6_alignment", []))
        row["aqrf_level_6_alignment"] = _csv_or_lines_to_list(existing.get("aqrf_level_6_alignment", []))
        row["relevant_sdgs"] = _csv_or_lines_to_list(existing.get("relevant_sdgs", []))
    return row


def _empty_week_row(label, existing=None):
    existing = existing if isinstance(existing, dict) else {}
    # Preserve flat string/list output from AI — only build categorized
    # dict structure when the existing content already has that format.
    ilo_existing = existing.get("intended_learning_outcomes")
    tla_existing = existing.get("teaching_learning_activities")
    res_existing = existing.get("learning_resources")
    return {
        "time_frame_label": label,
        "field_key": _slugify_week_label(label),
        "mapped_clos": _normalize_mapped_clos(existing.get("mapped_clos", [])),
        "intended_learning_outcomes":
            ilo_existing if isinstance(ilo_existing, (str, list))
            else {
                "lead_in": str(ilo_existing.get("lead_in") if isinstance(ilo_existing, dict) else existing.get("intended_learning_outcomes", {}).get("lead_in") or "At the end of the week, students should have the ability to:").strip(),
                "cognitive": _csv_or_lines_to_list(ilo_existing.get("cognitive", []) if isinstance(ilo_existing, dict) else []),
                "affective": _csv_or_lines_to_list(ilo_existing.get("affective", []) if isinstance(ilo_existing, dict) else []),
                "psychomotor": _csv_or_lines_to_list(ilo_existing.get("psychomotor", []) if isinstance(ilo_existing, dict) else []),
            },
        "topics": _csv_or_lines_to_list(existing.get("topics", [])),
        "teaching_learning_activities":
            tla_existing if isinstance(tla_existing, (str, list))
            else {
                "lecture": _csv_or_lines_to_list(tla_existing.get("lecture", []) if isinstance(tla_existing, dict) else []),
                "practical_session": _csv_or_lines_to_list(tla_existing.get("practical_session", []) if isinstance(tla_existing, dict) else []),
                "other": _csv_or_lines_to_list(tla_existing.get("other", []) if isinstance(tla_existing, dict) else []),
            },
        "assessment": _csv_or_lines_to_list(existing.get("assessment", [])),
        "learning_resources":
            res_existing if isinstance(res_existing, (str, list))
            else {
                "clms": _csv_or_lines_to_list(res_existing.get("clms", []) if isinstance(res_existing, dict) else []),
                "textbook": _csv_or_lines_to_list(res_existing.get("textbook", []) if isinstance(res_existing, dict) else []),
                "website": _csv_or_lines_to_list(res_existing.get("website", []) if isinstance(res_existing, dict) else []),
                "journal": _csv_or_lines_to_list(res_existing.get("journal", []) if isinstance(res_existing, dict) else []),
                "other": _csv_or_lines_to_list(res_existing.get("other", []) if isinstance(res_existing, dict) else []),
            },
        "teacher_locked": _to_bool(existing.get("teacher_locked", False)),
        "review_status": existing.get("review_status", "draft"),
    }


def _group_row_existing_map(rows):
    return {
        str(row.get("clo_code") or ""): row
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict)
    }


def _merge_primary_clo_row(existing, primary):
    existing = dict(existing) if isinstance(existing, dict) else {}
    primary = primary if isinstance(primary, dict) else {}
    for key, value in primary.items():
        if key == "teacher_locked":
            existing[key] = _to_bool(value)
        elif isinstance(value, list):
            if value:
                existing[key] = value
        elif isinstance(value, str):
            if value.strip():
                existing[key] = value
        elif value not in (None, "", [], {}):
            existing[key] = value
    return existing


def _normalize_clo_groups(content, spec):
    alignment_style = _get_alignment_style(content)
    checkmark_po_codes = _get_checkmark_po_codes(content)
    existing_groups = content.get("clo_alignment_groups") if isinstance(content.get("clo_alignment_groups"), list) else []
    existing_by_id = {
        str(group.get("group_id") or group.get("id") or ""): group
        for group in existing_groups
        if isinstance(group, dict)
    }
    primary_existing = content.get("clo_alignment_table") if isinstance(content.get("clo_alignment_table"), list) else []
    groups = []
    for group_index, group_spec in enumerate(spec.get("clo_groups") or [], start=1):
        group_id = str(group_spec.get("id") or f"clo_group_{group_index}")
        stored = existing_by_id.get(group_id, {})
        existing_rows = stored.get("clo_alignment_table") if isinstance(stored.get("clo_alignment_table"), list) else []
        if group_index == 1 and not existing_rows:
            existing_rows = primary_existing
        existing_by_code = _group_row_existing_map(existing_rows)
        if group_index == 1 and primary_existing:
            for code, primary_row in _group_row_existing_map(primary_existing).items():
                existing_by_code[code] = _merge_primary_clo_row(existing_by_code.get(code, {}), primary_row)
        group_po_codes = group_spec.get("checkmark_po_codes") if isinstance(group_spec.get("checkmark_po_codes"), list) else checkmark_po_codes
        rows = []
        for blueprint in _clo_blueprints_from_spec(spec, group_spec):
            existing = existing_by_code.get(str(blueprint.get("code") or ""), {})
            rows.append(_empty_clo_row(blueprint, existing, alignment_style=alignment_style, checkmark_po_codes=group_po_codes))
        groups.append({
            "group_id": group_id,
            "label": group_spec.get("label") or f"CLO Alignment {group_index}",
            "program_scope": group_spec.get("program_scope") if isinstance(group_spec.get("program_scope"), dict) else {},
            "checkmark_po_codes": group_po_codes if alignment_style == "checkmark" else [],
            "clo_alignment_table": rows,
        })
    if not groups:
        if spec.get("program_institutional_alignments"):
            content["clo_alignment_groups"] = []
            content["clo_alignment_table"] = []
            return content
        group_spec = default_generation_spec()["clo_groups"][0]
        rows = [_empty_clo_row(bp, {}, alignment_style=alignment_style, checkmark_po_codes=checkmark_po_codes) for bp in _clo_blueprints_from_spec(spec, group_spec)]
        groups = [{"group_id": "clo_group_1", "label": "CLO Alignment", "program_scope": {}, "clo_alignment_table": rows}]
    content["clo_alignment_groups"] = groups
    content["clo_alignment_table"] = groups[0]["clo_alignment_table"] if groups else []
    return content


def _sync_primary_clo_rows_to_groups(content, include_alignment=False):
    primary = content.get("clo_alignment_table") if isinstance(content.get("clo_alignment_table"), list) else []
    groups = content.get("clo_alignment_groups") if isinstance(content.get("clo_alignment_groups"), list) else []
    if not primary or not groups:
        return content
    alignment_style = _get_alignment_style(content)
    for group_index, group in enumerate(groups):
        rows = group.get("clo_alignment_table") if isinstance(group.get("clo_alignment_table"), list) else []
        for idx, source in enumerate(primary):
            if idx >= len(rows):
                continue
            target = rows[idx]
            for key in ("clo_code", "domain", "clo_statement", "teacher_locked"):
                target[key] = source.get(key, target.get(key))
            if include_alignment or group_index == 0:
                if alignment_style == "checkmark":
                    for key in ("checkmark_alignments", "review_status"):
                        if key in source:
                            target[key] = source.get(key, target.get(key))
                else:
                    for key in (
                        "aligned_plos",
                        "graduate_attributes",
                        "core_values",
                        "pqf_level_6_alignment",
                        "aqrf_level_6_alignment",
                        "relevant_sdgs",
                        "review_status",
                    ):
                        target[key] = source.get(key, target.get(key))
    if groups:
        content["clo_alignment_table"] = groups[0].get("clo_alignment_table", primary)
    return content


def _normalize_clo_domain(raw_value, fallback_domain):
    value = str(raw_value or "").strip().lower()
    if value in {"cognitive", "affective", "psychomotor"}:
        return value
    return fallback_domain


def _is_orientation_week(label):
    text = str(label or "").strip().lower()
    # Matches 'Week 1', 'week 1', etc., flexibly
    is_week_1 = bool(re.search(r"\bweek\s*1\b", text))
    has_intro_keyword = bool(re.search(r"\b(?:orientation|introduction|course overview|overview|induction|preliminaries)\b", text))
    return is_week_1 or has_intro_keyword


def _non_orientation_week_labels():
    return [label for label in WEEK_ROW_LABELS if not _is_orientation_week(label)]


def _week_progress_step(label):
    labels = _non_orientation_week_labels()
    total = len(labels)
    if total == 0:
        return 1, 1
    if label not in labels:
        return 1, total
    return labels.index(label) + 1, total


def _course_signal_text(metadata):
    return " ".join(
        [
            str((metadata or {}).get("course_title") or ""),
            str((metadata or {}).get("course_description") or ""),
            str((metadata or {}).get("service_learning_component") or ""),
            str((metadata or {}).get("source_context") or ""),
        ]
    ).lower()


def _course_reference_candidates(metadata):
    signal = _course_signal_text(metadata)

    if any(keyword in signal for keyword in [
        "contemporary world",
        "globalization",
        "global citizenship",
        "global governance",
        "sustainable development",
        "global economy",
        "global migration",
    ]):
        return {
            "themes": [
                "definitions, interpretations, and theoretical paradigms of globalization",
                "global economic integration, market systems, and international financial institutions",
                "the interstate system, global governance, and the role of international organizations",
                "global divides, regionalism, media, culture, and religion in globalization",
                "global cities, demography, migration, sustainable development, food security, and global citizenship",
            ],
            "textbook": [
                "Claudio, L. E., & Abinales, P. N. (2018). The Contemporary World. C&E Publishing.",
                "Coronacion, D., & Calilung, F. (2018). Convergence: A College Textbook in Contemporary World. Books Atbp. Publishing Corp.",
                "Steger, M. B. (2020). Globalization: A Very Short Introduction (5th ed.). Oxford University Press.",
            ],
            "website": [
                "https://sdgs.un.org/goals",
                "https://www.un.org/en/global-issues",
                "https://www.worldbank.org/en/topic/globalization",
            ],
            "journal": [
                "Appadurai, A. (1990). Disjuncture and difference in the global cultural economy. Theory, Culture & Society.",
                "Held, D., & McGrew, A. (2007). Globalization theory: Approaches and controversies. Polity.",
                "Robertson, R. (1995). Glocalization: Time-space and homogeneity-heterogeneity. Global Modernities.",
            ],
            "other": [
                "UN Sustainable Development Goals classroom activity sheet",
                "Global issues news analysis worksheet",
                "Global citizenship reflection guide",
            ],
        }

    if any(keyword in signal for keyword in ["assurance", "security", "cyber", "risk", "threat", "vulnerability"]):
        return {
            "themes": [
                "information assurance principles and security objectives",
                "threat modeling, vulnerabilities, and risk assessment",
                "security governance, policies, and compliance",
                "access control, authentication, and identity management",
                "network and endpoint security controls",
                "incident response, forensics basics, and recovery planning",
                "security auditing, metrics, and continuous improvement",
            ],
            "textbook": [
                "Whitman, M. E., & Mattord, H. J. (2021). Principles of Information Security (7th ed.). Cengage.",
                "Stallings, W., & Brown, L. (2018). Computer Security: Principles and Practice (4th ed.). Pearson.",
                "Andress, J. (2019). The Basics of Information Security (3rd ed.). Syngress.",
            ],
            "website": [
                "https://www.nist.gov/cyberframework",
                "https://owasp.org/www-project-top-ten/",
                "https://www.cisa.gov/cybersecurity",
            ],
            "journal": [
                "Siponen, M., Mahmood, M. A., & Pahnila, S. (2014). Employees' adherence to information security policies: An exploratory field study. Information & Management.",
                "Venter, H., & Eloff, J. H. P. (2003). A taxonomy for information security technologies. Computers & Security.",
                "Anderson, R. (2001). Why information security is hard: An economic perspective. Proceedings of the Annual Computer Security Applications Conference.",
            ],
            "other": [
                "Threat modeling worksheet or STRIDE analysis template",
                "Risk register template and security controls checklist",
            ],
        }

    if any(keyword in signal for keyword in ["hci", "human-computer", "usability", "interaction design", "user experience", "ux"]):
        return {
            "themes": [
                "human-computer interaction foundations and design principles",
                "usability goals, user-centered design, and accessibility",
                "interaction paradigms, interfaces, and mental models",
                "prototyping, evaluation methods, and iterative refinement",
                "emerging interaction technologies and ethical implications",
            ],
            "textbook": [
                "Preece, J., Rogers, Y., & Sharp, H. (2019). Interaction Design: Beyond Human-Computer Interaction (5th ed.). Wiley.",
                "Dix, A., Finlay, J., Abowd, G. D., & Beale, R. (2004). Human-Computer Interaction (3rd ed.). Pearson.",
                "Shneiderman, B., Plaisant, C., Cohen, M., Jacobs, S., Elmqvist, N., & Diakopoulos, N. (2016). Designing the User Interface (6th ed.). Pearson.",
            ],
            "website": [
                "https://www.interaction-design.org/literature",
                "https://www.nngroup.com/articles/",
                "https://www.w3.org/WAI/standards-guidelines/wcag/",
            ],
            "journal": [
                "Norman, D. A. (2010). The research-practice gap: The need for translational developers. Interactions.",
                "Lazar, J., Feng, J. H., & Hochheiser, H. (2017). Research methods in human-computer interaction (2nd ed.) insights. International Journal of Human-Computer Studies.",
                "Bannon, L. (2011). Reimagining HCI: Toward a more human-centered perspective. Interactions.",
            ],
            "other": ["Usability test script and observation sheet", "Low-fidelity prototype worksheet or wireframing guide"],
        }

    return {
        "themes": [
            "course fundamentals and conceptual framework",
            "methods, techniques, and tool-supported workflows",
            "analysis and design using discipline-relevant examples",
            "implementation, testing, and quality considerations",
            "integration, communication, and professional practice",
        ],
        "textbook": [
            "Laudon, K. C., & Laudon, J. P. (2021). Management Information Systems: Managing the Digital Firm (17th ed.). Pearson.",
            "Sommerville, I. (2016). Software Engineering (10th ed.). Pearson.",
        ],
        "website": [
            "https://www.iso.org/standards.html",
            "https://ocw.mit.edu/",
            "https://www.computer.org/",
        ],
        "journal": [
            "Denning, P. J. (2005). Is computer science science? Communications of the ACM.",
            "Hevner, A. R., March, S. T., Park, J., & Ram, S. (2004). Design science in information systems research. MIS Quarterly.",
        ],
        "other": ["Course case study packet", "Rubric and guided reflection worksheet"],
    }


def _pick_progressive_theme(metadata, label):
    themes = _course_reference_candidates(metadata).get("themes", [])
    if not themes:
        return "core course concepts"
    step, total = _week_progress_step(label)
    if total <= 1:
        return themes[0]
    idx = round((step - 1) * (len(themes) - 1) / (total - 1))
    return themes[idx]


def _build_progressive_topics(label, course_title, metadata=None):
    theme = _pick_progressive_theme(metadata or {"course_title": course_title}, label)
    return [
        f"Core concept focus in {course_title}: {theme}",
        f"Definition, scope, and foundational principles of {theme}",
        f"Guided analysis of {theme} using examples, use cases, or scenarios",
        f"Applied task or output development for weekly competency building in {theme}",
    ]


def _select_progressive_entries(entries, label, count=2):
    items = [item for item in (entries or []) if str(item).strip()]
    if not items:
        return []
    if len(items) <= count:
        return items
    step, _ = _week_progress_step(label)
    start = (step - 1) % len(items)
    selected = []
    for offset in range(count):
        selected.append(items[(start + offset) % len(items)])
    return selected


def _looks_like_url(value):
    text = str(value or "").strip().lower()
    return bool(re.search(r"https?://|www\\.", text))


def _looks_like_citation(value):
    text = str(value or "").strip()
    return bool(re.search(r"\b(19|20)\d{2}\b", text)) and len(text.split()) >= 5


def _has_real_reference_entries(textbook, website, journal):
    website_ok = any(_looks_like_url(item) for item in (website or []))
    scholarly_ok = any(_looks_like_citation(item) for item in (textbook or [])) or any(
        _looks_like_citation(item) for item in (journal or [])
    )
    return website_ok and scholarly_ok


def _mentions_week_label(value):
    text = str(value or "").strip().lower()
    return bool(re.search(r"\bweek\s*\d", text))


def _extract_clo_codes_from_text(value):
    matches = re.findall(r"\bCLO\s*(\d+)\b", str(value or ""), flags=re.I)
    return [f"CLO {match}" for match in matches]


def _looks_generic_ilo_phrase(value):
    text = str(value or "").strip().lower()
    generic_patterns = [
        "perform a guided task aligned with clo",
        "perform a guided task aligned with",
        "apply course concepts in a guided activity",
        "produce a weekly output or exercise",
        "complete a simple output or exercise",
        "aligned with clo x",
    ]
    return any(pattern in text for pattern in generic_patterns)


def _build_even_progressive_mapped_clos(clo_rows, label):
    clo_codes = [
        str(row.get("clo_code") or "").strip()
        for row in (clo_rows or [])
        if isinstance(row, dict) and str(row.get("clo_code") or "").strip()
    ]
    if not clo_codes:
        return ["CLO 1"]
    if _is_orientation_week(label):
        return ["CLO 1"]

    step, total = _week_progress_step(label)
    if len(clo_codes) == 1 or total <= 1:
        return [clo_codes[0]]

    # Evenly spread CLO targets from early to late weeks.
    primary_index = round((step - 1) * (len(clo_codes) - 1) / (total - 1))
    secondary_index = min(primary_index + 1, len(clo_codes) - 1)
    mapped = [clo_codes[primary_index]]
    if secondary_index != primary_index:
        mapped.append(clo_codes[secondary_index])
    return mapped


def _fallback_clo_statement(course_title, domain, index, total):
    cognitive_templates = [
        "Explain the foundational concepts, scope, and principles of {course_title}.",
        "Analyze key elements, requirements, and issues related to {course_title}.",
        "Evaluate methods, tools, or frameworks commonly used in {course_title}.",
        "Synthesize course concepts to address authentic issues in {course_title}.",
        "Justify decisions and recommendations using evidence from {course_title}.",
    ]
    affective_templates = [
        "Demonstrate ethical responsibility and professionalism in tasks related to {course_title}.",
        "Value collaboration, inclusivity, and stakeholder needs in activities involving {course_title}.",
        "Show reflective awareness of social, cultural, and sustainability concerns in {course_title}.",
        "Advocate responsible participation and civic-minded action through {course_title}.",
    ]
    psychomotor_templates = [
        "Apply appropriate techniques and tools in practical activities related to {course_title}.",
        "Develop outputs, prototypes, or solutions aligned with the requirements of {course_title}.",
        "Assess outputs, processes, or solutions in {course_title} using relevant criteria.",
        "Present evidence-based outputs that demonstrate competent practice in {course_title}.",
    ]
    banks = {
        "cognitive": cognitive_templates,
        "affective": affective_templates,
        "psychomotor": psychomotor_templates,
    }
    templates = banks.get(domain, cognitive_templates)
    statement = templates[(max(index, 1) - 1) % len(templates)].format(course_title=course_title)
    if total > 8 and index == total:
        statement = f"Integrate the major concepts, values, and applications of {course_title} in a culminating learning output."
    return statement


def _build_fallback_clo_rows(metadata, clo_blueprints=None):
    course_title = str(metadata.get("course_title") or "the course").strip() or "the course"
    blueprints = clo_blueprints if isinstance(clo_blueprints, list) and clo_blueprints else [
        {"index": index, "code": code, "domain": domain}
        for index, (code, domain) in enumerate(DEFAULT_CLO_ROWS, start=1)
    ]
    total = len(blueprints)
    return [
        {
            "clo_code": str(blueprint.get("code") or f"CLO {index}").strip(),
            "domain": domain,
            "clo_statement": _fallback_clo_statement(course_title, domain, index, total),
        }
        for index, blueprint in enumerate(blueprints, start=1)
        for domain in [_normalize_clo_domain(blueprint.get("domain"), "cognitive")]
    ]


def _choose_present(options, candidates, fallback_count=1):
    selected = [item for item in candidates if item in options]
    if selected:
        return selected
    return list(options[:fallback_count]) if options else []


def _serialize_line_values(values):
    return "\n".join(str(item).strip() for item in values or [] if str(item).strip())


def serialize_copilot_program_outcomes(program_outcomes):
    lines = []
    for row in program_outcomes or []:
        code = str((row or {}).get("code") or "").strip()
        description = str((row or {}).get("description") or "").strip()
        if code and description:
            lines.append(f"{code} | {description}")
    return "\n".join(lines)


def serialize_copilot_sdg_context(context):
    lines = []
    for code in SDG_OPTIONS:
        guidance = str(((context or {}).get(code) or {}).get("guidance") or "").strip()
        if guidance:
            lines.append(f"{code} | {guidance}")
    return "\n".join(lines)


def _program_outcomes_seed_rows(local_supabase=None):
    client = local_supabase or supabase
    try:
        result = client.table("program_outcomes").select("code, description").order("code").execute()
        rows = result.data or []
    except Exception:
        rows = []
    seen = set()
    normalized = []
    for row in rows:
        code = str((row or {}).get("code") or "").strip()
        description = str((row or {}).get("description") or "").strip()
        if not code or not description or code in seen:
            continue
        seen.add(code)
        normalized.append({"code": code, "description": description})
    return normalized


def get_copilot_data_setting_defaults(local_supabase=None):
    return {
        COPILOT_PROGRAM_OUTCOMES_KEY: serialize_copilot_program_outcomes(_program_outcomes_seed_rows(local_supabase=local_supabase)),
        COPILOT_GRADUATE_ATTRIBUTES_KEY: _serialize_line_values(SGA_OPTIONS),
        COPILOT_CORE_VALUES_KEY: _serialize_line_values(CORE_VALUE_OPTIONS),
        COPILOT_PQF_OPTIONS_KEY: _serialize_line_values(PQF_LEVEL_6_OPTIONS),
        COPILOT_AQRF_OPTIONS_KEY: _serialize_line_values(AQRF_LEVEL_6_OPTIONS),
        COPILOT_SDG_OPTIONS_KEY: _serialize_line_values(SDG_OPTIONS),
        COPILOT_SDG_CONTEXT_KEY: serialize_copilot_sdg_context(SDG_CONTEXT),
    }


def _parse_multiline_option_setting(raw_value, label, require_values=False):
    parsed = [str(item).strip() for item in str(raw_value or "").splitlines() if str(item).strip()]
    if require_values and not parsed:
        raise ValueError(f"{label} is empty. Ask the admin to configure it in Admin Settings > Production Copilot Data.")
    return parsed


def _parse_program_outcomes_setting(raw_value, require_values=False):
    rows = []
    seen_codes = set()
    for line_number, line in enumerate(str(raw_value or "").splitlines(), start=1):
        cleaned = str(line or "").strip()
        if not cleaned:
            continue
        if "|" not in cleaned:
            raise ValueError(
                f"Program Outcomes for Copilot line {line_number} must use `CODE | Description` format."
            )
        code, description = [part.strip() for part in cleaned.split("|", 1)]
        if not code or not description:
            raise ValueError(
                f"Program Outcomes for Copilot line {line_number} must include both a code and a description."
            )
        if code in seen_codes:
            raise ValueError(f"Program Outcomes for Copilot contains a duplicate code: {code}.")
        seen_codes.add(code)
        rows.append({"code": code, "description": description})
    if require_values and not rows:
        raise ValueError(
            "Program Outcomes for Copilot is empty. Ask the admin to configure it in Admin Settings > Production Copilot Data."
        )
    return rows


def _parse_sdg_context_setting(raw_value, allowed_codes, strict=False):
    default_titles = {code: details.get("title", code) for code, details in SDG_CONTEXT.items()}
    parsed = {}
    for line_number, line in enumerate(str(raw_value or "").splitlines(), start=1):
        cleaned = str(line or "").strip()
        if not cleaned:
            continue
        if "|" not in cleaned:
            raise ValueError(
                f"SDG Guidance line {line_number} must use `SDG CODE | guidance text` format."
            )
        code, guidance = [part.strip() for part in cleaned.split("|", 1)]
        if not code or not guidance:
            raise ValueError(
                f"SDG Guidance line {line_number} must include both an SDG code and guidance text."
            )
        if code not in allowed_codes:
            raise ValueError(f"SDG Guidance references an SDG code that is not allowed: {code}.")
        parsed[code] = {"title": default_titles.get(code, code), "guidance": guidance}

    missing_codes = [code for code in allowed_codes if code not in parsed]
    if strict and missing_codes:
        raise ValueError(
            "SDG Guidance is missing entries for: " + ", ".join(missing_codes) + "."
        )
    for code in missing_codes:
        parsed[code] = deepcopy(SDG_CONTEXT.get(code, {"title": code, "guidance": ""}))
    return parsed


def _normalize_limited_selection(value, allowed_options, fallback_values=None, minimum=ALIGNMENT_MIN_ITEMS, maximum=ALIGNMENT_MAX_ITEMS):
    fallback_values = fallback_values or []
    allowed_options = list(allowed_options or [])
    allow_any = not allowed_options
    seen = set()
    normalized = []
    for item in _csv_or_lines_to_list(value):
        if (allow_any or item in allowed_options) and item not in seen:
            seen.add(item)
            normalized.append(item)
    for item in fallback_values:
        if len(normalized) >= minimum:
            break
        if (allow_any or item in allowed_options) and item not in seen:
            seen.add(item)
            normalized.append(item)
    if len(normalized) < minimum:
        for item in allowed_options:
            if item not in seen:
                seen.add(item)
                normalized.append(item)
            if len(normalized) >= minimum:
                break
    return normalized if maximum <= 0 else normalized[:maximum]


def _preferred_alignment_minimum(field_name, row, allowed_options, fallback_values=None, program_outcomes=None, metadata=None):
    fallback_values = fallback_values or []
    if len(allowed_options or []) <= 1:
        return ALIGNMENT_MIN_ITEMS
    if field_name == "aligned_plos":
        ranked_plos = _rank_plo_candidates_for_clo(row or {}, program_outcomes or [], metadata=metadata)
        return ALIGNMENT_MIN_ITEMS if len(ranked_plos) < ALIGNMENT_PREFERRED_MIN_ITEMS else ALIGNMENT_PREFERRED_MIN_ITEMS
    available = _unique_keep_order(_csv_or_lines_to_list(fallback_values))
    return ALIGNMENT_MIN_ITEMS if len(available) <= 1 else ALIGNMENT_PREFERRED_MIN_ITEMS


def _normalize_preferred_alignment_selection(value, allowed_options, fallback_values=None, field_name=None, row=None, program_outcomes=None, metadata=None):
    fallback_values = fallback_values or []
    preferred_minimum = _preferred_alignment_minimum(
        field_name or "",
        row or {},
        allowed_options,
        fallback_values=fallback_values,
        program_outcomes=program_outcomes,
        metadata=metadata,
    )
    normalized = _normalize_limited_selection(
        value,
        allowed_options,
        fallback_values=fallback_values,
        minimum=preferred_minimum,
        maximum=ALIGNMENT_MAX_ITEMS,
    )
    if normalized:
        return normalized
    if field_name == "aligned_plos":
        raw_values = _unique_keep_order(_csv_or_lines_to_list(value))
        if raw_values:
            return raw_values if ALIGNMENT_MAX_ITEMS <= 0 else raw_values[:ALIGNMENT_MAX_ITEMS]
    return _normalize_limited_selection(
        fallback_values,
        allowed_options,
        fallback_values=fallback_values,
        minimum=ALIGNMENT_MIN_ITEMS,
        maximum=ALIGNMENT_MAX_ITEMS,
    )


def _infer_relevant_sdgs(clo_statement, metadata, context):
    text = " ".join(
        [
            str(metadata.get("course_title") or ""),
            str(metadata.get("course_description") or ""),
            str(metadata.get("service_learning_component") or ""),
            str(clo_statement or ""),
        ]
    ).lower()
    matched = []
    rules = [
        ("SDG 4", ["learn", "education", "teaching", "student", "literacy", "training", "instruction"]),
        ("SDG 8", ["industry", "professional", "work", "career", "employment", "productivity", "entrepreneur"]),
        ("SDG 9", ["system", "technology", "innovation", "prototype", "design", "interface", "digital", "infrastructure"]),
        ("SDG 16", ["ethical", "ethics", "security", "accountability", "fairness", "governance", "integrity", "trust"]),
        ("SDG 17", ["collaboration", "partnership", "community", "stakeholder", "interdisciplinary", "coordination"]),
    ]
    for sdg_code, keywords in rules:
        if sdg_code in context["sdg_options"] and any(keyword in text for keyword in keywords):
            matched.append(sdg_code)
    if matched:
        return matched
    return _choose_present(context["sdg_options"], ["SDG 4", "SDG 9"], fallback_count=min(2, len(context["sdg_options"])))


def _build_fallback_alignment_rows(content, allowed_plos, context):
    rows = []
    clo_defaults = {
        "CLO 1": {
            "graduate_attributes": ["Critical Thinkers", "Industry Competent"],
            "core_values": ["Excellence", "Integrity"],
            "pqf_level_6_alignment": ["PQF1", "PQF2", "PQF3"],
            "aqrf_level_6_alignment": ["AQRF1", "AQRF2"],
            "relevant_sdgs": ["SDG 4", "SDG 16"],
        },
        "CLO 2": {
            "graduate_attributes": ["Critical Thinkers", "Industry Competent"],
            "core_values": ["Excellence", "Integrity"],
            "pqf_level_6_alignment": ["PQF2", "PQF3", "PQF4"],
            "aqrf_level_6_alignment": ["AQRF2", "AQRF3"],
            "relevant_sdgs": ["SDG 9", "SDG 16"],
        },
        "CLO 3": {
            "graduate_attributes": ["Critical Thinkers", "Research-Oriented"],
            "core_values": ["Excellence", "Integrity"],
            "pqf_level_6_alignment": ["PQF3", "PQF4", "PQF5"],
            "aqrf_level_6_alignment": ["AQRF3", "AQRF4"],
            "relevant_sdgs": ["SDG 8", "SDG 9"],
        },
        "CLO 4": {
            "graduate_attributes": ["Holistic Persons", "Reconcilers"],
            "core_values": ["Integrity", "Faith", "Reconciliation"],
            "pqf_level_6_alignment": ["PQF4", "PQF5", "PQF9"],
            "aqrf_level_6_alignment": ["AQRF4", "AQRF5"],
            "relevant_sdgs": ["SDG 16", "SDG 17"],
        },
        "CLO 5": {
            "graduate_attributes": ["Holistic Persons", "Transformative Leaders"],
            "core_values": ["Solidarity", "Integrity", "Excellence"],
            "pqf_level_6_alignment": ["PQF5", "PQF6", "PQF9"],
            "aqrf_level_6_alignment": ["AQRF5", "AQRF6"],
            "relevant_sdgs": ["SDG 4", "SDG 17"],
        },
        "CLO 6": {
            "graduate_attributes": ["Industry Competent", "Information and Communication Technology Proficient"],
            "core_values": ["Excellence", "Integrity"],
            "pqf_level_6_alignment": ["PQF10", "PQF11"],
            "aqrf_level_6_alignment": ["AQRF7", "AQRF8"],
            "relevant_sdgs": ["SDG 8", "SDG 9"],
        },
        "CLO 7": {
            "graduate_attributes": ["Industry Competent", "Information and Communication Technology Proficient"],
            "core_values": ["Excellence", "Solidarity"],
            "pqf_level_6_alignment": ["PQF11", "PQF12"],
            "aqrf_level_6_alignment": ["AQRF8", "AQRF9"],
            "relevant_sdgs": ["SDG 9", "SDG 16"],
        },
        "CLO 8": {
            "graduate_attributes": ["Industry Competent", "Research-Oriented"],
            "core_values": ["Excellence", "Integrity"],
            "pqf_level_6_alignment": ["PQF10", "PQF12", "PQF13"],
            "aqrf_level_6_alignment": ["AQRF7", "AQRF9"],
            "relevant_sdgs": ["SDG 8", "SDG 16"],
        },
    }
    for row in content["clo_alignment_table"]:
        defaults = clo_defaults.get(row.get("clo_code"), clo_defaults["CLO 1"])
        clo_text = str(row.get("clo_statement") or "")
        inferred_sdgs = _infer_relevant_sdgs(clo_text, content["metadata"], context)
        blended_sdgs = list(dict.fromkeys((inferred_sdgs or []) + defaults["relevant_sdgs"]))
        rows.append({
            "clo_code": row.get("clo_code", ""),
            "aligned_plos": _normalize_limited_selection(
                allowed_plos if ALIGNMENT_MAX_ITEMS <= 0 else allowed_plos[:ALIGNMENT_MAX_ITEMS],
                allowed_plos, fallback_values=[], minimum=0),
            "graduate_attributes": _normalize_limited_selection(defaults["graduate_attributes"], context["sga_options"], fallback_values=defaults["graduate_attributes"]),
            "core_values": _normalize_limited_selection(defaults["core_values"], context["core_value_options"], fallback_values=defaults["core_values"]),
            "pqf_level_6_alignment": _normalize_limited_selection(defaults["pqf_level_6_alignment"], context["pqf_level_6_options"], fallback_values=defaults["pqf_level_6_alignment"]),
            "aqrf_level_6_alignment": _normalize_limited_selection(defaults["aqrf_level_6_alignment"], context["aqrf_level_6_options"], fallback_values=defaults["aqrf_level_6_alignment"]),
            "relevant_sdgs": _normalize_limited_selection(blended_sdgs, context["sdg_options"], fallback_values=defaults["relevant_sdgs"]),
        })
    return rows


def _ranked_plo_fallbacks(row, allowed_plos, program_outcomes=None, metadata=None):
    ranked_codes = [code for _, code in _rank_plo_candidates_for_clo(row or {}, program_outcomes or [], metadata=metadata) if code in (allowed_plos or [])]
    if ranked_codes:
        return ranked_codes if ALIGNMENT_MAX_ITEMS <= 0 else ranked_codes[:ALIGNMENT_MAX_ITEMS]
    return list(allowed_plos or []) if ALIGNMENT_MAX_ITEMS <= 0 else list((allowed_plos or [])[:ALIGNMENT_MAX_ITEMS])


def _sanitize_alignment_selection(value, allowed_options):
    seen = set()
    normalized = []
    for item in _csv_or_lines_to_list(value):
        if item in allowed_options and item not in seen:
            seen.add(item)
            normalized.append(item)
    return normalized


def _collect_alignment_regeneration_issues(rows):
    issues = []
    field_labels = {
        "aligned_plos": "aligned PLOs",
        "graduate_attributes": "graduate attributes",
        "core_values": "core values",
        "pqf_level_6_alignment": "PQF Level 6 alignment",
        "aqrf_level_6_alignment": "AQRF Level 6 alignment",
        "relevant_sdgs": "relevant SDGs",
    }

    for field_name, label in field_labels.items():
        grouped = {}
        for row in rows:
            group_key = row.get("domain", "__all__")
            value_key = tuple(_csv_or_lines_to_list(row.get(field_name, [])))
            if not value_key:
                continue
            grouped.setdefault((group_key, value_key), []).append(row.get("clo_code", "Unknown CLO"))
        for (domain, value_key), clo_codes in grouped.items():
            if len(clo_codes) > 1:
                issues.append(
                    f"{label} are repeated for {', '.join(clo_codes)} within the {domain} domain: {', '.join(value_key)}."
                )

    return issues


def _program_outcome_text(outcome):
    if not isinstance(outcome, dict):
        return ""
    return " ".join(
        [
            str(outcome.get("code") or ""),
            str(outcome.get("description") or ""),
            str(outcome.get("outcome_statement") or ""),
            str(outcome.get("statement") or ""),
            str(outcome.get("name") or ""),
        ]
    ).strip()


def _rank_plo_candidates_for_clo(row, program_outcomes, metadata=None):
    clo_signal = " ".join(
        [
            str(row.get("clo_statement") or ""),
            str((metadata or {}).get("course_title") or ""),
            str((metadata or {}).get("course_description") or ""),
        ]
    )
    clo_tokens = _keyword_tokens(clo_signal)
    ranked = []
    for outcome in program_outcomes or []:
        code = str((outcome or {}).get("code") or "").strip()
        if not code:
            continue
        outcome_tokens = _keyword_tokens(_program_outcome_text(outcome))
        overlap = clo_tokens & outcome_tokens
        if not overlap:
            continue
        ranked.append((len(overlap), code))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked


def _singleton_alignment_allowed(field_name, row, program_outcomes=None, metadata=None):
    values = _csv_or_lines_to_list(row.get(field_name, []))
    if len(values) != 1:
        return False
    if field_name != "aligned_plos":
        return False
    ranked_plos = _rank_plo_candidates_for_clo(row, program_outcomes or [], metadata=metadata)
    return len(ranked_plos) < ALIGNMENT_PREFERRED_MIN_ITEMS


def _collect_alignment_breadth_issues(content, program_outcomes, context):
    rows = content.get("clo_alignment_table") if isinstance(content.get("clo_alignment_table"), list) else []
    if not rows:
        return []
    issues = []
    field_labels = {
        "aligned_plos": "aligned PLOs",
        "graduate_attributes": "graduate attributes",
        "core_values": "core values",
        "pqf_level_6_alignment": "PQF Level 6 alignment",
        "aqrf_level_6_alignment": "AQRF Level 6 alignment",
        "relevant_sdgs": "relevant SDGs",
    }
    metadata = content.get("metadata") if isinstance(content.get("metadata"), dict) else {}
    for row in rows:
        clo_code = row.get("clo_code", "Unknown CLO")
        clo_statement = str(row.get("clo_statement") or "").strip() or "the CLO statement"
        for field_name, label in field_labels.items():
            values = _csv_or_lines_to_list(row.get(field_name, []))
            if len(values) >= ALIGNMENT_PREFERRED_MIN_ITEMS:
                continue
            if _singleton_alignment_allowed(field_name, row, program_outcomes=program_outcomes, metadata=metadata):
                continue
            issues.append(
                f"{clo_code} should usually have at least 2 {label} because the CLO statement "
                f"('{clo_statement}') supports more than one defensible approved match; keep only 1 only when no second approved match is genuinely aligned."
            )
    return issues


def _repair_alignment_repetition_with_ai(content, allowed_plos, context, issues):
    metadata = content.get("metadata", {})
    prompt = f"""
{BETA_ALIGNMENT_REPAIR_PROMPT}

COURSE METADATA:
{json.dumps(metadata)}

CURRENT CLO ROWS:
{json.dumps(content.get("clo_alignment_table", []))}

APPROVED PLO CODES:
{json.dumps(allowed_plos)}

APPROVED GRADUATE ATTRIBUTES:
{json.dumps(context["sga_options"])}

GRADUATE ATTRIBUTE MEANINGS:
{json.dumps(context.get("sga_details", []))}

APPROVED CORE VALUES:
{json.dumps(context["core_value_options"])}

CORE VALUE MEANINGS:
{json.dumps(context.get("core_value_details", []))}

APPROVED PQF CODES:
{json.dumps(context["pqf_level_6_options"])}

PQF LEVEL 6 DETAILS:
{json.dumps(context.get("pqf_level_6_details", []))}

APPROVED AQRF CODES:
{json.dumps(context["aqrf_level_6_options"])}

AQRF LEVEL 6 DETAILS:
{json.dumps(context.get("aqrf_level_6_details", []))}

APPROVED SDG CODES:
{json.dumps(context["sdg_options"])}

COURSE-LEVEL TARGET SDGS:
{json.dumps(_csv_or_lines_to_list(metadata.get("target_sdgs_display", [])))}

{_generation_shape_contract_text(content)}

ALIGNMENT ISSUES TO FIX:
{json.dumps(issues)}
""".strip()
    return _generate_json(prompt, "copilot_beta_alignment")


def _rebalance_alignment_repetition(content, allowed_plos, context, program_outcomes=None):
    rows = content.get("clo_alignment_table") if isinstance(content.get("clo_alignment_table"), list) else []
    if not rows:
        return content

    issues = _collect_alignment_regeneration_issues(rows)
    issues.extend(_collect_alignment_breadth_issues(content, program_outcomes or [], context))
    issues = _unique_keep_order(issues)
    if not issues:
        return content

    try:
        repaired = _repair_alignment_repetition_with_ai(content, allowed_plos, context, issues)
    except ValueError as exc:
        current_app.logger.warning("AI alignment repair failed; preserving generated rows: %s", exc)
        _record_validation_warning(
            content,
            "AI alignment repair failed after the main alignment generation, so the generated alignment rows were preserved for review.",
        )
        return content
    repaired_rows = _normalize_ai_row_list(
        repaired,
        ["clo_alignment_table", "alignment_rows", "clo_rows", "rows", "mappings"],
    )
    if not repaired_rows:
        _record_validation_warning(content, "AI alignment repair could not remove repetitive patterns, so the original AI output was preserved.")
        return content
    fallback_rows = _build_fallback_alignment_rows(content, allowed_plos, context)
    metadata = content.get("metadata", {}) if isinstance(content.get("metadata"), dict) else {}
    repaired_by_code = {
        str(row.get("clo_code") or row.get("code") or row.get("outcome_code") or "").strip(): row
        for row in repaired_rows
        if isinstance(row, dict)
        and str(row.get("clo_code") or row.get("code") or row.get("outcome_code") or "").strip()
    }

    for idx, row in enumerate(rows):
        row_code = str(row.get("clo_code") or "").strip()
        source = repaired_by_code.get(row_code) or (repaired_rows[idx] if idx < len(repaired_rows) and isinstance(repaired_rows[idx], dict) else {})
        fallback_row = fallback_rows[idx] if idx < len(fallback_rows) else {}
        row["aligned_plos"] = _normalize_preferred_alignment_selection(
            source.get("aligned_plos", row.get("aligned_plos", [])),
            allowed_plos,
            fallback_values=_ranked_plo_fallbacks(row, allowed_plos, program_outcomes=program_outcomes, metadata=metadata),
            field_name="aligned_plos",
            row=row,
            program_outcomes=program_outcomes,
            metadata=metadata,
        )
        row["graduate_attributes"] = _normalize_preferred_alignment_selection(
            source.get("graduate_attributes", row.get("graduate_attributes", [])),
            context["sga_options"],
            fallback_values=fallback_row.get("graduate_attributes", []),
            field_name="graduate_attributes",
            row=row,
            program_outcomes=program_outcomes,
            metadata=metadata,
        )
        row["core_values"] = _normalize_preferred_alignment_selection(
            source.get("core_values", row.get("core_values", [])),
            context["core_value_options"],
            fallback_values=fallback_row.get("core_values", []),
            field_name="core_values",
            row=row,
            program_outcomes=program_outcomes,
            metadata=metadata,
        )
        row["pqf_level_6_alignment"] = _normalize_preferred_alignment_selection(
            source.get("pqf_level_6_alignment", row.get("pqf_level_6_alignment", [])),
            context["pqf_level_6_options"],
            fallback_values=fallback_row.get("pqf_level_6_alignment", []),
            field_name="pqf_level_6_alignment",
            row=row,
            program_outcomes=program_outcomes,
            metadata=metadata,
        )
        row["aqrf_level_6_alignment"] = _normalize_preferred_alignment_selection(
            source.get("aqrf_level_6_alignment", row.get("aqrf_level_6_alignment", [])),
            context["aqrf_level_6_options"],
            fallback_values=fallback_row.get("aqrf_level_6_alignment", []),
            field_name="aqrf_level_6_alignment",
            row=row,
            program_outcomes=program_outcomes,
            metadata=metadata,
        )
        row["relevant_sdgs"] = _normalize_preferred_alignment_selection(
            source.get("relevant_sdgs", row.get("relevant_sdgs", [])),
            context["sdg_options"],
            fallback_values=fallback_row.get("relevant_sdgs", []),
            field_name="relevant_sdgs",
            row=row,
            program_outcomes=program_outcomes,
            metadata=metadata,
        )

    suggested_target_sdgs = _normalize_limited_selection(
        repaired.get("target_sdgs_display", []) if isinstance(repaired, dict) else [],
        context["sdg_options"],
        fallback_values=[],
        minimum=0,
        maximum=ALIGNMENT_MAX_ITEMS,
    )
    if suggested_target_sdgs:
        content["metadata"]["target_sdgs_display"] = ", ".join(suggested_target_sdgs)

    return content


def _get_institution_config():
    try:
        settings = get_system_settings_map()
    except Exception:
        settings = {}
    return {
        "name": str(settings.get("institution_name") or "University of La Salette").strip(),
        "acronym": str(settings.get("institution_acronym") or "ULS").strip(),
        "website": str(settings.get("institution_website") or "https://uls.edu.ph").strip(),
        "program": str(settings.get("program_name") or "BSIT").strip(),
        "lms": str(settings.get("lms_name") or "ULS CLMS").strip(),
    }


def _build_fallback_week_row(content, label):
    clo_rows = content.get("clo_alignment_table") or []
    metadata = content.get("metadata") or {}
    course_title = str(metadata.get("course_title") or "the course").strip() or "the course"
    if _is_orientation_week(label):
        inst = _get_institution_config()
        available_codes = [row.get("clo_code") for row in clo_rows if isinstance(row, dict) and row.get("clo_code")]
        return {
            "time_frame_label": label,
            "mapped_clos": _choose_present(available_codes, ["CLO 1", "CLO 4", "CLO 6"], fallback_count=min(3, len(available_codes))) or ["CLO 1"],
            "intended_learning_outcomes": {
                "lead_in": "At the end of the week, students should have the ability to:",
                "cognitive": [
                    f"Explain the {inst['name']} vision, mission, core values, core competencies, institutional objectives, and outcomes.",
                    f"Relate the {inst['program']} program learning outcomes to the course learning outcomes of {course_title}.",
                    f"Identify the relevance of {course_title} in the field of Information Technology and society.",
                ],
                "affective": [
                    "Demonstrate respect for institutional policies, academic integrity, and responsible use of technology.",
                    f"Appreciate the importance of user-centered thinking and ethical responsibility in the study of {course_title}.",
                ],
                "psychomotor": [
                    f"Navigate the university's online learning management system ({inst['lms']}) to access course materials, policies, and learning activities.",
                ],
            },
            "topics": [
                "Course Orientation",
                f"{inst['name']} vision, mission, core values, core competencies, and institutional objectives",
                f"{inst['program']} Program Learning Outcomes",
                f"Course overview: {course_title}",
                "Course policies and requirements",
            ],
            "teaching_learning_activities": {
                "lecture": [
                    "Face-to-face lecture and discussion",
                    "Orientation and presentation of course syllabus",
                    f"Demonstration of the {inst['lms']} platform",
                    f"Guided discussion on the role of {course_title} in Information Technology",
                ],
                "practical_session": [
                    f"Guided navigation of course materials, policies, and learning activities in the {inst['lms']}",
                ],
                "other": [],
            },
            "assessment": [
                f"Recitation on the university's vision, mission, core values, and institutional objectives",
                "Short quiz on university and course policies",
                f"Reflective essay on the role of {course_title} in creating user-centered and ethical technology",
                f"Career reflection aligning {inst['program']} program outcomes with {course_title} competencies",
            ],
            "learning_resources": {
                "clms": ["Student Handbook"],
                "textbook": [],
                "website": [
                    "CHED CMO 25, series 2015 \"PSG for IT Education\"",
                    "Curriculum Guidelines for Baccalaureate Degree Programs in Information Technology (IT2017) of ACM and IEEE-CS",
                    f"{inst['acronym']} Official Website",
                    inst["website"],
                ],
                "journal": [],
                "other": [],
            },
        }
    mapped_clos = _build_even_progressive_mapped_clos(clo_rows, label)
    reference_pack = _course_reference_candidates(metadata)
    topic_lines = _build_progressive_topics(label, course_title, metadata)
    if clo_rows:
        current_labels = [
            row.get("time_frame_label")
            for row in content.get("weekly_course_outline", [])
            if isinstance(row, dict) and row.get("time_frame_label")
        ]
        offset = (current_labels.index(label) if label in current_labels else 0) % len(clo_rows)
        primary = clo_rows[offset]
        cognitive = [
            f"Analyze the key concept addressed by {primary.get('clo_statement') or course_title}.",
            f"Explain how the lesson focus supports progression toward {primary.get('clo_code') or 'CLO 1'}.",
        ]
        affective = [
            f"Value responsible, collaborative, and learner-centered practice in {course_title}.",
            f"Show openness to feedback and continuous improvement during the weekly learning tasks.",
        ]
        psychomotor = [
            f"Perform a guided task aligned with {primary.get('clo_code') or 'CLO 1'}.",
            f"Produce a weekly output, worksheet, or applied task related to {course_title}.",
        ]
    else:
        cognitive = [
            f"Explain key concepts related to {course_title}.",
            "Identify the main ideas that support the lesson focus and expected competencies.",
        ]
        affective = [
            f"Show appreciation for the relevance of {course_title} in practice.",
            f"Demonstrate professionalism and active participation during the lesson.",
        ]
        psychomotor = [
            f"Apply course concepts in a guided activity related to {course_title}.",
            f"Complete a simple output or exercise based on the weekly topic.",
        ]
    topics = topic_lines
    is_exam_week = _is_exam_week(label)
    is_pre_exam_week = _is_pre_exam_week(label)
    if is_exam_week:
        assessments = [
            "Structured review quiz or readiness check",
            "Consultation-based correction of common errors and misconceptions",
            "Major examination, practical test, or integrative performance output",
        ]
        lecture = [
            "Guided review and synthesis session on previously covered competencies.",
            "Facilitated walkthrough of recurring errors, rubric criteria, and expected performance levels.",
        ]
        practical_session = [
            f"Applied review activity, consultation, or examination preparation for {course_title}.",
            "Timed or scaffolded dry-run task aligned with the grading criteria.",
        ]
        resources = {
            "clms": [
                f"CLMS review module on {topic_lines[0]}",
                "Exam instructions, rubric, and submission page in the CLMS",
            ],
            "textbook": _select_progressive_entries(reference_pack.get("textbook", []), label, count=2),
            "website": _select_progressive_entries(reference_pack.get("website", []), label, count=2),
            "journal": _select_progressive_entries(reference_pack.get("journal", []), label, count=1),
            "other": _select_progressive_entries(reference_pack.get("other", []), label, count=1)
            or ["Consultation checklist, review worksheet, or exam coverage guide"],
        }
    elif is_pre_exam_week:
        assessments = [
            "Reviewer-based formative quiz on key concepts and recurring problem types",
            "Synthesis worksheet or concept map covering the upcoming examination scope",
            "Mock problem set, case drill, or practice performance task with feedback",
        ]
        lecture = [
            "Structured review and synthesis session covering the major competencies leading into the examination.",
            "Guided discussion of common errors, likely assessment patterns, and strategies for accurate performance.",
            "Faculty-led consolidation of concepts, frameworks, and applied procedures from preceding lessons.",
        ]
        practical_session = [
            f"Reviewer workshop, consultation, or guided drill aligned with {course_title}.",
            "Timed practice task, mock quiz, or scenario-based rehearsal using the exam coverage.",
            "Peer checking and refinement of reviewer notes, worked examples, or readiness outputs.",
        ]
        resources = {
            "clms": [
                f"CLMS reviewer packet and synthesis slides for {topic_lines[0]}",
                "Exam coverage guide, practice set, and consultation task in the CLMS",
            ],
            "textbook": _select_progressive_entries(reference_pack.get("textbook", []), label, count=2),
            "website": _select_progressive_entries(reference_pack.get("website", []), label, count=2),
            "journal": _select_progressive_entries(reference_pack.get("journal", []), label, count=1),
            "other": _select_progressive_entries(reference_pack.get("other", []), label, count=1)
            or ["Reviewer packet, mock quiz set, consultation notes, or synthesis worksheet"],
        }
    else:
        assessments = [
            "Short quiz on the lesson concept and its practical implications",
            "Performance check through class participation, guided recitation, or case response",
            "Submitted worksheet, timeline, mini-output, or applied task artifact",
        ]
        lecture = [
            "Focused discussion of the lesson concept and its practical significance.",
            f"Instructor-led explanation of the topic and its connection to course outcomes in {course_title}.",
            "Interactive slide-supported discussion with guided questioning and concept clarification.",
        ]
        practical_session = [
            f"Guided practice or workshop activity aligned to {course_title}.",
            "Short applied task, exercise, or output submission aligned with targeted competencies.",
            "Case-based or scenario-based analysis with feedback and revision.",
        ]
        resources = {
            "clms": [
                f"CLMS module and slide deck for {topic_lines[0]}",
                "Activity sheet, rubric, and submission task page in the CLMS",
            ],
            "textbook": _select_progressive_entries(reference_pack.get("textbook", []), label, count=2),
            "website": _select_progressive_entries(reference_pack.get("website", []), label, count=2),
            "journal": _select_progressive_entries(reference_pack.get("journal", []), label, count=1),
            "other": _select_progressive_entries(reference_pack.get("other", []), label, count=1)
            or ["Worksheet, case handout, software/tool guide, or lab file"],
        }
    return {
        "time_frame_label": label,
        "mapped_clos": mapped_clos,
        "intended_learning_outcomes": {
            "lead_in": "At the end of the week, students should have the ability to:",
            "cognitive": cognitive,
            "affective": affective,
            "psychomotor": psychomotor,
        },
        "topics": topics,
        "teaching_learning_activities": {
            "lecture": lecture,
            "practical_session": practical_session,
            "other": [
                "Short reflective or collaborative activity tied to the lesson focus.",
                "Feedback and refinement checkpoint based on rubric criteria.",
            ] if not (is_exam_week or is_pre_exam_week) else [
                "Consultation, reviewer refinement, and targeted remediation support session."
            ],
        },
        "assessment": assessments,
        "learning_resources": {
            "clms": resources["clms"],
            "textbook": resources["textbook"],
            "website": resources["website"],
            "journal": resources["journal"],
            "other": resources["other"],
        },
    }


def _looks_generic_weekly_phrase(value):
    text = str(value or "").strip().lower()
    if not text:
        return True
    generic_phrases = [
        "core lesson for",
        "short assessment for",
        "guided discussion and lecture",
        "applied activity aligned",
        "weekly concept focus",
        "course overview",
        "official course or discipline reference website",
        "peer-reviewed article aligned with the weekly concept",
        "recent scholarly source used for evidence-based reflection",
    ]
    return any(phrase in text for phrase in generic_phrases)


def _looks_generic_week1_phrase(value):
    text = str(value or "").strip().lower()
    if not text:
        return True
    generic_phrases = [
        "core lesson for",
        "short assessment for",
        "guided discussion and lecture",
        "applied activity aligned",
        "weekly concept focus",
        "official course or discipline reference website",
        "peer-reviewed article aligned with the weekly concept",
        "recent scholarly source used for evidence-based reflection",
    ]
    return any(phrase in text for phrase in generic_phrases)


def _is_exam_week(label):
    text = str(label or "").lower()
    return bool(re.search(r"\b(?:exam|examination|final test|prelim|midterm|finals?)\b", text))


def _is_pre_exam_week(label):
    text = str(label or "").lower()
    return bool(re.search(r"\b(?:review|synthesis|consultation|recap|integration|consolidation)\b", text))


def _is_assessment_function_row(row):
    """Return True for rows whose label OR assessment/topics content indicates
    an exam or review function. Checks the label first (fast path), then examines
    the assessment and topics fields for exam/review keywords. Avoids scanning
    the full narrative text to prevent false positives from incidental keywords.
    """
    if not isinstance(row, dict):
        return False
    label = row.get("time_frame_label", "")
    if _is_exam_week(label) or _is_pre_exam_week(label):
        return True
    # Check assessment and topics fields for exam indicators
    for field in ("assessment", "topics"):
        vals = row.get(field, [])
        if isinstance(vals, list):
            text = " ".join(str(v) for v in vals).lower()
        elif isinstance(vals, str):
            text = vals.lower()
        else:
            continue
        if re.search(r"\b(?:exam|examination|prelim|midterm|finals?|periodical)\b", text):
            return True
    return False


def _keyword_tokens(value):
    tokens = re.findall(r"[a-zA-Z]{4,}", str(value or "").lower())
    return {token for token in tokens if token not in WEEKLY_PROGRESS_STOPWORDS}


def _weekly_row_narrative_texts(row):
    ilo = row.get("intended_learning_outcomes", {}) if isinstance(row.get("intended_learning_outcomes"), dict) else {}
    tla = row.get("teaching_learning_activities", {}) if isinstance(row.get("teaching_learning_activities"), dict) else {}
    return (
        _csv_or_lines_to_list(ilo.get("cognitive", []))
        + _csv_or_lines_to_list(ilo.get("affective", []))
        + _csv_or_lines_to_list(ilo.get("psychomotor", []))
        + _csv_or_lines_to_list(row.get("topics", []))
        + _csv_or_lines_to_list(tla.get("lecture", []))
        + _csv_or_lines_to_list(tla.get("practical_session", []))
        + _csv_or_lines_to_list(tla.get("other", []))
        + _csv_or_lines_to_list(row.get("assessment", []))
    )


def _weekly_row_resource_texts(row):
    resources = row.get("learning_resources", {}) if isinstance(row.get("learning_resources"), dict) else {}
    return (
        _csv_or_lines_to_list(resources.get("clms", []))
        + _csv_or_lines_to_list(resources.get("textbook", []))
        + _csv_or_lines_to_list(resources.get("website", []))
        + _csv_or_lines_to_list(resources.get("journal", []))
        + _csv_or_lines_to_list(resources.get("other", []))
    )


def _resource_supports_weekly_sections(row):
    resources_tokens = _keyword_tokens(" ".join(_weekly_row_resource_texts(row)))
    if not resources_tokens:
        return False
    tla = row.get("teaching_learning_activities", {}) if isinstance(row.get("teaching_learning_activities"), dict) else {}
    sections = [
        _csv_or_lines_to_list(row.get("topics", [])),
        _csv_or_lines_to_list(tla.get("lecture", [])) + _csv_or_lines_to_list(tla.get("practical_session", [])) + _csv_or_lines_to_list(tla.get("other", [])),
        _csv_or_lines_to_list(row.get("assessment", [])),
    ]
    for section_items in sections:
        section_tokens = _keyword_tokens(" ".join(section_items))
        if section_tokens and not (section_tokens & resources_tokens):
            return False
    return True


def _has_minimum_resource_grounding_for_pre_exam(row):
    if not _is_pre_exam_week(row.get("time_frame_label", "")):
        return False

    resources = _weekly_row_resource_texts(row)
    if not any(str(item or "").strip() for item in resources):
        return False

    tla = row.get("teaching_learning_activities", {}) if isinstance(row.get("teaching_learning_activities"), dict) else {}
    review_signal_tokens = _keyword_tokens(
        " ".join(
            _csv_or_lines_to_list(row.get("topics", []))
            + _csv_or_lines_to_list(tla.get("lecture", []))
            + _csv_or_lines_to_list(tla.get("practical_session", []))
            + _csv_or_lines_to_list(tla.get("other", []))
            + _csv_or_lines_to_list(row.get("assessment", []))
            + resources
        )
    )
    return bool(review_signal_tokens & PRE_EXAM_REVIEW_TOKENS)


def _token_overlap_ratio(left, right):
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, len(left | right))


def _course_progression_keywords(metadata):
    reference_pack = _course_reference_candidates(metadata or {})
    combined = " ".join(
        [
            str((metadata or {}).get("course_title") or ""),
            str((metadata or {}).get("course_description") or ""),
            str((metadata or {}).get("service_learning_component") or ""),
            str((metadata or {}).get("source_context") or ""),
            " ".join(reference_pack.get("themes", [])),
        ]
    )
    return _keyword_tokens(combined)


def _weekly_row_progression_issues(row, metadata=None, previous_row=None):
    metadata = metadata or {}
    label = row.get("time_frame_label", "")
    if _is_orientation_week(label) or _is_assessment_function_row(row):
        return []

    issues = []
    narrative_tokens = _keyword_tokens(" ".join(_weekly_row_narrative_texts(row)))
    resource_tokens = _keyword_tokens(" ".join(_weekly_row_resource_texts(row)))
    course_tokens = _course_progression_keywords(metadata)

    if course_tokens and not ((narrative_tokens | resource_tokens) & course_tokens):
        issues.append("does not clearly advance the course-specific theme")
    if resource_tokens and not (narrative_tokens & resource_tokens):
        issues.append("does not align its topics and assessments with the listed resources")
    if not _is_orientation_week(label) and not _resource_supports_weekly_sections(row):
        if not _has_minimum_resource_grounding_for_pre_exam(row):
            issues.append("does not ground its topics, teaching-learning activities, and assessments in the listed resources")

    if _is_pre_exam_week(label):
        assessment_tokens = _keyword_tokens(" ".join(_csv_or_lines_to_list(row.get("assessment", []))))
        topic_tokens = _keyword_tokens(" ".join(_csv_or_lines_to_list(row.get("topics", []))))
        review_signal_tokens = narrative_tokens | resource_tokens | assessment_tokens | topic_tokens
        if not (review_signal_tokens & PRE_EXAM_REVIEW_TOKENS):
            issues.append("does not shift into structured pre-examination review and synthesis")

    if previous_row and not _is_orientation_week(previous_row.get("time_frame_label", "")) and not _is_exam_week(previous_row.get("time_frame_label", "")):
        current_topic_tokens = _keyword_tokens(" ".join(_csv_or_lines_to_list(row.get("topics", []))))
        previous_topic_tokens = _keyword_tokens(" ".join(_csv_or_lines_to_list(previous_row.get("topics", []))))
        previous_resource_tokens = _keyword_tokens(" ".join(_weekly_row_resource_texts(previous_row)))
        if current_topic_tokens and previous_topic_tokens and _token_overlap_ratio(current_topic_tokens, previous_topic_tokens) >= 0.85:
            issues.append("repeats almost the same topic progression as the previous teaching week")
        if resource_tokens and previous_resource_tokens and _token_overlap_ratio(resource_tokens, previous_resource_tokens) >= 0.9:
            issues.append("reuses almost the same resource cluster as the previous teaching week")

    return issues


def _safe_hint(format_hints, field):
    """Safely extract a field hint dict from format_hints."""
    if not isinstance(format_hints, dict):
        return None
    hint = format_hints.get(field)
    return hint if isinstance(hint, dict) else None


def _weekly_row_quality_issues(row, metadata=None, previous_row=None, format_hints=None):
    label = row.get("time_frame_label", "")
    is_exam_week = _is_exam_week(label)
    is_assessment_function_row = _is_assessment_function_row(row)
    ilo = row.get("intended_learning_outcomes", {})
    tla = row.get("teaching_learning_activities", {})
    resources = row.get("learning_resources", {})

    # Flat format (string/array) passes quality automatically
    ilo_is_flat = isinstance(ilo, (str, list)) and bool(ilo)
    tla_is_flat = isinstance(tla, (str, list)) and bool(tla)
    resources_is_flat = isinstance(resources, (str, list)) and bool(resources)

    # Also check format_hints from template profile
    ilo_hs = _safe_hint(format_hints, "intended_learning_outcomes")
    tla_hs = _safe_hint(format_hints, "teaching_learning_activities")
    resources_hs = _safe_hint(format_hints, "learning_resources")
    topics_hs = _safe_hint(format_hints, "topics")
    assessment_hs = _safe_hint(format_hints, "assessment")

    ilo_flat_by_hint = ilo_hs.get("output_style") == "flat_lines" if ilo_hs else False
    tla_flat_by_hint = tla_hs.get("output_style") == "flat_lines" if tla_hs else False
    resources_flat_by_hint = resources_hs.get("output_style") == "flat_lines" if resources_hs else False

    # Per-column caps from template (0 = unknown, use fallback)
    ilo_cap = (ilo_hs or {}).get("typical_item_count", 0) or 0
    topics_cap = (topics_hs or {}).get("typical_item_count", 0) or 0
    tla_cap = (tla_hs or {}).get("typical_item_count", 0) or 0
    assessment_cap = (assessment_hs or {}).get("typical_item_count", 0) or 0

    cognitive = _csv_or_lines_to_list(ilo.get("cognitive", [])) if isinstance(ilo, dict) else []
    affective = _csv_or_lines_to_list(ilo.get("affective", [])) if isinstance(ilo, dict) else []
    psychomotor = _csv_or_lines_to_list(ilo.get("psychomotor", [])) if isinstance(ilo, dict) else []
    topics = _csv_or_lines_to_list(row.get("topics", []))
    lecture = _csv_or_lines_to_list(tla.get("lecture", [])) if isinstance(tla, dict) else []
    practical = _csv_or_lines_to_list(tla.get("practical_session", [])) if isinstance(tla, dict) else []
    tla_other = _csv_or_lines_to_list(tla.get("other", [])) if isinstance(tla, dict) else []
    assessment = _csv_or_lines_to_list(row.get("assessment", []))
    mapped_clos = _normalize_mapped_clos(row.get("mapped_clos", []))

    clms = _csv_or_lines_to_list(resources.get("clms", [])) if isinstance(resources, dict) else []
    textbook = _csv_or_lines_to_list(resources.get("textbook", [])) if isinstance(resources, dict) else []
    website = _csv_or_lines_to_list(resources.get("website", [])) if isinstance(resources, dict) else []
    journal = _csv_or_lines_to_list(resources.get("journal", [])) if isinstance(resources, dict) else []

    def _dict_has_any_content(d, keys):
        return any(_csv_or_lines_to_list(d.get(k, [])) for k in keys) if isinstance(d, dict) else False

    resources_has_content = resources_is_flat or resources_flat_by_hint or _dict_has_any_content(resources, ["clms", "textbook", "website", "journal", "other"])

    issues = []

    # ── ILO quality ──
    if ilo_is_flat or ilo_flat_by_hint:
        pass  # flat ILO — no categorized check needed
    elif ilo_hs and ilo_hs.get("output_style") == "categorized" and ilo_hs.get("category_labels"):
        cats = ilo_hs["category_labels"]
        n_cats = len(cats)
        if n_cats > 0 and ilo_cap > 0:
            threshold = max(1, min(2, (ilo_cap + n_cats - 1) // n_cats))
        else:
            threshold = 2
        if len(cognitive) < threshold or len(affective) < threshold or len(psychomotor) < threshold:
            issues.append("does not fully populate all ILO groups")
    elif not ilo_is_flat and (len(cognitive) < 2 or len(affective) < 2 or len(psychomotor) < 2):
        issues.append("does not fully populate all ILO groups")

    if not _is_orientation_week(label) and not mapped_clos:
        issues.append("does not map any CLOs")

    # ── Topics quality ──
    if topics_cap > 0:
        if len(topics) < min(topics_cap, 1):
            issues.append("does not provide enough weekly topics")
    elif len(topics) < (2 if is_exam_week else 3):
        issues.append("does not provide enough weekly topics")

    # ── TLA quality ──
    if tla_is_flat or tla_flat_by_hint:
        total_tla = len(_csv_or_lines_to_list(tla)) if isinstance(tla, (str, list)) else 0
        if tla_cap > 0 and total_tla > tla_cap:
            issues.append("exceeds template TLA capacity")
    elif tla_hs and tla_hs.get("output_style") == "categorized" and tla_hs.get("category_labels"):
        tla_cats = tla_hs["category_labels"]
        tla_n = len(tla_cats)
        if tla_n > 0 and tla_cap > 0:
            tla_threshold = max(1, (tla_cap + tla_n - 1) // tla_n)
        else:
            tla_threshold = 1
        for cat_key in ["lecture", "practical_session", "other"]:
            if any(c.lower() == cat_key.replace("_session", "").replace("_", "").lower() for c in tla_cats):
                items = _csv_or_lines_to_list(tla.get(cat_key, []))
                if len(items) < tla_threshold:
                    issues.append(f"does not provide enough {cat_key.replace('_', ' ')} items")
    elif not tla_is_flat and (len(lecture) < 2 or len(practical) < 2):
        issues.append("does not provide enough teaching-learning activities")

    # ── Assessment quality ──
    if assessment_cap > 0:
        if len(assessment) < min(assessment_cap, 1):
            issues.append("does not provide enough assessments")
    elif len(assessment) < 2:
        issues.append("does not provide enough assessments")

    # ── Resources quality ──
    if not resources_has_content and not _is_orientation_week(label):
        issues.append("does not include learning resources")
    elif not (resources_is_flat or resources_flat_by_hint) and resources_hs and resources_hs.get("output_style") == "categorized":
        res_cats = [c.lower().strip() for c in (resources_hs.get("category_labels") or [])]
        has_clms_label = any("clms" in c for c in res_cats)
        has_external_labels = any(k in c for c in res_cats for k in ("textbook", "website", "journal"))
        if has_clms_label and not clms:
            issues.append("does not include a CLMS resource")
        if has_external_labels and not _is_orientation_week(label) and not is_assessment_function_row:
            external_count = len(textbook) + len(website) + len(journal)
            if external_count < 3:
                issues.append("does not include enough external learning resources")
            if not _has_real_reference_entries(textbook, website, journal):
                issues.append("does not include credible textbook, website, and journal support")
    elif not (resources_is_flat or resources_flat_by_hint) and not clms:
        issues.append("does not include a CLMS resource")
    if not (resources_is_flat or resources_flat_by_hint) and not _is_orientation_week(label) and not is_assessment_function_row and (len(textbook) + len(website) + len(journal) < 3):
        issues.append("does not include enough external learning resources")
    if not (resources_is_flat or resources_flat_by_hint) and not _is_orientation_week(label) and not is_assessment_function_row and not _has_real_reference_entries(textbook, website, journal):
        issues.append("does not include credible textbook, website, and journal support")
    if is_assessment_function_row and not (clms or (isinstance(resources, dict) and _csv_or_lines_to_list(resources.get("other", [])))):
        issues.append("does not include exam/review instructions, coverage, rubric, or consultation resources")

    sample_texts = cognitive + affective + psychomotor + topics + lecture + practical + assessment
    if any(_looks_generic_ilo_phrase(item) for item in psychomotor):
        issues.append("uses generic psychomotor outcomes")
    if mapped_clos:
        referenced_clos = []
        for item in sample_texts:
            referenced_clos.extend(_extract_clo_codes_from_text(item))
        if any(ref not in mapped_clos for ref in referenced_clos):
            issues.append("references CLO codes outside the mapped CLO set")
    if not _is_orientation_week(label) and any(_mentions_week_label(item) for item in sample_texts):
        issues.append("mentions explicit week labels inside narrative content")
    generic_phrase_checker = _looks_generic_week1_phrase if _is_orientation_week(label) else _looks_generic_weekly_phrase
    if any(generic_phrase_checker(item) for item in sample_texts):
        issues.append("contains generic weekly filler text")

    issues.extend(_weekly_row_progression_issues(row, metadata=metadata, previous_row=previous_row))
    return issues


# Issue messages that come purely from _weekly_row_progression_issues.
# These are semantic/alignment checks, not structural completeness checks.
# After a recovery pass they demote to warnings rather than blocking the generation.
_ADVISORY_PROGRESSION_ISSUE_PATTERNS = frozenset({
    "does not clearly advance the course-specific theme",
    "does not align its topics and assessments with the listed resources",
    "does not ground its topics, teaching-learning activities, and assessments in the listed resources",
    "does not shift into structured pre-examination review and synthesis",
    "repeats almost the same topic progression as the previous teaching week",
    "reuses almost the same resource cluster as the previous teaching week",
    "does not fully populate all ILO groups",
})


def _all_issues_are_advisory(issues):
    """True when every issue in *issues* is a semantic/progression advisory."""
    return bool(issues) and all(
        any(pattern in issue for pattern in _ADVISORY_PROGRESSION_ISSUE_PATTERNS)
        for issue in issues
    )


def _weekly_row_needs_quality_fallback(row, metadata=None, previous_row=None):
    return bool(_weekly_row_quality_issues(row, metadata=metadata, previous_row=previous_row))


_alignment_bundle_cache = {}
_alignment_bundle_lock = threading.Lock()
_ALIGNMENT_BUNDLE_TTL = 30


def get_copilot_alignment_bundle(local_supabase=None, strict=False, require_program_outcomes=False):
    department_name_key = local_supabase if isinstance(local_supabase, str) else "__default__"
    cache_key = (department_name_key, strict, require_program_outcomes)
    now = _time.monotonic()
    with _alignment_bundle_lock:
        cached = _alignment_bundle_cache.get(cache_key)
        if cached and (now - cached[1]) < _ALIGNMENT_BUNDLE_TTL:
            return deepcopy(cached[0])

    client = get_server_supabase_client(local_supabase)
    department_name = None
    if isinstance(local_supabase, str):
        department_name = local_supabase
        client = get_server_supabase_client()
    defaults = get_copilot_data_setting_defaults(local_supabase=client)
    dept_id = get_department_id_by_name(department_name, local_supabase=client) if department_name else None

    reference_rows = []
    if dept_id:
        try:
            reference_rows = (
                client.table('copilot_reference_entries')
                .select('*')
                .eq('department_id', dept_id)
                .order('sort_order')
                .execute()
                .data or []
            )
        except Exception:
            reference_rows = []

    grouped = {
        'graduate_attributes': [],
        'core_values': [],
        'pqf_level_6': [],
        'aqrf_level_6': [],
        'sdg': [],
    }
    for row in reference_rows:
        category = str(row.get('category') or '').strip()
        if category in grouped:
            grouped[category].append(row)

    program_outcomes = _program_outcomes_seed_rows(local_supabase=client)
    if require_program_outcomes and not program_outcomes:
        raise ValueError("Program Outcomes are empty. Configure them in the Outcome Matrix first.")

    sga_rows = grouped['graduate_attributes']
    core_rows = grouped['core_values']
    pqf_rows = grouped['pqf_level_6']
    aqrf_rows = grouped['aqrf_level_6']
    sdg_rows = grouped['sdg']

    sga_options = [str(row.get('title') or row.get('code') or '').strip() for row in sga_rows if str(row.get('title') or row.get('code') or '').strip()] or _parse_multiline_option_setting(defaults[COPILOT_GRADUATE_ATTRIBUTES_KEY], "Graduate Attributes", require_values=strict)
    core_value_options = [str(row.get('title') or row.get('code') or '').strip() for row in core_rows if str(row.get('title') or row.get('code') or '').strip()] or _parse_multiline_option_setting(defaults[COPILOT_CORE_VALUES_KEY], "Core Values", require_values=strict)
    pqf_level_6_options = [str(row.get('code') or row.get('title') or '').strip() for row in pqf_rows if str(row.get('code') or row.get('title') or '').strip()] or _parse_multiline_option_setting(defaults[COPILOT_PQF_OPTIONS_KEY], "PQF Level 6 Options", require_values=strict)
    aqrf_level_6_options = [str(row.get('code') or row.get('title') or '').strip() for row in aqrf_rows if str(row.get('code') or row.get('title') or '').strip()] or _parse_multiline_option_setting(defaults[COPILOT_AQRF_OPTIONS_KEY], "AQRF Level 6 Options", require_values=strict)
    sdg_options = [str(row.get('code') or row.get('title') or '').strip() for row in sdg_rows if str(row.get('code') or row.get('title') or '').strip()] or _parse_multiline_option_setting(defaults[COPILOT_SDG_OPTIONS_KEY], "SDG Options", require_values=strict)

    if sdg_rows:
        sdg_context = {}
        for row in sdg_rows:
            code = str(row.get('code') or row.get('title') or '').strip()
            if not code:
                continue
            sdg_context[code] = {
                "title": str(row.get('title') or code).strip(),
                "guidance": str(row.get('description') or '').strip(),
            }
        for code in sdg_options:
            sdg_context.setdefault(code, deepcopy(SDG_CONTEXT.get(code, {"title": code, "guidance": ""})))
    else:
        sdg_context = _parse_sdg_context_setting(
            defaults[COPILOT_SDG_CONTEXT_KEY],
            sdg_options,
            strict=strict,
        )

    result = {
        "program_outcomes": program_outcomes,
        "program_headers": [row["code"] for row in program_outcomes],
        "alignment_context": {
            "sga_options": sga_options,
            "core_value_options": core_value_options,
            "pqf_level_6_options": pqf_level_6_options,
            "aqrf_level_6_options": aqrf_level_6_options,
            "sdg_options": sdg_options,
            "sdg_context": sdg_context,
            "sga_details": [
                {"label": str(row.get('title') or row.get('code') or '').strip(), "meaning": str(row.get('description') or '').strip()}
                for row in sga_rows
                if str(row.get('title') or row.get('code') or '').strip()
            ],
            "core_value_details": [
                {"label": str(row.get('title') or row.get('code') or '').strip(), "meaning": str(row.get('description') or '').strip()}
                for row in core_rows
                if str(row.get('title') or row.get('code') or '').strip()
            ],
            "pqf_level_6_details": [
                {"code": str(row.get('code') or row.get('title') or '').strip(), "title": str(row.get('title') or row.get('code') or '').strip(), "meaning": str(row.get('description') or '').strip()}
                for row in pqf_rows
                if str(row.get('code') or row.get('title') or '').strip()
            ],
            "aqrf_level_6_details": [
                {"code": str(row.get('code') or row.get('title') or '').strip(), "title": str(row.get('title') or row.get('code') or '').strip(), "meaning": str(row.get('description') or '').strip()}
                for row in aqrf_rows
                if str(row.get('code') or row.get('title') or '').strip()
            ],
        },
    }

    with _alignment_bundle_lock:
        _alignment_bundle_cache[cache_key] = (result, _time.monotonic())

    return deepcopy(result)


def get_department_outcomes_bundle(department_name=None):
    bundle = get_copilot_alignment_bundle(department_name or supabase, strict=False, require_program_outcomes=False)
    return {
        "department_id": None,
        "program_outcomes": deepcopy(bundle.get("program_outcomes", [])),
        "course_outcomes": [],
        "institutional_outcomes": [],
        "institutional_headers": [],
        "program_headers": deepcopy(bundle.get("program_headers", [])),
    }


def get_static_alignment_context(local_supabase=None, strict=False):
    return get_copilot_alignment_bundle(local_supabase=local_supabase, strict=strict).get("alignment_context", {})


def get_profile_primary_alignment_bundle(template_context, department_name=None):
    """Build alignment bundle with template context as the PRIMARY source.

    Admin/system settings are used ONLY for categories completely missing
    from the template context. When template_context has data for a category,
    admin data for that category is NOT included — avoiding context pollution
    where the AI sees two conflicting versions of graduate attributes, core
    values, PQF codes, etc.
    """
    if not template_context or not isinstance(template_context, dict):
        return get_copilot_alignment_bundle(
            department_name or supabase,
            strict=False,
            require_program_outcomes=False,
        )

    template_bundle = build_alignment_bundle_from_template_context(template_context)

    admin_bundle = get_copilot_alignment_bundle(
        department_name or supabase,
        strict=False,
        require_program_outcomes=False,
    )

    merged = dict(template_bundle)
    merged["source"] = "template_profile"
    merged.setdefault("program_outcomes", [])
    merged.setdefault("program_headers", [])

    ctx = merged.setdefault("alignment_context", {})
    admin_ctx = admin_bundle.get("alignment_context", {}) if isinstance(admin_bundle.get("alignment_context"), dict) else {}

    _gap_categories = [
        "sga_options",
        "sga_details",
        "core_value_options",
        "core_value_details",
        "pqf_level_6_options",
        "pqf_level_6_details",
        "aqrf_level_6_options",
        "aqrf_level_6_details",
        "sdg_options",
        "sdg_context",
        "institutional_context",
        "institutional_outcome_options",
        "institutional_outcome_details",
    ]

    for key in _gap_categories:
        template_val = ctx.get(key)
        if not template_val or (isinstance(template_val, (list, dict)) and len(template_val) == 0):
            admin_val = admin_ctx.get(key)
            if admin_val:
                ctx[key] = admin_val

    if not merged.get("program_outcomes"):
        merged["program_outcomes"] = admin_bundle.get("program_outcomes", [])
        merged["program_headers"] = admin_bundle.get("program_headers", [])

    # ── Build source log for transparency on the review page ──
    source_log = {}
    for key in _gap_categories:
        has_template = bool(ctx.get(key))
        source_log[key] = "template" if has_template else "admin"
    source_log["program_outcomes"] = "template" if merged.get("program_outcomes") and template_bundle.get("program_outcomes") else ("admin" if merged.get("program_outcomes") else "none")
    merged["_context_source_log"] = source_log

    return merged


def get_cached_alignment_bundle(content, template_context, department_name=None):
    """Return alignment bundle from content cache, or compute and cache it.

    The alignment context (SGA options, core values, PQF/AQRF, SDGs) is
    identical across all generators for the same content.  Caching avoids
    redundant DB queries and bundle construction.
    """
    if not isinstance(content, dict):
        return get_profile_primary_alignment_bundle(template_context, department_name)
    bundle = content.get("_cached_alignment_bundle")
    if bundle is not None:
        return bundle
    bundle = get_profile_primary_alignment_bundle(template_context, department_name)
    content["_cached_alignment_bundle"] = bundle
    return bundle


def get_context_source_log(template_context, department_name=None) -> dict:
    """Return context source log for display on the review page.

    Shows which categories came from the template profile vs admin defaults.
    """
    if not template_context or not isinstance(template_context, dict):
        return {}
    bundle = get_profile_primary_alignment_bundle(template_context, department_name)
    return bundle.get("_context_source_log", {})


def get_beta_prompt_defaults():
    return {
        "prompt_copilot_beta_clo": BETA_CLO_DEFAULT_PROMPT,
        "prompt_copilot_beta_alignment": BETA_ALIGNMENT_DEFAULT_PROMPT,
        "prompt_copilot_beta_weekly": BETA_WEEKLY_DEFAULT_PROMPT,
    }


def get_review_stage_labels():
    return {
        "metadata": "Metadata ready",
        "clo_generated": "CLO draft ready",
        "alignment_generated": "Alignment draft ready",
        "weekly_generated": "Weekly outline ready",
        "beta_ready": "Finalized CLP ready",
        "beta_inserted": "Inserted into template",
    }


def build_initial_beta_content(course_data, user_profile=None, generation_spec=None):
    semester_setting = get_system_prompt(supabase, "current_semester", "Second Semester 2025-2026")
    semester_label, academic_year = _parse_current_semester(semester_setting)
    department_signatories = get_department_signatory_settings(department_name=course_data.get("department"))
    spec = normalize_generation_spec(generation_spec or course_data.get("template_generation_spec") or course_data.get("generation_spec"))

    alignment_style = spec.get("alignment_style", "clo_based")
    checkmark_po_codes_global = []
    for g in (spec.get("clo_groups") or []):
        if isinstance(g, dict) and isinstance(g.get("checkmark_po_codes"), list) and g["checkmark_po_codes"]:
            checkmark_po_codes_global = g["checkmark_po_codes"]
            break

    has_program_inst_only = bool(spec.get("program_institutional_alignments")) and not bool(spec.get("clo_groups"))
    clo_rows = []
    for blueprint in ([] if has_program_inst_only else _clo_blueprints_from_spec(spec)):
        clo_rows.append(_empty_clo_row(blueprint, alignment_style=alignment_style, checkmark_po_codes=checkmark_po_codes_global))

    clo_groups = []
    for group_index, group_spec in enumerate(spec.get("clo_groups") or [], start=1):
        group_po_codes = group_spec.get("checkmark_po_codes") if isinstance(group_spec.get("checkmark_po_codes"), list) else checkmark_po_codes_global
        group_rows = [_empty_clo_row(blueprint, alignment_style=alignment_style, checkmark_po_codes=group_po_codes) for blueprint in _clo_blueprints_from_spec(spec, group_spec)]
        clo_groups.append({
            "group_id": group_spec.get("id") or f"clo_group_{group_index}",
            "label": group_spec.get("label") or f"CLO Alignment {group_index}",
            "program_scope": group_spec.get("program_scope") if isinstance(group_spec.get("program_scope"), dict) else {},
            "checkmark_po_codes": group_po_codes if alignment_style == "checkmark" else [],
            "clo_alignment_table": group_rows,
        })
    if clo_groups:
        clo_groups[0]["clo_alignment_table"] = clo_rows

    weekly_rows = []
    for row_spec in _weekly_blueprints_from_spec(spec):
        weekly_rows.append(_empty_week_row(row_spec.get("label") or f"Week {row_spec.get('index') or len(weekly_rows) + 1}"))

    return {
        "workflow_type": "copilot_beta",
        "template_version": "new_clp_beta_v1",
        "review_stage": "metadata",
        "alignment_style": alignment_style,
        "teacher_locked_sections": [],
        "beta_ready_for_template": False,
        "beta_document_inserted": False,
        "beta_document_filename": "",
        "metadata": {
            "department": course_data.get("department", ""),
            "semester_label": semester_label or course_data.get("semester_label", ""),
            "academic_year": academic_year or course_data.get("academic_year", ""),
            "course_code": course_data.get("course_code", ""),
            "course_title": course_data.get("course_title", ""),
            "course_description": course_data.get("course_description", ""),
            "type_of_course": course_data.get("type_of_course", ""),
            "units_display": course_data.get("unit", ""),
            "credit_display": course_data.get("credit_display") or course_data.get("unit", ""),
            "contact_hours_display": course_data.get("contact_hours_per_week", ""),
            "contact_hours_per_week": course_data.get("contact_hours_per_week", ""),
            "pre_requisite": course_data.get("pre_requisites", ""),
            "co_requisite": course_data.get("co_requisites", ""),
            "class_schedule": course_data.get("class_schedule", ""),
            "room_assignment": course_data.get("room_assignment", ""),
            "service_learning_component": course_data.get("service_learning_component", ""),
            "target_sdgs_display": course_data.get("target_sdgs_display", ""),
            "source_context": course_data.get("source_context", ""),
        },
        "signatories": {
            "prepared_by_name": (
                f"{(user_profile or {}).get('first_name', '')} {(user_profile or {}).get('last_name', '')}".strip()
            ),
            "prepared_by_position": (user_profile or {}).get("title") or "Instructor",
            "reviewed_by_name": department_signatories.get("program_coordinator_name") or "",
            "reviewed_by_position": department_signatories.get("program_coordinator_title") or "Program Coordinator",
            "endorsed_by_name": department_signatories.get("dean_name") or "",
            "endorsed_by_position": department_signatories.get("dean_title") or "Dean",
            "approved_by_name": department_signatories.get("vice_president_name") or "",
            "approved_by_position": department_signatories.get("vice_president_title") or "Vice President",
        },
        "alignment_context": get_static_alignment_context(),
        "template_generation_spec": spec,
        "template_profile_warnings": spec.get("warnings", []),
        "program_institutional_alignments": _empty_program_institutional_alignments(spec),
        "clo_alignment_groups": clo_groups or ([] if has_program_inst_only else [{
            "group_id": "clo_group_1",
            "label": "CLO Alignment",
            "program_scope": {},
            "clo_alignment_table": clo_rows,
        }]),
        "clo_alignment_table": clo_rows,
        "weekly_course_outline": weekly_rows,
        "references": {
            "website": [],
            "textbook": [],
            "journal": [],
            "other": [],
        },
        "validation": {
            "errors": [],
            "warnings": [],
        },
    }


def normalize_beta_content(content):
    """Merge existing content into a fresh skeleton + apply defaults.

    Does NOT apply spec-driven shape (use apply_spec_shape() explicitly
    when the generation spec might have changed: generation, profile swap).
    """
    if not isinstance(content, dict):
        content = {}
    spec = _get_generation_spec(content)
    merged = build_initial_beta_content({}, user_profile={}, generation_spec=spec)
    merged.update({k: v for k, v in content.items() if k not in {"metadata", "signatories", "alignment_context", "references", "validation"}})
    for key in ("metadata", "signatories", "alignment_context", "references", "validation"):
        current = content.get(key)
        base = merged.get(key, {})
        if isinstance(base, dict):
            base.update(current if isinstance(current, dict) else {})
            merged[key] = base
    if isinstance(content.get("clo_alignment_table"), list) and content.get("clo_alignment_table"):
        merged["clo_alignment_table"] = content["clo_alignment_table"]
    if isinstance(content.get("weekly_course_outline"), list) and content.get("weekly_course_outline"):
        merged["weekly_course_outline"] = content["weekly_course_outline"]
    if isinstance(content.get("program_institutional_alignments"), dict):
        merged["program_institutional_alignments"] = content["program_institutional_alignments"]
    merged = apply_defaults(merged)
    return merged


def _compute_spec_hash(content):
    """Return a stable hash of the template_generation_spec for change detection."""
    spec = content.get("template_generation_spec") or {}
    raw = json.dumps(spec, sort_keys=True, default=str)
    return hashlib.md5(raw.encode()).hexdigest()


_RUNTIME_COPILOT_CACHE_KEYS = {
    "template_generation_spec",
    "_spec_shape_hash",
}


def strip_runtime_copilot_cache(content):
    """Return beta content without prompt/runtime cache data that can be rebuilt."""
    if not isinstance(content, dict):
        return content
    cleaned = dict(content)
    for key in list(cleaned.keys()):
        if key in _RUNTIME_COPILOT_CACHE_KEYS or key.startswith("_cached_") or key.startswith("_cache_"):
            cleaned.pop(key, None)
    return cleaned


def apply_defaults(content):
    """Set missing default keys on content. Never overwrites existing values."""
    metadata = content.setdefault("metadata", {})
    for key in [
        "department", "semester_label", "academic_year", "course_code",
        "course_title", "course_description", "type_of_course", "units_display",
        "credit_display", "contact_hours_display", "contact_hours_per_week",
        "pre_requisite", "co_requisite", "class_schedule", "room_assignment",
        "service_learning_component", "target_sdgs_display", "source_context",
    ]:
        metadata.setdefault(key, "")

    signatories = content.setdefault("signatories", {})
    for key in [
        "prepared_by_name", "prepared_by_position",
        "reviewed_by_name", "reviewed_by_position",
        "endorsed_by_name", "endorsed_by_position",
        "approved_by_name", "approved_by_position",
    ]:
        signatories.setdefault(key, "")

    content.setdefault("workflow_type", "copilot_beta")
    content.setdefault("template_version", "new_clp_beta_v1")
    content.setdefault("generation_mode", "template_matched")
    content.setdefault("review_stage", "metadata")
    if not isinstance(content.get("teacher_locked_sections"), list):
        content["teacher_locked_sections"] = []
    content["beta_ready_for_template"] = _to_bool(content.get("beta_ready_for_template", False))
    content["beta_document_inserted"] = _to_bool(content.get("beta_document_inserted", False))
    content["beta_document_filename"] = str(content.get("beta_document_filename") or "")

    references = content.setdefault("references", {})
    references.setdefault("website", [])
    references.setdefault("textbook", [])
    references.setdefault("journal", [])
    references.setdefault("other", [])

    validation = content.setdefault("validation", {})
    if not isinstance(validation.get("errors"), list):
        validation["errors"] = []
    if not isinstance(validation.get("warnings"), list):
        validation["warnings"] = []

    return content


def apply_spec_shape(content, force=False):
    """Apply generation-spec-driven shape. Only re-applies when spec changed."""
    spec = _get_generation_spec(content)
    current_hash = _compute_spec_hash(content)
    stored_hash = content.get("_spec_shape_hash")

    if not force and stored_hash == current_hash:
        return content

    content["template_generation_spec"] = spec
    content["template_profile_warnings"] = spec.get("warnings", [])
    content["alignment_style"] = spec.get("alignment_style", content.get("alignment_style", "clo_based"))
    content["program_institutional_alignments"] = _empty_program_institutional_alignments(
        spec,
        content.get("program_institutional_alignments") if isinstance(content.get("program_institutional_alignments"), dict) else {},
    )

    content = _normalize_clo_groups(content, spec)

    existing_weekly = content.get("weekly_course_outline") if isinstance(content.get("weekly_course_outline"), list) else []
    existing_by_label = {
        str(row.get("time_frame_label") or ""): row
        for row in existing_weekly
        if isinstance(row, dict)
    }
    weekly_rows = []
    for row_spec in _weekly_blueprints_from_spec(spec):
        label = str(row_spec.get("label") or f"Week {len(weekly_rows) + 1}")
        existing = existing_by_label.get(label, {})
        weekly_rows.append(_empty_week_row(label, existing))
    content["weekly_course_outline"] = weekly_rows

    content["_spec_shape_hash"] = current_hash
    return content


def ensure_beta_shape(content):
    """Legacy wrapper — apply_defaults + apply_spec_shape.

    Kept for backward compatibility with external callers.
    Use apply_defaults / apply_spec_shape directly for finer control.
    """
    content = apply_defaults(content)
    return apply_spec_shape(content)


def update_beta_content_from_form(content, form_data):
    content = ensure_beta_shape(normalize_beta_content(content))
    metadata = content["metadata"]
    for key in [
        "semester_label",
        "academic_year",
        "course_code",
        "course_title",
        "course_description",
        "type_of_course",
        "units_display",
        "credit_display",
        "contact_hours_display",
        "contact_hours_per_week",
        "pre_requisite",
        "co_requisite",
        "class_schedule",
        "room_assignment",
        "service_learning_component",
        "target_sdgs_display",
        "source_context",
    ]:
        if key in form_data:
            metadata[key] = (form_data.get(key) or "").strip()

    signatories = content.setdefault("signatories", {})
    for key in [
        "prepared_by_name",
        "prepared_by_position",
        "reviewed_by_name",
        "reviewed_by_position",
        "endorsed_by_name",
        "endorsed_by_position",
        "approved_by_name",
        "approved_by_position",
    ]:
        if key in form_data:
            signatories[key] = (form_data.get(key) or "").strip()

    alignment_style = _get_alignment_style(content)
    for index, row in enumerate(content["clo_alignment_table"], start=1):
        row["clo_statement"] = (form_data.get(f"clo_{index}_statement") or row.get("clo_statement") or "").strip()
        if alignment_style == "checkmark":
            checkmark_data = row.get("checkmark_alignments") if isinstance(row.get("checkmark_alignments"), dict) else {}
            po_codes = list(checkmark_data.keys())
            submitted_keys = []
            for po_code in po_codes:
                submitted_keys.append(_checkmark_form_key(index, po_code))
                submitted_keys.append(f"clo_{index}_check_{str(po_code or '').replace(' ', '_')}")
            row_was_submitted = (
                _form_has_key(form_data, _checkmark_present_form_key(index))
                or any(_form_has_key(form_data, key) for key in submitted_keys)
            )
            if not row_was_submitted:
                row["checkmark_alignments"] = checkmark_data
                continue
            for po_code in po_codes:
                form_key = _checkmark_form_key(index, po_code)
                legacy_key = f"clo_{index}_check_{str(po_code or '').replace(' ', '_')}"
                checkmark_data[po_code] = _to_bool(form_data.get(form_key, form_data.get(legacy_key, False)))
            row["checkmark_alignments"] = checkmark_data
        else:
            row["aligned_plos"] = _csv_or_lines_to_list(form_data.get(f"clo_{index}_aligned_plos", row.get("aligned_plos", [])))
            row["graduate_attributes"] = _csv_or_lines_to_list(form_data.get(f"clo_{index}_graduate_attributes", row.get("graduate_attributes", [])))
            row["core_values"] = _csv_or_lines_to_list(form_data.get(f"clo_{index}_core_values", row.get("core_values", [])))
            row["pqf_level_6_alignment"] = _csv_or_lines_to_list(form_data.get(f"clo_{index}_pqf_alignment", row.get("pqf_level_6_alignment", [])))
            row["aqrf_level_6_alignment"] = _csv_or_lines_to_list(form_data.get(f"clo_{index}_aqrf_alignment", row.get("aqrf_level_6_alignment", [])))
            row["relevant_sdgs"] = _csv_or_lines_to_list(form_data.get(f"clo_{index}_relevant_sdgs", row.get("relevant_sdgs", [])))

    program_inst_specs = _program_institutional_specs(content)
    if program_inst_specs:
        matrices = content.get("program_institutional_alignments")
        if not isinstance(matrices, dict):
            matrices = {}
        for alignment in program_inst_specs:
            alignment_id = alignment.get("id") or "program_institutional_alignment"
            row_keys = [
                normalize_alignment_row_label(item)
                for item in (alignment.get("row_labels_normalized") or [])
                if normalize_alignment_row_label(item)
            ]
            col_keys = [
                normalize_alignment_column_label(item)
                for item in (alignment.get("column_labels_normalized") or [])
                if normalize_alignment_column_label(item)
            ]
            matrix = matrices.get(alignment_id)
            if not isinstance(matrix, dict):
                matrix = {}
            for row_key in row_keys:
                row_values = matrix.get(row_key)
                if not isinstance(row_values, dict):
                    row_values = {}
                row_was_submitted = (
                    _form_has_key(form_data, _program_inst_present_form_key(alignment_id, row_key))
                    or any(_form_has_key(form_data, _program_inst_form_key(alignment_id, row_key, col_key)) for col_key in col_keys)
                )
                if not row_was_submitted:
                    matrix[row_key] = row_values
                    continue
                for col_key in col_keys:
                    form_key = _program_inst_form_key(alignment_id, row_key, col_key)
                    row_values[col_key] = "✔" if _to_bool(form_data.get(form_key, False)) else ""
                matrix[row_key] = row_values
            matrices[alignment_id] = matrix
        content["program_institutional_alignments"] = matrices

    # Detect weekly field structure from format_hints
    spec_fs = content.get("template_generation_spec", {})
    wo_fs = spec_fs.get("weekly_outline", {}) if isinstance(spec_fs, dict) else {}
    hints = (wo_fs.get("format_hints") or {}).get("fields") if isinstance(wo_fs, dict) else None
    hints = hints if isinstance(hints, dict) else {}

    for row in content["weekly_course_outline"]:
        key = row["field_key"]
        row["mapped_clos"] = _normalize_mapped_clos(form_data.get(f"{key}_mapped_clos", row.get("mapped_clos", [])))

        for field_name in _WEEKLY_FIELDS:
            spec_info = _weekly_field_spec(hints, field_name)
            label = _WEEKLY_FIELD_LABELS.get(field_name, field_name)

            if spec_info["sub_fields"]:
                # Categorized — each sub-field is its own textarea
                current = row.get(field_name)
                if not isinstance(current, dict):
                    current = {}
                for sf in spec_info["sub_fields"]:
                    form_key = f"{key}_{field_name}_{sf['key']}"
                    submitted = _csv_or_lines_to_list(form_data.get(form_key, current.get(sf["key"], [])))
                    current[_slugify(sf["label"])] = submitted
                row[field_name] = current
            else:
                # Flat — one textarea for the whole field
                flat_key = f"{key}_{field_name}"
                if flat_key in form_data:
                    raw = form_data.get(flat_key, "")
                    if isinstance(raw, str):
                        row[field_name] = raw.strip()
                    else:
                        row[field_name] = _csv_or_lines_to_list(raw)
                # If not in form_data, preserve existing value (no change)

        row["topics"] = _csv_or_lines_to_list(form_data.get(f"{key}_topics", row.get("topics", [])))
        row["assessment"] = _csv_or_lines_to_list(form_data.get(f"{key}_assessment", row.get("assessment", [])))

    content["review_stage"] = form_data.get("review_stage") or content.get("review_stage", "metadata")
    generation_mode = form_data.get("generation_mode")
    if generation_mode in ("template_matched", "unconstrained"):
        content["generation_mode"] = generation_mode
    _sync_primary_clo_rows_to_groups(content, include_alignment=False)
    return content


def validate_beta_content(content, require_final_ready=False):
    content = ensure_beta_shape(normalize_beta_content(content))
    errors = []
    warnings = []
    alignment_style = _get_alignment_style(content)
    disable_po_matrix = _should_disable_po_matrix(content)
    metadata = content["metadata"]
    for key in ["course_code", "course_title", "course_description", "department"]:
        if not metadata.get(key):
            errors.append(f"Missing required metadata: {key.replace('_', ' ')}.")
    for row in content["clo_alignment_table"]:
        if not row.get("clo_statement"):
            errors.append(f"{row.get('clo_code')} is missing a statement.")

        # --- Strict Structural Validation ---
        if alignment_style == "checkmark":
            checks = row.get("checkmark_alignments") if isinstance(row.get("checkmark_alignments"), dict) else {}
            if not any(checks.values()) and not disable_po_matrix:
                warnings.append(f"{row.get('clo_code')} has no program outcomes checked yet.")
            # Purge non-checkmark alignment keys to maintain strict pipeline
            for k in ["aligned_plos", "graduate_attributes", "core_values", "pqf_level_6_alignment", "aqrf_level_6_alignment", "relevant_sdgs"]:
                if k in row:
                    row.pop(k)
        else:
            if not row.get("aligned_plos") and not disable_po_matrix:
                warnings.append(f"{row.get('clo_code')} has no aligned PLOs yet.")
            # Purge checkmark alignments if in clo_based mode
            if "checkmark_alignments" in row:
                row.pop("checkmark_alignments")
    groups = content.get("clo_alignment_groups") if isinstance(content.get("clo_alignment_groups"), list) else []
    if len(groups) > 1:
        for group in groups:
            group_label = group.get("label") or "CLO alignment group"
            for row in group.get("clo_alignment_table", []) or []:
                if alignment_style == "checkmark":
                    checks = row.get("checkmark_alignments") if isinstance(row.get("checkmark_alignments"), dict) else {}
                    if not any(checks.values()) and not disable_po_matrix:
                        warnings.append(f"{group_label} {row.get('clo_code')} has no program outcomes checked yet.")
                    # Purge non-checkmark alignment keys
                    for k in ["aligned_plos", "graduate_attributes", "core_values", "pqf_level_6_alignment", "aqrf_level_6_alignment", "relevant_sdgs"]:
                        if k in row:
                            row.pop(k)
                else:
                    if not row.get("aligned_plos") and not disable_po_matrix:
                        warnings.append(f"{group_label} {row.get('clo_code')} has no aligned PLOs yet.")
                    # Purge checkmark alignments
                    if "checkmark_alignments" in row:
                        row.pop("checkmark_alignments")
    if not any(row.get("topics") for row in content["weekly_course_outline"]):
        warnings.append("Weekly outline is still empty.")

    program_inst_content = content.get("program_institutional_alignments") if isinstance(content.get("program_institutional_alignments"), dict) else {}
    for alignment in _program_institutional_specs(content):
        alignment_id = alignment.get("id") or "program_institutional_alignment"
        row_keys = [
            normalize_alignment_row_label(item)
            for item in (alignment.get("row_labels_normalized") or [])
            if normalize_alignment_row_label(item)
        ]
        col_keys = [
            normalize_alignment_column_label(item)
            for item in (alignment.get("column_labels_normalized") or [])
            if normalize_alignment_column_label(item)
        ]
        matrix = program_inst_content.get(alignment_id)
        if not isinstance(matrix, dict):
            errors.append(f"{alignment_id} is missing program-institutional alignment data.")
            continue
        extra_rows = sorted(set(matrix.keys()) - set(row_keys))
        if extra_rows:
            errors.append(f"{alignment_id} contains extra alignment rows: {', '.join(extra_rows)}.")
        for row_key in row_keys:
            row_values = matrix.get(row_key)
            if not isinstance(row_values, dict):
                errors.append(f"{alignment_id} is missing row {row_key}.")
                continue
            extra_cols = sorted(set(row_values.keys()) - set(col_keys))
            if extra_cols:
                errors.append(f"{alignment_id} row {row_key} contains extra columns: {', '.join(extra_cols)}.")
            for col_key in col_keys:
                if col_key not in row_values:
                    errors.append(f"{alignment_id} is missing required key {row_key}_{col_key}.")
                    continue
                value = row_values.get(col_key)
                if value not in ("✔", ""):
                    errors.append(f"{alignment_id} value {row_key}_{col_key} must be \"✔\" or blank.")
                row_values[col_key] = _to_checkmark_value(value)

    if require_final_ready:
        if not _csv_or_lines_to_list(metadata.get("target_sdgs_display", [])):
            errors.append("Target SDGs are still empty.")

        for row in content["clo_alignment_table"]:
            if alignment_style == "checkmark":
                checks = row.get("checkmark_alignments") if isinstance(row.get("checkmark_alignments"), dict) else {}
                if not any(checks.values()) and not disable_po_matrix:
                    warnings.append(f"{row.get('clo_code')} has no program outcomes checked. Mark at least one PO.")
            else:
                missing_alignment = []
                if not row.get("aligned_plos") and not disable_po_matrix:
                    missing_alignment.append("aligned PLOs")
                if not row.get("graduate_attributes"):
                    missing_alignment.append("graduate attributes")
                if not row.get("core_values"):
                    missing_alignment.append("core values")
                if not row.get("pqf_level_6_alignment"):
                    missing_alignment.append("PQF alignment")
                if not row.get("aqrf_level_6_alignment"):
                    missing_alignment.append("AQRF alignment")
                if not row.get("relevant_sdgs"):
                    missing_alignment.append("relevant SDGs")
                if missing_alignment:
                    errors.append(f"{row.get('clo_code')} is missing alignment data: {', '.join(missing_alignment)}.")
        if len(groups) > 1:
            for group in groups:
                group_label = group.get("label") or "CLO alignment group"
                for row in group.get("clo_alignment_table", []) or []:
                    if alignment_style == "checkmark":
                        checks = row.get("checkmark_alignments") if isinstance(row.get("checkmark_alignments"), dict) else {}
                        if not any(checks.values()) and not disable_po_matrix:
                            warnings.append(f"{group_label} {row.get('clo_code')} has no program outcomes checked.")
                    else:
                        missing_alignment = []
                        if not row.get("aligned_plos") and not disable_po_matrix:
                            missing_alignment.append("aligned PLOs")
                        if not row.get("graduate_attributes"):
                            missing_alignment.append("graduate attributes")
                        if not row.get("core_values"):
                            missing_alignment.append("core values")
                        if not row.get("pqf_level_6_alignment"):
                            missing_alignment.append("PQF alignment")
                        if not row.get("aqrf_level_6_alignment"):
                            missing_alignment.append("AQRF alignment")
                        if not row.get("relevant_sdgs"):
                            missing_alignment.append("relevant SDGs")
                        if missing_alignment:
                            errors.append(f"{group_label} {row.get('clo_code')} is missing alignment data: {', '.join(missing_alignment)}.")
        for row in content["weekly_course_outline"]:
            label = row.get("time_frame_label", "Weekly row")
            ilo = row.get("intended_learning_outcomes", {})
            tla = row.get("teaching_learning_activities", {})
            resources = row.get("learning_resources", {})
            if not row.get("mapped_clos"):
                errors.append(f"{label} is missing mapped CLOs.")
            if isinstance(ilo, dict):
                if not _csv_or_lines_to_list(ilo.get("cognitive", [])):
                    errors.append(f"{label} is missing cognitive intended learning outcomes.")
                if not _csv_or_lines_to_list(ilo.get("affective", [])):
                    errors.append(f"{label} is missing affective intended learning outcomes.")
                if not _csv_or_lines_to_list(ilo.get("psychomotor", [])):
                    errors.append(f"{label} is missing psychomotor intended learning outcomes.")
            elif not ilo:
                errors.append(f"{label} is missing intended learning outcomes.")
            if not row.get("topics"):
                errors.append(f"{label} is missing topics.")
            if not row.get("assessment"):
                errors.append(f"{label} is missing assessments.")
            if isinstance(tla, dict):
                if not any(_csv_or_lines_to_list(tla.get(key, [])) for key in ["lecture", "practical_session", "other"]):
                    errors.append(f"{label} is missing teaching-learning activities.")
            elif not tla:
                errors.append(f"{label} is missing teaching-learning activities.")
            if isinstance(resources, dict):
                if not any(_csv_or_lines_to_list(resources.get(key, [])) for key in ["clms", "textbook", "website", "journal", "other"]):
                    errors.append(f"{label} is missing learning resources.")
            elif not resources:
                errors.append(f"{label} is missing learning resources.")

    validation_state = content.setdefault("validation", {})
    preserved_warnings = [
        str(item).strip()
        for item in (validation_state.get("warnings") or [])
        if str(item).strip()
        and "template-safe detail" not in str(item).lower()
    ]
    warnings = list(dict.fromkeys(preserved_warnings + warnings))
    resolved_warnings = {
        str(item).strip()
        for item in (validation_state.get("resolved_warnings") or [])
        if str(item).strip()
    }
    if resolved_warnings:
        warnings = [warning for warning in warnings if str(warning).strip() not in resolved_warnings]
    content["validation"]["errors"] = errors
    content["validation"]["warnings"] = list(dict.fromkeys(warnings))
    return errors, warnings, content


def compute_validation(content):
    """Run full validation and attach fresh results to content.

    Sets content['validation'] with errors, warnings, and a computed-at
    version marker so the review page can detect stale state.

    Returns (errors, warnings, content) — same signature as validate_beta_content.
    """
    errors, warnings, content = validate_beta_content(content)
    content.setdefault("_validation_version", 0)
    content["_validation_computed_at"] = content.get("_validation_version", 0)
    return errors, warnings, content


def _validate_ai_response_shape(data, task_key, expected_clo_count=None, expected_week_labels=None):
    """Validate parsed AI JSON shape matches expectations.

    Returns a dict with:
      - ``valid``: bool
      - ``warnings``: list of shape mismatch descriptions
      - ``data``: the original data (unchanged)
    """
    warnings = []
    if not isinstance(data, dict):
        return {"valid": False, "warnings": ["AI response is not a JSON object."], "data": data}

    if task_key == "copilot_beta_clo":
        rows = []
        for key in ("clo_alignment_table", "clo_rows", "clos", "course_learning_outcomes", "rows"):
            val = data.get(key)
            if isinstance(val, list):
                rows = val
                break
        if expected_clo_count is not None and len(rows) != expected_clo_count:
            warnings.append(
                f"AI returned {len(rows)} CLO rows, expected {expected_clo_count}. "
                f"Mismatched rows will use existing content."
            )
        missing_statements = 0
        for row in rows:
            if isinstance(row, dict) and not str(row.get("clo_statement") or row.get("statement") or "").strip():
                missing_statements += 1
        if missing_statements:
            warnings.append(f"{missing_statements} CLO row(s) have empty statements.")

    elif task_key == "copilot_beta_alignment":
        rows = []
        for key in ("clo_alignment_table", "alignment_rows", "rows"):
            val = data.get(key)
            if isinstance(val, list):
                rows = val
                break
        if expected_clo_count is not None and len(rows) != expected_clo_count:
            warnings.append(
                f"AI returned {len(rows)} alignment rows, expected {expected_clo_count}. "
                f"Mismatched rows will use existing content."
            )

    elif task_key == "copilot_beta_weekly":
        rows = []
        for key in ("weekly_course_outline", "weekly_rows", "course_outline", "rows"):
            val = data.get(key)
            if isinstance(val, list):
                rows = val
                break
        if expected_week_labels and rows:
            returned_labels = [
                str(row.get("time_frame_label") or "").strip()
                for row in rows if isinstance(row, dict)
            ]
            expected_set = set(expected_week_labels)
            returned_set = set(returned_labels)
            missing = expected_set - returned_set
            extra = returned_set - expected_set
            if missing:
                warnings.append(
                    f"AI missed {len(missing)} week label(s): {sorted(missing)[:5]}. "
                    f"Missing rows will use existing or fallback content."
                )
            if extra:
                warnings.append(
                    f"AI returned {len(extra)} unexpected week label(s): {sorted(extra)[:3]}. "
                    f"Extra rows will be ignored."
                )

    elif task_key in ("copilot_beta_final_review",):
        if "approved" not in data:
            warnings.append("Final review response missing 'approved' field.")
        if "issues" not in data:
            warnings.append("Final review response missing 'issues' field.")
        if "warnings" not in data:
            warnings.append("Final review response missing 'warnings' field.")

    return {"valid": len(warnings) == 0, "warnings": warnings, "data": data}


def _beta_ai_budget(task_key, prompt=""):
    if task_key in {"copilot_beta_clo", "copilot_beta_single_clo"}:
        return {"max_output_tokens": 4096, "retries": 1, "prompt_warn_chars": 12000}
    if task_key in {
        "copilot_beta_alignment",
        "copilot_beta_checkmark_alignment",
        "copilot_beta_program_institutional_alignment",
    }:
        return {"max_output_tokens": 4096, "retries": 1, "prompt_warn_chars": 12000}
    if task_key in {"copilot_beta_weekly", "copilot_beta_single_week"}:
        return {"max_output_tokens": 8192, "retries": 1, "prompt_warn_chars": 18000}
    if task_key in {"copilot_beta_final_fix"}:
        return {"max_output_tokens": 16384, "retries": 2, "prompt_warn_chars": 24000}
    if task_key in {"copilot_beta_final_review"}:
        return {"max_output_tokens": 4096, "retries": 1, "prompt_warn_chars": 24000}
    return {"max_output_tokens": 8192, "retries": 1, "prompt_warn_chars": 18000}


def _generate_json(prompt, task_key):
    def _coerce_json_literals_for_python(value):
        output = []
        token = ""
        in_string = False
        escape = False

        def flush_token():
            nonlocal token
            if not token:
                return
            lowered = token.lower()
            if lowered == "true":
                output.append("True")
            elif lowered == "false":
                output.append("False")
            elif lowered == "null":
                output.append("None")
            else:
                output.append(token)
            token = ""

        for ch in value:
            if in_string:
                output.append(ch)
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                flush_token()
                in_string = True
                output.append(ch)
                continue
            if ch.isalpha():
                token += ch
                continue
            flush_token()
            output.append(ch)
        flush_token()
        return "".join(output)

    def _strip_trailing_commas(value):
        return re.sub(r",\s*([}\]])", r"\1", value)

    def _extract_json_snippet(value):
        if not value:
            return value
        obj_start = value.find("{")
        arr_start = value.find("[")
        if obj_start == -1 and arr_start == -1:
            return value
        start = obj_start if arr_start == -1 else (arr_start if obj_start == -1 else min(obj_start, arr_start))
        end_obj = value.rfind("}")
        end_arr = value.rfind("]")
        end = end_obj if end_arr == -1 else (end_arr if end_obj == -1 else max(end_obj, end_arr))
        if start >= 0 and end > start:
            return value[start:end + 1]
        return value

    def _normalize_json_quotes(value):
        # Replace common non-ASCII quotes with ASCII equivalents before parsing.
        return (
            value.replace("\u201c", '"')
            .replace("\u201d", '"')
            .replace("\u2018", "'")
            .replace("\u2019", "'")
        )

    def _parse_ai_json(raw_text):
        cleaned = AIClient.clean_ai_json(raw_text)
        cleaned = _normalize_json_quotes(cleaned)
        candidates = [cleaned, _extract_json_snippet(cleaned)]
        for candidate in candidates:
            if not candidate:
                continue
            candidate = _strip_trailing_commas(candidate)
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
        try:
            decoder = json.JSONDecoder()
            parsed, _ = decoder.raw_decode(_strip_trailing_commas(cleaned))
            if isinstance(parsed, (dict, list)):
                return parsed
        except json.JSONDecodeError:
            pass
        try:
            python_like = _coerce_json_literals_for_python(_strip_trailing_commas(cleaned))
            parsed = ast.literal_eval(python_like)
        except Exception as exc:
            raise exc
        if isinstance(parsed, (dict, list)):
            return parsed
        raise json.JSONDecodeError("AI JSON parsing failed", cleaned, 0)

    def _json_retry_prompt(original_prompt):
        return f"""\
The previous response for {task_key} was incomplete or invalid JSON.
Regenerate the result from the original request.

Return exactly one complete valid JSON object or array.
Do not include markdown, commentary, or multiple JSON values.
Use double-quoted JSON strings, escape line breaks inside strings, and close every object and array.

ORIGINAL REQUEST:
{original_prompt}
""".strip()

    def _json_retry_budget():
        retry_budget = dict(budget)
        retry_budget["retries"] = 1
        if task_key in {
            "copilot_beta_alignment",
            "copilot_beta_checkmark_alignment",
            "copilot_beta_program_institutional_alignment",
        }:
            retry_budget["max_output_tokens"] = max(int(retry_budget.get("max_output_tokens") or 0), 8192)
        return retry_budget

    budget = _beta_ai_budget(task_key, prompt)
    prompt_size = len(prompt or "")
    if prompt_size > budget["prompt_warn_chars"]:
        current_app.logger.warning(
            "Beta AI prompt for %s is large: prompt_chars=%s max_output_tokens=%s",
            task_key,
            prompt_size,
            budget["max_output_tokens"],
        )

    plan_id = getattr(_BETA_AI_CALL_CONTEXT, "plan_id", None)
    user_id = getattr(_BETA_AI_CALL_CONTEXT, "user_id", None)
    preview_callback = getattr(_BETA_AI_CALL_CONTEXT, "preview_callback", None)
    config = {"response_mime_type": "application/json", "max_output_tokens": budget["max_output_tokens"]}
    stream_kwargs = {}
    if callable(preview_callback) and not current_app.config.get("TESTING", False):
        stream_kwargs["on_chunk"] = lambda chunk: _publish_beta_ai_preview(task_key, chunk)

    try:
        model = AIClient.get_model()
        resp = AIClient.generate_with_retry(
            model,
            [prompt],
            config,
            retries=budget["retries"],
            task_type=task_key,
            plan_id=plan_id,
            user_id=user_id,
            **stream_kwargs,
        )
    except Exception as exc:
        current_app.logger.error("Beta AI generation failed for %s: %s", task_key, exc)
        raise ValueError(f"AI generation failed for {task_key}: {exc}") from exc
    if stream_kwargs:
        _publish_beta_ai_preview(task_key, force=True)
    current_app.logger.info(
        "Beta AI response for %s: prompt_chars=%s response_chars=%s plan_id=%s user_id=%s retries=%s max_output_tokens=%s",
        task_key,
        prompt_size,
        len(getattr(resp, "text", "") or ""),
        plan_id,
        user_id,
        budget["retries"],
        budget["max_output_tokens"],
    )
    try:
        return _parse_ai_json(resp.text)
    except Exception as exc:
        current_app.logger.error("Beta AI returned unparseable JSON for %s: %s", task_key, exc)
        if not str(task_key or "").startswith("copilot_beta_"):
            raise ValueError(f"AI returned invalid JSON for {task_key}. Please retry.") from exc
        retry_budget = _json_retry_budget()
        retry_prompt = _json_retry_prompt(prompt)
        retry_config = {
            "response_mime_type": "application/json",
            "max_output_tokens": retry_budget["max_output_tokens"],
        }
        current_app.logger.warning(
            "Retrying beta AI JSON generation for %s after parse failure: prompt_chars=%s max_output_tokens=%s",
            task_key,
            len(retry_prompt),
            retry_budget["max_output_tokens"],
        )
        try:
            retry_resp = AIClient.generate_with_retry(
                model,
                [retry_prompt],
                retry_config,
                retries=retry_budget["retries"],
                task_type=task_key,
                plan_id=plan_id,
                user_id=user_id,
                **stream_kwargs,
            )
            if stream_kwargs:
                _publish_beta_ai_preview(task_key, force=True)
            current_app.logger.info(
                "Beta AI JSON retry response for %s: prompt_chars=%s response_chars=%s plan_id=%s user_id=%s retries=%s max_output_tokens=%s",
                task_key,
                len(retry_prompt),
                len(getattr(retry_resp, "text", "") or ""),
                plan_id,
                user_id,
                retry_budget["retries"],
                retry_budget["max_output_tokens"],
            )
            return _parse_ai_json(retry_resp.text)
        except Exception as retry_exc:
            current_app.logger.error("Beta AI JSON retry failed for %s: %s", task_key, retry_exc)
            raise ValueError(f"AI returned invalid JSON for {task_key}. Please retry.") from retry_exc


def _build_template_profile_hints(profile):
    """Build concise AI prompt hints from a template profile.

    Returns a string block describing the expected output structure based on
    the template profile sections and columns, or empty string if no profile.
    """
    if not profile or not isinstance(profile, dict):
        return ""
    sections = profile.get("sections", [])
    alignment_cols = profile.get("alignment_columns", {})
    if not sections and not alignment_cols:
        return ""
    lines = ["TEMPLATE STRUCTURE HINTS (your output must fit this template layout):"]
    if alignment_cols:
        col_names = sorted(alignment_cols.items(), key=lambda x: x[1].get("col_index", 0))
        cols_desc = ", ".join(f"{v.get('label', k)} (col {v.get('col_index', '?')})" for k, v in col_names)
        lines.append(f"- CLO table columns: {cols_desc}")
    generation_spec = profile.get("generation_spec") if isinstance(profile.get("generation_spec"), dict) else {}
    if generation_spec:
        groups = generation_spec.get("clo_groups") or []
        weekly = generation_spec.get("weekly_outline") or {}
        if groups:
            lines.append("- CLO groups: " + "; ".join(f"{g.get('label', g.get('id', 'Group'))}: {g.get('row_count', len(g.get('rows', [])))} rows" for g in groups))
        if weekly:
            lines.append(f"- Weekly outline rows: {weekly.get('row_count', len(weekly.get('rows', [])))}")
    for sec in sections:
        sec_id = sec.get("id", "")
        label = sec.get("label", sec_id)
        ctype = sec.get("content_type", "")
        lines.append(f"- Section \"{label}\" ({ctype})")
    lines.append("Ensure your generated content is compatible with the above structure.")
    return "\n".join(lines)


def _context_source_note(alignment_bundle):
    if str((alignment_bundle or {}).get("source") or "").startswith("template_profile"):
        return (
            "CONTEXT SOURCE RULE:\n"
            "Use template-extracted framework data as authoritative where present. "
            "Use admin/system defaults only for categories missing from the template."
        )
    return ""


def generate_beta_clos(content, template_profile_hints=None, template_context=None):
    content = ensure_beta_shape(normalize_beta_content(content))
    metadata = content["metadata"]
    clo_blueprints = _clo_blueprints_from_spec(_get_generation_spec(content))
    clo_codes = [row.get("code") for row in clo_blueprints]
    clo_domains = {row.get("code"): row.get("domain", "cognitive") for row in clo_blueprints}
    clo_plan = [{"code": bp.get("code"), "domain": bp.get("domain", "cognitive")} for bp in clo_blueprints]
    alignment_bundle = get_cached_alignment_bundle(
        content,
        template_context,
        metadata.get("department"),
    )
    department_pos = alignment_bundle.get("program_outcomes", [])
    context = alignment_bundle.get("alignment_context", {})
    dynamic_clo = _get_dynamic_prompt(content, "clo")
    prompt = f"""
{dynamic_clo}

COURSE METADATA:
{json.dumps({
    "department": metadata.get("department", ""),
    "course_code": metadata.get("course_code", ""),
    "course_title": metadata.get("course_title", ""),
    "course_description": metadata.get("course_description", ""),
    "service_learning_component": metadata.get("service_learning_component", ""),
})}

DYNAMIC CLO ROW PLAN:
Return exactly {len(clo_blueprints)} rows using these CLO codes and pre-assigned Bloom's domains in order:
{json.dumps(clo_plan)}
Full detected CLO row plan, including source-template statements when available:
{json.dumps(clo_blueprints)}
The "domain" field in every returned row MUST exactly match the pre-assigned domain above.
"""
    data = _generate_json(prompt, "copilot_beta_clo")
    shape_check = _validate_ai_response_shape(data, "copilot_beta_clo", expected_clo_count=len(clo_blueprints))
    for warning in shape_check["warnings"]:
        _record_validation_warning(content, f"[AI Shape] {warning}")
    generated_rows = _normalize_ai_row_list(
        data,
        ["clo_alignment_table", "clo_rows", "clos", "course_learning_outcomes", "rows"],
    )
    if not generated_rows:
        raise ValueError("AI did not return recognizable CLO rows.")

    generated_by_code = {}
    for generated in generated_rows:
        if not isinstance(generated, dict):
            continue
        generated_code = str(
            generated.get("clo_code")
            or generated.get("code")
            or generated.get("outcome_code")
            or ""
        ).strip()
        if generated_code:
            generated_by_code[generated_code] = generated

    fallback_rows = {
        row.get("clo_code"): row
        for row in _build_fallback_clo_rows(metadata, clo_blueprints)
        if row.get("clo_code")
    }
    for idx, row in enumerate(content["clo_alignment_table"]):
        default_code = row.get("clo_code") or (clo_blueprints[idx].get("code") if idx < len(clo_blueprints) else f"CLO {idx + 1}")
        default_domain = clo_domains.get(default_code) or row.get("domain") or "cognitive"
        source = generated_by_code.get(default_code) or (generated_rows[idx] if idx < len(generated_rows) else {})
        source = source if isinstance(source, dict) else {}
        fallback = fallback_rows.get(default_code, {})
        row["clo_code"] = default_code
        row["domain"] = _normalize_clo_domain(source.get("domain") or fallback.get("domain"), default_domain)
        row["clo_statement"] = str(
            source.get("clo_statement")
            or source.get("statement")
            or source.get("outcome")
            or source.get("description")
            or row.get("clo_statement")
            or fallback.get("clo_statement")
            or ""
        ).strip()
        if not row["clo_statement"]:
            raise ValueError(f"Unable to produce a fallback CLO statement for: {default_code}.")
        if source.get("clo_statement") or source.get("statement") or source.get("outcome") or source.get("description"):
            row["review_status"] = "ai_draft"
        else:
            row["review_status"] = "fallback_draft"
    _sync_primary_clo_rows_to_groups(content, include_alignment=False)
    _advance_stage(content, "clo_generated")
    return content


def generate_single_clo_with_alignment(content, clo_index, template_context=None):
    """Regenerate a single CLO row and its alignment in one AI call."""
    content = ensure_beta_shape(normalize_beta_content(content))
    metadata = content["metadata"]
    alignment_bundle = get_cached_alignment_bundle(
        content,
        template_context,
        metadata.get("department"),
    )
    program_outcomes = alignment_bundle.get("program_outcomes", [])
    allowed_plos = [row.get("code") for row in program_outcomes if row.get("code")]
    context = alignment_bundle.get("alignment_context", {})

    row_idx = clo_index - 1
    if row_idx < 0 or row_idx >= len(content["clo_alignment_table"]):
        raise ValueError("Invalid CLO row selection.")
    existing_row = content["clo_alignment_table"][row_idx]
    default_code = existing_row.get("clo_code") or f"CLO {clo_index}"
    default_domain = existing_row.get("domain") or "cognitive"
    other_clo_summaries = [
        {"clo_code": r["clo_code"], "domain": r["domain"], "clo_statement": r.get("clo_statement", "")}
        for i, r in enumerate(content["clo_alignment_table"]) if i != row_idx
    ]

    prompt = f"""
You are regenerating a single Course Learning Outcome and its full alignment data.
Return JSON only. Do not include markdown fences, prose, notes, or explanations.

Regenerate ONLY {default_code} (domain: {default_domain}).
The statement must be different from and complementary to the other CLOs listed below.

Rules:
- Return exactly one row with all CLO and alignment fields
- `clo_statement` must be measurable, faculty-ready, and aligned with the course
- All alignment arrays must use ONLY codes/labels from the approved lists
- Each alignment array should contain 2–3 items (1 only if truly single match)
- Do not duplicate alignment patterns already used by other CLOs

COURSE METADATA:
{json.dumps(metadata)}

OTHER CLO ROWS (for context — do NOT regenerate these):
{json.dumps(other_clo_summaries)}

APPROVED PLO CODES:
{json.dumps(allowed_plos)}

APPROVED GRADUATE ATTRIBUTES:
{json.dumps(context.get("sga_options", []))}

APPROVED CORE VALUES:
{json.dumps(context.get("core_value_options", []))}

APPROVED PQF CODES:
{json.dumps(context.get("pqf_level_6_options", []))}

APPROVED AQRF CODES:
{json.dumps(context.get("aqrf_level_6_options", []))}

APPROVED SDG CODES:
{json.dumps(context.get("sdg_options", []))}

TEMPLATE INSTITUTIONAL CONTEXT:
{json.dumps(context.get("institutional_context", {}))}

{_context_source_note(alignment_bundle)}

Return this exact JSON shape:
{{
  "clo_code": "{default_code}",
  "domain": "{default_domain}",
  "clo_statement": "",
  "aligned_plos": [],
  "graduate_attributes": [],
  "core_values": [],
  "pqf_level_6_alignment": [],
  "aqrf_level_6_alignment": [],
  "relevant_sdgs": []
}}
"""
    data = _generate_json(prompt, "copilot_beta_single_clo")
    source = data if isinstance(data, dict) else {}
    if isinstance(data, dict) and not source.get("clo_statement"):
        rows = _normalize_ai_row_list(data, ["clo_alignment_table", "clo_rows", "rows"])
        source = rows[0] if rows else source

    statement = str(
        source.get("clo_statement")
        or source.get("statement")
        or source.get("outcome")
        or source.get("description")
        or ""
    ).strip()
    if not statement:
        raise ValueError(f"AI did not return a CLO statement for {default_code}.")

    target_row = content["clo_alignment_table"][row_idx]
    target_row["clo_code"] = default_code
    target_row["domain"] = _normalize_clo_domain(source.get("domain"), default_domain)
    target_row["clo_statement"] = statement
    target_row["aligned_plos"] = _normalize_preferred_alignment_selection(
        source.get("aligned_plos", []), allowed_plos,
        fallback_values=_ranked_plo_fallbacks(target_row, allowed_plos, program_outcomes=program_outcomes, metadata=metadata),
        field_name="aligned_plos", row=target_row, program_outcomes=program_outcomes, metadata=metadata,
    )
    target_row["graduate_attributes"] = _normalize_preferred_alignment_selection(
        source.get("graduate_attributes", []), context["sga_options"],
        fallback_values=[], field_name="graduate_attributes", row=target_row,
        program_outcomes=program_outcomes, metadata=metadata,
    )
    target_row["core_values"] = _normalize_preferred_alignment_selection(
        source.get("core_values", []), context["core_value_options"],
        fallback_values=[], field_name="core_values", row=target_row,
        program_outcomes=program_outcomes, metadata=metadata,
    )
    target_row["pqf_level_6_alignment"] = _normalize_preferred_alignment_selection(
        source.get("pqf_level_6_alignment", []), context["pqf_level_6_options"],
        fallback_values=[], field_name="pqf_level_6_alignment", row=target_row,
        program_outcomes=program_outcomes, metadata=metadata,
    )
    target_row["aqrf_level_6_alignment"] = _normalize_preferred_alignment_selection(
        source.get("aqrf_level_6_alignment", []), context["aqrf_level_6_options"],
        fallback_values=[], field_name="aqrf_level_6_alignment", row=target_row,
        program_outcomes=program_outcomes, metadata=metadata,
    )
    target_row["relevant_sdgs"] = _normalize_preferred_alignment_selection(
        source.get("relevant_sdgs", []), context["sdg_options"],
        fallback_values=[], field_name="relevant_sdgs", row=target_row,
        program_outcomes=program_outcomes, metadata=metadata,
    )
    target_row["review_status"] = "ai_draft"
    _sync_primary_clo_rows_to_groups(content, include_alignment=False)

    if not content["metadata"].get("target_sdgs_display"):
        suggested = _normalize_limited_selection(
            source.get("target_sdgs_display", source.get("relevant_sdgs", [])),
            context["sdg_options"], fallback_values=[], minimum=0, maximum=ALIGNMENT_MAX_ITEMS,
        )
        if suggested:
            content["metadata"]["target_sdgs_display"] = ", ".join(suggested)

    return content


def _generate_checkmark_alignment(content, template_context=None):
    """Generate checkmark-style alignment: CLO x PO matrix with boolean checkmarks."""
    metadata = content["metadata"]
    fallback_po_codes = _get_checkmark_po_codes(content)
    if not fallback_po_codes:
        raise ValueError("Checkmark alignment requires PO column codes from the template profile but none were detected.")

    alignment_bundle = get_cached_alignment_bundle(
        content,
        template_context,
        metadata.get("department"),
    )
    default_program_outcomes = alignment_bundle.get("program_outcomes", [])
    dynamic_am = _get_dynamic_prompt(content, "alignment_checkmark")
    default_prompt = dynamic_am

    groups = content.get("clo_alignment_groups") if isinstance(content.get("clo_alignment_groups"), list) else []
    if not groups:
        groups = [{
            "group_id": "clo_group_1",
            "label": "CLO Alignment",
            "program_scope": {},
            "checkmark_po_codes": fallback_po_codes,
            "clo_alignment_table": content.get("clo_alignment_table", []),
        }]

    def _checked_codes_from_source(source, row, allowed_codes, program_outcomes):
        checked_pos = []
        if isinstance(source, dict):
            checked_pos = source.get("checked_pos") or source.get("aligned_pos") or source.get("po_codes") or []
        if isinstance(checked_pos, str):
            checked_pos = _csv_or_lines_to_list(checked_pos)
        checked = [str(po).strip() for po in checked_pos if str(po).strip() in allowed_codes]
        if checked:
            if len(allowed_codes) >= 4 and len(set(checked)) >= len(allowed_codes) - 1:
                existing_checks = row.get("checkmark_alignments") if isinstance(row.get("checkmark_alignments"), dict) else {}
                return [code for code in allowed_codes if _to_bool(existing_checks.get(code, False))]
            if ALIGNMENT_MAX_ITEMS > 0:
                checked = checked[:ALIGNMENT_MAX_ITEMS]
            return checked
        existing_checks = row.get("checkmark_alignments") if isinstance(row.get("checkmark_alignments"), dict) else {}
        return [code for code in allowed_codes if _to_bool(existing_checks.get(code, False))]

    runtime_groups = []
    prompt_groups = []
    for idx, group in enumerate(groups):
        group_po_codes = _group_checkmark_po_codes(group, fallback_po_codes)
        if not group_po_codes:
            continue
        group_id = group.get("group_id") or group.get("id") or f"clo_group_{idx + 1}"
        group["group_id"] = group_id
        group_program_outcomes = _group_program_outcomes(group, default_program_outcomes)
        clo_rows = group.get("clo_alignment_table") if isinstance(group.get("clo_alignment_table"), list) else []
        clo_summaries = [
            {
                "clo_code": row.get("clo_code"),
                "clo_statement": row.get("clo_statement"),
                "domain": row.get("domain"),
            }
            for row in clo_rows
            if isinstance(row, dict)
        ]
        runtime_groups.append((group, group_id, group_po_codes, clo_rows))
        prompt_groups.append({
            "group_id": group_id,
            "label": group.get("label") or "CLO Alignment",
            "program_scope": group.get("program_scope", {}),
            "po_codes": group_po_codes,
            "program_outcomes": group_program_outcomes,
            "clo_rows": clo_summaries,
        })

    def _alignment_matrix_for_group(data, group_id, label, allow_single_group=False):
        if isinstance(data, list):
            return data if allow_single_group else []
        if not isinstance(data, dict):
            return []
        grouped = data.get("alignment_groups") or data.get("groups") or []
        if isinstance(grouped, dict):
            grouped = [
                {"group_id": key, **value} if isinstance(value, dict) else {"group_id": key, "alignment_matrix": value}
                for key, value in grouped.items()
            ]
        if isinstance(grouped, list):
            for item in grouped:
                if not isinstance(item, dict):
                    continue
                item_id = str(item.get("group_id") or item.get("id") or "").strip()
                item_label = str(item.get("label") or "").strip()
                if item_id == group_id or (label and item_label == label):
                    matrix = (
                        item.get("alignment_matrix")
                        or item.get("alignments")
                        or item.get("rows")
                        or item.get("clo_alignment_table")
                        or []
                    )
                    return matrix if isinstance(matrix, list) else []
        direct = data.get(group_id)
        if isinstance(direct, list):
            return direct
        if isinstance(direct, dict):
            matrix = direct.get("alignment_matrix") or direct.get("alignments") or direct.get("rows") or []
            return matrix if isinstance(matrix, list) else []
        if allow_single_group:
            matrix = data.get("alignment_matrix") or data.get("alignments") or data.get("rows") or []
            return matrix if isinstance(matrix, list) else []
        return []

    if runtime_groups:
        prompt = f"""\
{default_prompt}

COURSE METADATA:
{json.dumps(metadata)}

ALIGNMENT GROUPS (MANDATORY, process every group exactly once):
{json.dumps(prompt_groups)}

Return one "alignment_groups" item for each input group_id.
Each group's alignment_matrix must contain one entry per CLO row, in the same order as that group's clo_rows.
Each entry must contain "clo_code" and "checked_pos".
Use only the po_codes listed inside the same group. Never copy PO codes from another group.
Each CLO typically aligns with 4-7 POs. Be selective — only check POs that have a clear, defensible connection to the CLO statement. Do NOT check all POs for every CLO.
"""
        try:
            data = _generate_json(prompt, "copilot_beta_checkmark_alignment")
        except Exception as exc:
            _record_validation_warning(content, f"[AI Fallback] Checkmark alignment generation failed; fallback rows were kept. ({exc})")
            data = {}

        combined_matrix = []
        for group, group_id, _group_po_codes, clo_rows in runtime_groups:
            matrix = _alignment_matrix_for_group(
                data,
                group_id,
                str(group.get("label") or ""),
                allow_single_group=len(runtime_groups) == 1,
            )
            combined_matrix.extend(matrix)
        shape_check = _validate_ai_response_shape(
            {"alignment_matrix": combined_matrix},
            "copilot_beta_alignment",
            expected_clo_count=sum(len(item[3]) for item in runtime_groups),
        )
        for warning in shape_check["warnings"]:
            _record_validation_warning(content, f"[AI Shape] {warning}")

        fallback_row_count = 0
        for group, group_id, group_po_codes, clo_rows in runtime_groups:
            alignment_matrix = _alignment_matrix_for_group(
                data,
                group_id,
                str(group.get("label") or ""),
                allow_single_group=len(runtime_groups) == 1,
            )
            if not isinstance(alignment_matrix, list):
                alignment_matrix = []

            generated_by_code = {}
            for entry in alignment_matrix:
                if isinstance(entry, dict):
                    code = str(entry.get("clo_code") or "").strip()
                    if code:
                        generated_by_code[code] = entry

            for row_idx, row in enumerate(clo_rows):
                if not isinstance(row, dict):
                    continue
                row_code = str(row.get("clo_code") or "").strip()
                source = generated_by_code.get(row_code) or (
                    alignment_matrix[row_idx]
                    if row_idx < len(alignment_matrix) and isinstance(alignment_matrix[row_idx], dict)
                    else {}
                )
                checked = _checked_codes_from_source(source, row, group_po_codes, group_program_outcomes)
                checked_set = set(checked)
                row["checkmark_alignments"] = {
                    po: po in checked_set
                    for po in group_po_codes
                }
                row["review_status"] = "ai_draft" if source else "fallback_alignment"
                if not source:
                    fallback_row_count += 1

        if fallback_row_count:
            _record_validation_warning(content, f"[Alignment Fallback] Checkmark alignment used fallback rows for {fallback_row_count} CLO(s).")

    content["clo_alignment_groups"] = groups
    if groups:
        content["clo_alignment_table"] = groups[0].get("clo_alignment_table", content.get("clo_alignment_table", []))
    _advance_stage(content, "alignment_generated")
    return content


def _generate_program_institutional_alignment(content, template_context=None):
    specs = _program_institutional_specs(content)
    if not specs:
        return content
    metadata = content.get("metadata", {})
    alignment_bundle = get_cached_alignment_bundle(
        content,
        template_context,
        metadata.get("department"),
    )
    template_context = template_context if isinstance(template_context, dict) else {}
    contracts = [
        {
            "id": spec.get("id"),
            "alignment_format": spec.get("alignment_format"),
            "value_style": "checkmark",
            "row_labels_original": spec.get("row_labels_original") or [],
            "row_labels_normalized": spec.get("row_labels_normalized") or [],
            "column_labels_original": spec.get("column_labels_original") or [],
            "column_labels_normalized": spec.get("column_labels_normalized") or [],
            "required_alignment_keys": spec.get("required_alignment_keys") or [],
        }
        for spec in specs
    ]
    def _context_entries(items):
        entries = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or item.get("id") or item.get("label") or "").strip()
            description = str(
                item.get("description")
                or item.get("text")
                or item.get("title")
                or item.get("statement")
                or ""
            ).strip()
            if code or description:
                entries.append({"code": code, "description": description})
        return entries

    def _tokens(value):
        return {
            token
            for token in re.findall(r"[A-Za-z][A-Za-z0-9]+", str(value or "").lower())
            if len(token) >= 3
        }

    def _row_context_text(row_key, program_entries):
        key_upper = str(row_key or "").upper()
        for entry in program_entries:
            code = str(entry.get("code") or "").upper()
            if code and (key_upper == code or key_upper.startswith(code) or code.startswith(key_upper)):
                return f"{entry.get('code', '')} {entry.get('description', '')}".strip()
        return key_upper

    def _col_context_text(col_key, institutional_entries):
        key_upper = str(col_key or "").upper()
        for entry in institutional_entries:
            code = str(entry.get("code") or "").upper()
            if code and (key_upper == code or key_upper.startswith(code) or code.startswith(key_upper)):
                return f"{entry.get('code', '')} {entry.get('description', '')}".strip()
        return key_upper

    def _choose_fallback_columns(row_key, col_keys, institutional_entries, row_text):
        row_tokens = _tokens(row_text)
        scored = []
        for col_key in col_keys:
            col_text = _col_context_text(col_key, institutional_entries)
            col_tokens = _tokens(col_text)
            overlap = len(row_tokens & col_tokens)
            scored.append((overlap, col_key))
        scored.sort(key=lambda item: (-item[0], item[1]))
        if not scored:
            return []
        if scored[0][0] > 0:
            top = [col for score, col in scored if score == scored[0][0]]
            return top
        # deterministic safety fallback: keep at least one checked relation
        return [scored[0][1]]

    program_entries = _context_entries(
        template_context.get("program_outcomes") or alignment_bundle.get("program_outcomes") or []
    )
    institutional_entries = _context_entries(
        template_context.get("institutional_outcomes")
        or (template_context.get("institutional_context") or {}).get("institutional_outcomes")
        or []
    )

    dynamic_po_io = _get_dynamic_prompt(content, "po_io")
    system_part = dynamic_po_io

    prompt = f"""\
{system_part}

COURSE METADATA:
{json.dumps(metadata)}

TEMPLATE MATRIX CONTRACTS:
{json.dumps(contracts)}

PROGRAM OUTCOMES CONTEXT:
{json.dumps(template_context.get("program_outcomes") or alignment_bundle.get("program_outcomes") or [])}

INSTITUTIONAL OUTCOMES CONTEXT:
{json.dumps(template_context.get("institutional_outcomes") or (template_context.get("institutional_context") or {}).get("institutional_outcomes") or [])}

Return exactly this JSON shape:
{{
  "program_institutional_alignments": {{
    "program_institutional_alignment_1": {{
      "ROWKEY": {{
        "COLKEY": "✔"
      }}
    }}
  }}
}}
"""
    ai_generated = True
    try:
        data = _generate_json(prompt, "copilot_beta_program_institutional_alignment")
    except ValueError as exc:
        ai_generated = False
        data = {"program_institutional_alignments": {}}
        current_app.logger.warning(
            "AI program-institutional alignment failed; using deterministic fallback: %s",
            exc,
        )
        _record_validation_warning(
            content,
            "AI program-institutional alignment returned invalid JSON, so a deterministic template-profile fallback was used.",
        )
    if ai_generated:
        shape_check = _validate_ai_response_shape(data, "copilot_beta_alignment")
        for warning in shape_check["warnings"]:
            _record_validation_warning(content, f"[AI Shape] {warning}")
    generated = data.get("program_institutional_alignments") if isinstance(data, dict) else {}
    if not generated and isinstance(data, dict):
        spec_ids = {str(item.get("id") or "") for item in specs if isinstance(item, dict)}
        if any(key in spec_ids for key in data.keys()):
            generated = data
    if not isinstance(generated, dict):
        generated = {}
    if not generated:
        _record_validation_warning(
            content,
            "AI did not return recognizable program-institutional checkmark data, so a deterministic template-profile fallback was used.",
        )
    normalized_matrix = _empty_program_institutional_alignments(_get_generation_spec(content), generated)
    # Ensure relationship coverage exists even when AI returns sparse/blank matrices.
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        alignment_id = spec.get("id") or ""
        if not alignment_id or alignment_id not in normalized_matrix:
            continue
        row_keys = [
            normalize_alignment_row_label(item)
            for item in (spec.get("row_labels_normalized") or [])
            if normalize_alignment_row_label(item)
        ]
        col_keys = [
            normalize_alignment_column_label(item)
            for item in (spec.get("column_labels_normalized") or [])
            if normalize_alignment_column_label(item)
        ]
        matrix = normalized_matrix.get(alignment_id) if isinstance(normalized_matrix.get(alignment_id), dict) else {}
        for row_key in row_keys:
            row_values = matrix.get(row_key) if isinstance(matrix.get(row_key), dict) else {}
            has_any = any(_to_checkmark_value(row_values.get(col_key, "")) == "✔" for col_key in col_keys)
            if has_any:
                continue
            row_text = _row_context_text(row_key, program_entries)
            fallback_cols = _choose_fallback_columns(row_key, col_keys, institutional_entries, row_text)
            for col_key in col_keys:
                row_values[col_key] = "✔" if col_key in fallback_cols else ""
            matrix[row_key] = row_values
        normalized_matrix[alignment_id] = matrix
    content["program_institutional_alignments"] = normalized_matrix
    _advance_stage(content, "alignment_generated")
    return content


def generate_beta_alignment(content, template_context=None):
    content = ensure_beta_shape(normalize_beta_content(content))
    alignment_style = _get_alignment_style(content)
    spec = _get_generation_spec(content)
    has_clo_groups = bool(spec.get("clo_groups"))
    if _program_institutional_specs(content):
        content = _generate_program_institutional_alignment(content, template_context=template_context)
        if not has_clo_groups:
            return content

    # --- Checkmark alignment generation ---
    if alignment_style == "checkmark":
        return _generate_checkmark_alignment(content, template_context=template_context)

    # --- CLO-based alignment generation (original logic) ---
    metadata = content["metadata"]
    alignment_bundle = get_cached_alignment_bundle(
        content,
        template_context,
        metadata.get("department"),
    )
    program_outcomes = alignment_bundle.get("program_outcomes", [])
    allowed_plos = [row.get("code") for row in program_outcomes if row.get("code")]
    context = alignment_bundle.get("alignment_context", {})
    dynamic_al = _get_dynamic_prompt(content, "alignment_clo_based")
    default_prompt = dynamic_al

    def _scope_program_outcomes(group):
        scope = group.get("program_scope") if isinstance(group.get("program_scope"), dict) else {}
        items = scope.get("items") if isinstance(scope.get("items"), list) else []
        scoped = []
        for item in items:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            if code:
                scoped.append({"code": code, "description": str(item.get("description") or item.get("title") or "").strip()})
        return scoped or program_outcomes

    def _generate_for_group(group):
        group_rows = group.get("clo_alignment_table") if isinstance(group.get("clo_alignment_table"), list) else []
        group_program_outcomes = _scope_program_outcomes(group)
        group_allowed_plos = [row.get("code") for row in group_program_outcomes if row.get("code")] or allowed_plos
        prompt = f"""
{default_prompt}

COURSE METADATA:
{json.dumps(metadata)}

PROGRAM / ALIGNMENT GROUP:
{json.dumps({"label": group.get("label"), "program_scope": group.get("program_scope", {})})}

APPROVED PLO CODES (MANDATORY — aligned_plos must ONLY use codes from this list):
{json.dumps(group_allowed_plos)}

PROGRAM LEARNING OUTCOMES FOR THIS GROUP:
{json.dumps(group_program_outcomes)}

CURRENT CLO ROWS:
{json.dumps(group_rows)}

APPROVED GRADUATE ATTRIBUTES:
{json.dumps(context["sga_options"])}

GRADUATE ATTRIBUTE MEANINGS:
{json.dumps(context.get("sga_details", []))}

APPROVED CORE VALUES:
{json.dumps(context["core_value_options"])}

CORE VALUE MEANINGS:
{json.dumps(context.get("core_value_details", []))}

APPROVED PQF CODES:
{json.dumps(context["pqf_level_6_options"])}

PQF LEVEL 6 DETAILS:
{json.dumps(context.get("pqf_level_6_details", []))}

APPROVED AQRF CODES:
{json.dumps(context["aqrf_level_6_options"])}

AQRF LEVEL 6 DETAILS:
{json.dumps(context.get("aqrf_level_6_details", []))}

APPROVED SDG CODES:
{json.dumps(context["sdg_options"])}

APPROVED SDG GUIDANCE:
{json.dumps(context["sdg_context"])}

COURSE TARGET SDGs (MANDATORY — relevant_sdgs must ONLY reference SDGs from this list):
{json.dumps(_csv_or_lines_to_list(metadata.get('target_sdgs_display', [])))}
Do NOT reference any SDG not listed above.

TEMPLATE INSTITUTIONAL CONTEXT:
{json.dumps(context.get("institutional_context", {}))}

{_context_source_note(alignment_bundle)}

Return exactly {len(group_rows)} alignment rows in the same order as CURRENT CLO ROWS.
Do NOT invent PLO codes — every code in aligned_plos must appear in APPROVED PLO CODES above.
{_generation_shape_contract_text(content)}
{_template_seed_context_text(content)}
"""
        data = _generate_json(prompt, "copilot_beta_alignment")
        shape_check = _validate_ai_response_shape(data, "copilot_beta_alignment", expected_clo_count=len(group_rows))
        for warning in shape_check["warnings"]:
            _record_validation_warning(content, f"[AI Shape] {warning}")
        generated_rows = _normalize_ai_row_list(
            data,
            ["clo_alignment_table", "alignment_rows", "clo_rows", "rows", "mappings"],
        )
        if not generated_rows:
            raise ValueError(f"AI did not return recognizable alignment rows for {group.get('label') or 'the selected group'}.")
        fallback_content = {**content, "clo_alignment_table": group_rows}
        fallback_rows = _build_fallback_alignment_rows(fallback_content, group_allowed_plos, context)
        generated_by_code = {
            str(
                row.get("clo_code")
                or row.get("code")
                or row.get("outcome_code")
                or ""
            ).strip(): row
            for row in generated_rows
            if isinstance(row, dict)
            and str(row.get("clo_code") or row.get("code") or row.get("outcome_code") or "").strip()
        }

        for idx, row in enumerate(group_rows):
            row_code = str(row.get("clo_code") or "").strip()
            source = generated_by_code.get(row_code) or (generated_rows[idx] if idx < len(generated_rows) and isinstance(generated_rows[idx], dict) else {})
            fallback_row = fallback_rows[idx] if idx < len(fallback_rows) else {}
            row["aligned_plos"] = _normalize_preferred_alignment_selection(
                source.get("aligned_plos", row.get("aligned_plos", [])),
                group_allowed_plos,
                fallback_values=_ranked_plo_fallbacks(row, group_allowed_plos, program_outcomes=group_program_outcomes, metadata=metadata),
                field_name="aligned_plos",
                row=row,
                program_outcomes=group_program_outcomes,
                metadata=metadata,
            )
            row["graduate_attributes"] = _normalize_preferred_alignment_selection(
                source.get("graduate_attributes", row.get("graduate_attributes", [])),
                context["sga_options"],
                fallback_values=fallback_row.get("graduate_attributes", []),
                field_name="graduate_attributes",
                row=row,
                program_outcomes=group_program_outcomes,
                metadata=metadata,
            )
            row["core_values"] = _normalize_preferred_alignment_selection(
                source.get("core_values", row.get("core_values", [])),
                context["core_value_options"],
                fallback_values=fallback_row.get("core_values", []),
                field_name="core_values",
                row=row,
                program_outcomes=group_program_outcomes,
                metadata=metadata,
            )
            row["pqf_level_6_alignment"] = _normalize_preferred_alignment_selection(
                source.get("pqf_level_6_alignment", row.get("pqf_level_6_alignment", [])),
                context["pqf_level_6_options"],
                fallback_values=fallback_row.get("pqf_level_6_alignment", []),
                field_name="pqf_level_6_alignment",
                row=row,
                program_outcomes=group_program_outcomes,
                metadata=metadata,
            )
            row["aqrf_level_6_alignment"] = _normalize_preferred_alignment_selection(
                source.get("aqrf_level_6_alignment", row.get("aqrf_level_6_alignment", [])),
                context["aqrf_level_6_options"],
                fallback_values=fallback_row.get("aqrf_level_6_alignment", []),
                field_name="aqrf_level_6_alignment",
                row=row,
                program_outcomes=group_program_outcomes,
                metadata=metadata,
            )
            row["relevant_sdgs"] = _normalize_preferred_alignment_selection(
                source.get("relevant_sdgs", row.get("relevant_sdgs", [])),
                context["sdg_options"],
                fallback_values=fallback_row.get("relevant_sdgs", []),
                field_name="relevant_sdgs",
                row=row,
                program_outcomes=group_program_outcomes,
                metadata=metadata,
            )
            row["review_status"] = "ai_draft" if source else row.get("review_status", "pending")
        return data

    generated_payloads = []
    groups = content.get("clo_alignment_groups") if isinstance(content.get("clo_alignment_groups"), list) else []
    if not groups:
        groups = [{"group_id": "clo_group_1", "label": "CLO Alignment", "program_scope": {}, "clo_alignment_table": content["clo_alignment_table"]}]
    for group in groups:
        generated_payloads.append(_generate_for_group(group))
    content["clo_alignment_groups"] = groups
    content["clo_alignment_table"] = groups[0].get("clo_alignment_table", content["clo_alignment_table"])

    existing_target_sdgs = _csv_or_lines_to_list(content["metadata"].get("target_sdgs_display", ""))
    if not existing_target_sdgs:
        for data in generated_payloads:
            suggested_target_sdgs = _normalize_limited_selection(
                data.get("target_sdgs_display", []) if isinstance(data, dict) else [],
                context["sdg_options"],
                fallback_values=[],
                minimum=0,
                maximum=ALIGNMENT_MAX_ITEMS,
            )
            if suggested_target_sdgs:
                content["metadata"]["target_sdgs_display"] = ", ".join(suggested_target_sdgs)
                break
    content = _rebalance_alignment_repetition(content, allowed_plos, context, program_outcomes=program_outcomes)
    groups_for_validation = content.get("clo_alignment_groups") if isinstance(content.get("clo_alignment_groups"), list) else []
    for group in groups_for_validation:
        group_rows = group.get("clo_alignment_table") if isinstance(group.get("clo_alignment_table"), list) else []
        if not group_rows:
            continue
        group_program_outcomes = _scope_program_outcomes(group)
        group_allowed_plos = [row.get("code") for row in group_program_outcomes if row.get("code")] or allowed_plos
        fallback_content = {**content, "clo_alignment_table": group_rows}
        fallback_rows = _build_fallback_alignment_rows(fallback_content, group_allowed_plos, context)
        for idx, row in enumerate(group_rows):
            if not isinstance(row, dict):
                continue
            fallback_row = fallback_rows[idx] if idx < len(fallback_rows) else {}
            for field_name in (
                "aligned_plos",
                "graduate_attributes",
                "core_values",
                "pqf_level_6_alignment",
                "aqrf_level_6_alignment",
                "relevant_sdgs",
            ):
                if not _csv_or_lines_to_list(row.get(field_name, [])) and fallback_row.get(field_name):
                    row[field_name] = fallback_row.get(field_name)
                    row["review_status"] = "fallback_alignment"

    missing_alignment_fields = []
    rows_for_validation = []
    if groups_for_validation:
        for group in groups_for_validation:
            group_label = group.get("label") or "CLO group"
            for row in group.get("clo_alignment_table", []) or []:
                rows_for_validation.append((group_label, row))
    else:
        rows_for_validation = [("CLO group", row) for row in content["clo_alignment_table"]]
    for group_label, row in rows_for_validation:
        for field_name in (
            "aligned_plos",
            "graduate_attributes",
            "core_values",
            "pqf_level_6_alignment",
            "aqrf_level_6_alignment",
            "relevant_sdgs",
        ):
            if not _csv_or_lines_to_list(row.get(field_name, [])):
                missing_alignment_fields.append(f"{group_label} {row.get('clo_code', 'Unknown CLO')} {field_name}")
    if missing_alignment_fields:
        _record_validation_warning(content, f"[Alignment Fallback] AI did not fully populate alignment fields: {', '.join(missing_alignment_fields[:8])}.")
    insufficient_alignment_fields = _collect_alignment_breadth_issues(content, program_outcomes, context)
    if insufficient_alignment_fields:
        _record_validation_warning(
            content,
            "[Alignment Fallback] AI returned overly narrow alignment selections for these CLO rows: "
            + "; ".join(insufficient_alignment_fields[:4])
        )
    if not _csv_or_lines_to_list(content["metadata"].get("target_sdgs_display", [])):
        row_sdgs = []
        for _group_label, row in rows_for_validation:
            row_sdgs.extend(_csv_or_lines_to_list(row.get("relevant_sdgs", [])))
        suggested_target_sdgs = _normalize_limited_selection(
            row_sdgs,
            context["sdg_options"],
            fallback_values=[],
            minimum=0,
            maximum=ALIGNMENT_MAX_ITEMS,
        )
        if suggested_target_sdgs:
            content["metadata"]["target_sdgs_display"] = ", ".join(suggested_target_sdgs)
        else:
            _record_validation_warning(content, "[Alignment Fallback] AI did not provide course-level target SDGs.")
    _advance_stage(content, "alignment_generated")
    return content


def generate_beta_weekly(content, template_profile_hints=None, template_context=None):
    content = ensure_beta_shape(normalize_beta_content(content))
    metadata = content["metadata"]
    week_labels = [row.get("time_frame_label") for row in content.get("weekly_course_outline", []) if row.get("time_frame_label")]
    alignment_bundle = get_cached_alignment_bundle(
        content,
        template_context,
        metadata.get("department"),
    )
    alignment_context = alignment_bundle.get("alignment_context") if isinstance(alignment_bundle.get("alignment_context"), dict) else {}
    target_sdgs = _csv_or_lines_to_list(metadata.get("target_sdgs_display", []))
    sdg_context = alignment_context.get("sdg_context", SDG_CONTEXT)
    reference_candidates = _course_reference_candidates(metadata)
    clo_rows = [
        {
            "clo_code": row["clo_code"],
            "domain": row["domain"],
            "clo_statement": row["clo_statement"],
            "relevant_sdgs": row.get("relevant_sdgs", []),
        }
        for row in content["clo_alignment_table"]
    ]
    dynamic_wk = _get_dynamic_prompt(content, "weekly")
    default_prompt = dynamic_wk
    prompt = f"""
{default_prompt}

COURSE METADATA:
{json.dumps(metadata)}

APPROVED CLO ROWS:
{json.dumps(clo_rows)}

WEEK LABELS:
{json.dumps(week_labels)}

TARGET SDGS FOR COURSE:
{json.dumps(target_sdgs)}

COURSE REFERENCE CANDIDATES:
{json.dumps(reference_candidates)}

Write faculty-ready content aligned to the course and CLOs.
Use the references as anchors and prefer concrete citations/URLs over placeholders.
Final output checklist:
DYNAMIC WEEKLY ROW PLAN:
- Return exactly {len(week_labels)} objects in `weekly_course_outline`
- Use these labels exactly once each and in this order: {json.dumps(week_labels)}
- Missing even one label makes the answer invalid
- Do not stop early
- Do not summarize
- Do not compress multiple required weeks into one row
- Follow `weekly_format_hints` from the template shape: if a field is `flat_lines`, return simple list items for that field instead of inventing category labels; if it is `categorized`, keep the expected categories populated.
{_context_source_note(alignment_bundle)}
"""
    def _extract_weekly_rows(payload):
        return _normalize_ai_row_list(
            payload,
            ["weekly_course_outline", "weekly_rows", "course_outline", "rows"],
        )

    data = _generate_json(prompt, "copilot_beta_weekly")
    shape_check = _validate_ai_response_shape(data, "copilot_beta_weekly", expected_week_labels=week_labels)
    for warning in shape_check["warnings"]:
        _record_validation_warning(content, f"[AI Shape] {warning}")
    generated_rows = _extract_weekly_rows(data)
    if not generated_rows:
        raise ValueError("AI did not return recognizable weekly rows.")

    # Apply all generated rows, falling back to saved content for missing labels.
    for row_spec in content["weekly_course_outline"]:
        label = str(row_spec.get("time_frame_label") or "")
        source = None
        for gr in generated_rows:
            if isinstance(gr, dict) and str(gr.get("time_frame_label") or "") == label:
                source = gr
                break
        if source is None:
            fallback = _build_fallback_week_row(content, label)
            fallback["_fallback_source"] = True
            generated_rows.append(fallback)

    generated_by_label = {
        str(row.get("time_frame_label") or ""): row
        for row in generated_rows
        if isinstance(row, dict) and row.get("time_frame_label")
    }
    
    # Auto-heal: If a week has topics but AI forgot to map CLOs, and we have CLOs available,
    # assign the first CLO as a fallback.
    clo_codes = [row.get("clo_code") for row in content.get("clo_alignment_table", []) if row.get("clo_code")]
    
    def _truncate_row_fields(row, hints):
        """Truncate each list field in row to the template's typical_item_count cap.
        Respects output_style from format hints: flat_lines vs categorized."""
        for field, cap_key in [
            ("topics", "topics"),
            ("assessment", "assessment"),
            ("intended_learning_outcomes", "intended_learning_outcomes"),
            ("teaching_learning_activities", "teaching_learning_activities"),
            ("learning_resources", "learning_resources"),
        ]:
            if not isinstance(hints, dict):
                continue
            hint = hints.get(cap_key) or {}
            if not isinstance(hint, dict):
                continue
            cap = hint.get("typical_item_count", 0)
            if cap <= 0:
                continue
            style = hint.get("output_style", "flat_lines")
            is_categorized = (style == "categorized")
            if is_categorized:
                val = row.get(field)
                if isinstance(val, dict):
                    cats = hint.get("category_labels", []) or []
                    n_cats = len(cats)
                    if n_cats > 0:
                        per_cat = max(1, cap // n_cats)
                        remainder = cap - per_cat * n_cats
                        for i, cat in enumerate(cats):
                            cat_key = cat.lower().replace(" ", "_")
                            items = _csv_or_lines_to_list(val.get(cat_key, []))
                            budget = per_cat + (1 if i < remainder else 0)
                            val[cat_key] = items[:budget]
            else:
                val = row.get(field)
                if isinstance(val, list):
                    row[field] = val[:cap]
                elif isinstance(val, str):
                    parts = [p.strip() for p in val.split("\n") if p.strip()]
                    row[field] = "\n".join(parts[:cap])

    def _apply_generated_rows(source_map):
        missing_week_labels = []
        unrecoverable_missing_week_labels = []
        low_quality_week_issues = []
        low_quality_issue_map = {}
        previous_teaching_row = None
        # Extract format_hints for quality-check awareness of flat vs categorized
        q_spec = content.get("template_generation_spec", {})
        q_wo = q_spec.get("weekly_outline", {}) if isinstance(q_spec, dict) else {}
        q_hints = q_wo.get("format_hints", {}) if isinstance(q_wo, dict) else {}
        q_fields = q_hints.get("fields", {}) if isinstance(q_hints, dict) else {}
        for row in content["weekly_course_outline"]:
            source = source_map.get(row["time_frame_label"])
            if not source:
                missing_week_labels.append(row["time_frame_label"])
                if not _weekly_row_has_meaningful_content(row):
                    unrecoverable_missing_week_labels.append(row["time_frame_label"])
                row["review_status"] = row.get("review_status", "pending")
                continue
            source_ilo = source.get("intended_learning_outcomes", "")
            # Check format hints to decide output style for ILO
            ilo_hint = q_fields.get("intended_learning_outcomes", {}) if isinstance(q_fields, dict) else {}
            ilo_style = ilo_hint.get("output_style", "flat_lines") if isinstance(ilo_hint, dict) else "flat_lines"
            ilo_lead_template = ilo_hint.get("lead_in", "") if isinstance(ilo_hint, dict) else ""
            is_ilo_flat = (ilo_style == "flat_lines")

            if isinstance(source_ilo, str):
                row["intended_learning_outcomes"] = source_ilo.strip()
            elif isinstance(source_ilo, list):
                row["intended_learning_outcomes"] = source_ilo[:]
            elif isinstance(source_ilo, dict):
                if is_ilo_flat:
                    # Flat style: extract items from all categories into a flat list
                    all_items = []
                    for dk in ["cognitive", "affective", "psychomotor"]:
                        items = _csv_or_lines_to_list(source_ilo.get(dk, []))
                        all_items.extend(items)
                    # Use the template's lead-in from format hints if available
                    template_lead = ilo_lead_template or source_ilo.get("lead_in", "")
                    if template_lead:
                        result = template_lead.strip()
                        if all_items:
                            result += "\n" + "\n".join(all_items)
                        row["intended_learning_outcomes"] = result
                    else:
                        # Always produce a flat string for flat-style templates
                        row["intended_learning_outcomes"] = "\n".join(all_items) if all_items else ""
                else:
                    # Categorized style: preserve the AI's categorized output
                    row["intended_learning_outcomes"]["lead_in"] = str(
                        ilo_lead_template  # Prefer the template's detected lead-in
                        or source_ilo.get("lead_in")
                        or row["intended_learning_outcomes"].get("lead_in")
                        or "At the end of the week, students should have the ability to:"
                    ).strip()
                    row["intended_learning_outcomes"]["cognitive"] = _csv_or_lines_to_list(source_ilo.get("cognitive", row["intended_learning_outcomes"].get("cognitive", [])))
                    row["intended_learning_outcomes"]["affective"] = _csv_or_lines_to_list(source_ilo.get("affective", row["intended_learning_outcomes"].get("affective", [])))
                    row["intended_learning_outcomes"]["psychomotor"] = _csv_or_lines_to_list(source_ilo.get("psychomotor", row["intended_learning_outcomes"].get("psychomotor", [])))
            row["mapped_clos"] = _normalize_mapped_clos(source.get("mapped_clos", row.get("mapped_clos", [])))
            row["topics"] = _csv_or_lines_to_list(source.get("topics", row.get("topics", [])))
            source_tla = source.get("teaching_learning_activities", "")
            # Check format hints for TLA style
            tla_hint = q_fields.get("teaching_learning_activities", {}) if isinstance(q_fields, dict) else {}
            tla_style = tla_hint.get("output_style", "flat_lines") if isinstance(tla_hint, dict) else "flat_lines"
            is_tla_flat = (tla_style == "flat_lines")
            if isinstance(source_tla, str):
                row["teaching_learning_activities"] = source_tla.strip()
            elif isinstance(source_tla, list):
                row["teaching_learning_activities"] = source_tla[:]
            elif isinstance(source_tla, dict):
                if is_tla_flat:
                    # Flat style: flatten all categories into a single list
                    all_items = []
                    for v in source_tla.values():
                        items = _csv_or_lines_to_list(v) if isinstance(v, (list, str)) else []
                        all_items.extend(items)
                    row["teaching_learning_activities"] = all_items if all_items else source_tla[:]
                else:
                    # Categorized style: preserve the categories
                    row["teaching_learning_activities"]["lecture"] = _csv_or_lines_to_list(source_tla.get("lecture", row["teaching_learning_activities"].get("lecture", [])))
                    row["teaching_learning_activities"]["practical_session"] = _csv_or_lines_to_list(source_tla.get("practical_session", row["teaching_learning_activities"].get("practical_session", [])))
                    row["teaching_learning_activities"]["other"] = _csv_or_lines_to_list(source_tla.get("other", row["teaching_learning_activities"].get("other", [])))
            row["assessment"] = _csv_or_lines_to_list(source.get("assessment", row.get("assessment", [])))
            source_res = source.get("learning_resources", "")
            # Check format hints for resources style
            res_hint = q_fields.get("learning_resources", {}) if isinstance(q_fields, dict) else {}
            res_style = res_hint.get("output_style", "flat_lines") if isinstance(res_hint, dict) else "flat_lines"
            is_res_flat = (res_style == "flat_lines")
            if isinstance(source_res, str):
                row["learning_resources"] = source_res.strip()
            elif isinstance(source_res, list):
                row["learning_resources"] = source_res[:]
            elif isinstance(source_res, dict):
                if is_res_flat:
                    # Flat style: flatten categories into a single list
                    all_items = []
                    for v in source_res.values():
                        items = _csv_or_lines_to_list(v) if isinstance(v, (list, str)) else []
                        all_items.extend(items)
                    row["learning_resources"] = all_items if all_items else source_res[:]
                else:
                    # Categorized style: preserve categories
                    row["learning_resources"]["clms"] = _csv_or_lines_to_list(source_res.get("clms", row["learning_resources"].get("clms", [])))
                    row["learning_resources"]["textbook"] = _csv_or_lines_to_list(source_res.get("textbook", row["learning_resources"].get("textbook", [])))
                    row["learning_resources"]["website"] = _csv_or_lines_to_list(source_res.get("website", row["learning_resources"].get("website", [])))
                    row["learning_resources"]["journal"] = _csv_or_lines_to_list(source_res.get("journal", row["learning_resources"].get("journal", [])))
                    row["learning_resources"]["other"] = _csv_or_lines_to_list(source_res.get("other", row["learning_resources"].get("other", [])))

            # ── Exam / review week resource injection ──
            # Must run BEFORE quality check so injected resources satisfy validation.
            if _is_assessment_function_row(row):
                label = row.get("time_frame_label", "")
                resources = row.get("learning_resources", {})
                if not isinstance(resources, dict):
                    resources = {}
                    row["learning_resources"] = resources
                clms = _csv_or_lines_to_list(resources.get("clms", []))
                other = _csv_or_lines_to_list(resources.get("other", []))
                if not (clms or other):
                    if _is_exam_week(label):
                        resources["clms"] = [
                            "Exam instructions and coverage outline",
                            "Grading rubric and performance criteria",
                        ]
                        resources["other"] = [
                            "Consultation and remediation schedule",
                        ]
                    else:
                        # Pre-exam/review week
                        resources["clms"] = [
                            "Review materials and synthesis guide",
                        ]
                        resources["other"] = [
                            "Readiness checklist and consultation schedule",
                        ]

            # Post-generation cap: truncate to template capacity
            _truncate_row_fields(row, q_fields)

            # Pre-emptive map repair before quality check
            if row.get("topics") and not row.get("mapped_clos") and clo_codes:
                row["mapped_clos"] = [clo_codes[0]]

            quality_issues = _weekly_row_quality_issues(row, metadata=metadata, previous_row=previous_teaching_row, format_hints=q_fields)
            if quality_issues:
                low_quality_issue_map[row["time_frame_label"]] = quality_issues
                low_quality_week_issues.append(f"{row['time_frame_label']}: {quality_issues[0]}")
            row["review_status"] = "fallback_draft" if source.get("_fallback_source") else "ai_draft"
            if not _is_orientation_week(row["time_frame_label"]) and not _is_assessment_function_row(row):
                previous_teaching_row = row
        return missing_week_labels, unrecoverable_missing_week_labels, low_quality_week_issues, low_quality_issue_map

    missing_week_labels, unrecoverable_missing_week_labels, low_quality_week_issues, low_quality_issue_map = _apply_generated_rows(generated_by_label)

    if low_quality_issue_map:
        flagged_labels = [label for label in week_labels if label in low_quality_issue_map]
        flagged_context_rows = [
            row for row in content["weekly_course_outline"]
            if row.get("time_frame_label") in flagged_labels
        ]
        # Build per-row resource keyword hints so the AI knows exactly which
        # terms from the resources must appear in topics/TLAs/assessments.
        resource_keyword_hints = {}
        for flagged_row in flagged_context_rows:
            flabel = flagged_row.get("time_frame_label", "")
            res_tokens = sorted(_keyword_tokens(" ".join(_weekly_row_resource_texts(flagged_row))))[:20]
            if res_tokens:
                resource_keyword_hints[flabel] = res_tokens

        # Determine ILO format instruction for recovery prompt
        r_spec = content.get("template_generation_spec", {})
        r_wo = r_spec.get("weekly_outline", {}) if isinstance(r_spec, dict) else {}
        r_hints = r_wo.get("format_hints", {}) if isinstance(r_wo, dict) else {}
        r_fields = r_hints.get("fields", {}) if isinstance(r_hints, dict) else {}
        ilo_hint = r_fields.get("intended_learning_outcomes", {}) if isinstance(r_fields, dict) else {}
        if isinstance(ilo_hint, dict) and ilo_hint.get("output_style") == "categorized":
            ilo_format_instruction = "Every row MUST have at least 2 items in cognitive, affective, AND psychomotor ILO groups."
        else:
            ilo_format_instruction = "Generate a lead-in sentence with bullet items for intended learning outcomes (flat format)."

        quality_recovery_prompt = f"""
Repair the flagged weekly rows that failed QA. Return JSON with key `weekly_course_outline`.
Return exactly {len(flagged_labels)} row objects for these labels: {json.dumps(flagged_labels)}

ISSUES TO FIX:
{json.dumps(low_quality_issue_map)}

RESOURCE GROUNDING REQUIREMENT — CRITICAL:
Each row's topics, teaching-learning activities, and assessments MUST use terms drawn from
the row's own learning_resources. The keyword anchors below are extracted directly from
each row's resources. Weave these terms into topics, lecture, practical_session, and assessment
fields so the narrative is visibly grounded in its sources.

RESOURCE KEYWORD ANCHORS PER ROW:
{json.dumps(resource_keyword_hints)}

CURRENT FLAGGED ROWS:
{json.dumps(flagged_context_rows)}

COURSE METADATA: {json.dumps(metadata)}
CLO ROWS: {json.dumps(clo_rows)}
TARGET SDGS: {json.dumps(target_sdgs)}
REFERENCE CANDIDATES: {json.dumps(reference_candidates)}

{_generation_shape_contract_text(content)}

Fix instructions: replace generic filler with course-specific wording grounded in the resource keywords above;
make rows with review/synthesis/consultation/exam-preparation labels match that function; keep full schema.
{ilo_format_instruction}
Do not include unflagged rows.
"""
        quality_recovery_data = _generate_json(quality_recovery_prompt, "copilot_beta_weekly")
        quality_recovery_rows = _extract_weekly_rows(quality_recovery_data)
        for row in quality_recovery_rows:
            if isinstance(row, dict) and row.get("time_frame_label") in flagged_labels:
                generated_by_label[row.get("time_frame_label")] = row
        missing_week_labels, unrecoverable_missing_week_labels, low_quality_week_issues, low_quality_issue_map = _apply_generated_rows(generated_by_label)
    if unrecoverable_missing_week_labels:
        raise ValueError(
            f"AI omitted weekly content for: {_summarize_week_labels(unrecoverable_missing_week_labels)}."
        )
    if missing_week_labels:
        _record_validation_warning(
            content,
            "Weekly regeneration kept the previous saved content for: "
            + _summarize_week_labels(missing_week_labels)
            + "."
        )
    if low_quality_issue_map:
        # Skip recovery API call — just warn. The exam-week injection and
        # quality validation improvements handle most issues at merge time.
        soft_warnings = {
            label: issues for label, issues in low_quality_issue_map.items()
        }
        if soft_warnings:
            for row_label, prog_issues in soft_warnings.items():
                for row in content["weekly_course_outline"]:
                    if row.get("time_frame_label") == row_label:
                        row["review_status"] = "needs_resource_review"
            advisory_summary = "; ".join(
                f"{label}: {issues[0]}" for label, issues in soft_warnings.items()
            )
            _record_validation_warning(
                content,
                "Some weekly rows have quality suggestions — use single-week "
                "regeneration to refine: " + advisory_summary + ".",
            )
    # ── ILO auto-heal: Ensure every week has cognitive/affective/psychomotor arrays ──
    for row in content["weekly_course_outline"]:
        ilo = row.get("intended_learning_outcomes", {})
        if isinstance(ilo, dict):
            if not _csv_or_lines_to_list(ilo.get("cognitive", [])):
                ilo["cognitive"] = ["Define key concepts", "Explain fundamental principles"]
            if not _csv_or_lines_to_list(ilo.get("affective", [])):
                ilo["affective"] = ["Appreciate the relevance of the topic", "Value the application of concepts"]
            if not _csv_or_lines_to_list(ilo.get("psychomotor", [])):
                ilo["psychomotor"] = ["Demonstrate basic skills", "Perform related tasks"]
            if not ilo.get("lead_in"):
                ilo["lead_in"] = "At the end of the week, students should have the ability to:"
    _advance_stage(content, "weekly_generated")
    return content


def generate_single_week_row(content, week_index):
    """Regenerate a single weekly row in one AI call instead of regenerating all 14."""
    content = ensure_beta_shape(normalize_beta_content(content))
    metadata = content["metadata"]
    alignment_context = content.get("alignment_context") if isinstance(content.get("alignment_context"), dict) else {}
    target_sdgs = _csv_or_lines_to_list(metadata.get("target_sdgs_display", []))
    sdg_context = alignment_context.get("sdg_context", SDG_CONTEXT)
    reference_candidates = _course_reference_candidates(metadata)

    row_idx = week_index - 1
    target_row = content["weekly_course_outline"][row_idx]
    target_label = target_row["time_frame_label"]

    clo_rows = [
        {"clo_code": r["clo_code"], "domain": r["domain"], "clo_statement": r["clo_statement"]}
        for r in content["clo_alignment_table"]
    ]

    neighbor_rows = []
    for i in [row_idx - 1, row_idx + 1]:
        if 0 <= i < len(content["weekly_course_outline"]):
            nr = content["weekly_course_outline"][i]
            neighbor_rows.append({
                "time_frame_label": nr["time_frame_label"],
                "topics": nr.get("topics", [])[:3],
                "mapped_clos": nr.get("mapped_clos", []),
            })

    dynamic_single = _get_dynamic_prompt(content, "weekly")
    default_prompt = dynamic_single
    prompt = f"""
{default_prompt}

You are regenerating ONLY the week row for `{target_label}`.
Return JSON with one top-level key: `weekly_course_outline` containing exactly 1 row object.

COURSE METADATA:
{json.dumps(metadata)}

APPROVED CLO ROWS:
{json.dumps(clo_rows)}

TARGET SDGS FOR COURSE:
{json.dumps(target_sdgs)}

SDG CONTEXT GUIDANCE:
{json.dumps(sdg_context)}

COURSE REFERENCE CANDIDATES:
{json.dumps(reference_candidates)}

NEIGHBORING WEEKS (for progression context only — do NOT regenerate these):
{json.dumps(neighbor_rows)}

Generate exactly 1 row with `time_frame_label` = `{target_label}`.
{_generation_shape_contract_text(content)}
{_template_seed_context_text(content)}
"""
    data = _generate_json(prompt, "copilot_beta_single_week")
    generated_rows = _normalize_ai_row_list(
        data, ["weekly_course_outline", "weekly_rows", "course_outline", "rows"],
    )
    source = None
    for row in (generated_rows or []):
        if isinstance(row, dict) and row.get("time_frame_label") == target_label:
            source = row
            break
    if not source and generated_rows:
        source = generated_rows[0] if isinstance(generated_rows[0], dict) else None

    if not source:
        raise ValueError(f"AI did not return a week row for {target_label}.")

    target_row["mapped_clos"] = _normalize_mapped_clos(source.get("mapped_clos", target_row.get("mapped_clos", [])))
    source_ilo = source.get("intended_learning_outcomes", "")
    if isinstance(source_ilo, str):
        target_row["intended_learning_outcomes"] = source_ilo.strip()
    elif isinstance(source_ilo, list):
        target_row["intended_learning_outcomes"] = source_ilo[:]
    elif isinstance(source_ilo, dict):
        target_row["intended_learning_outcomes"]["lead_in"] = str(
            source_ilo.get("lead_in") or target_row["intended_learning_outcomes"].get("lead_in")
            or "At the end of the week, students should have the ability to:"
        ).strip()
        target_row["intended_learning_outcomes"]["cognitive"] = _csv_or_lines_to_list(source_ilo.get("cognitive", target_row["intended_learning_outcomes"].get("cognitive", [])))
        target_row["intended_learning_outcomes"]["affective"] = _csv_or_lines_to_list(source_ilo.get("affective", target_row["intended_learning_outcomes"].get("affective", [])))
        target_row["intended_learning_outcomes"]["psychomotor"] = _csv_or_lines_to_list(source_ilo.get("psychomotor", target_row["intended_learning_outcomes"].get("psychomotor", [])))
    target_row["topics"] = _csv_or_lines_to_list(source.get("topics", target_row.get("topics", [])))
    source_tla = source.get("teaching_learning_activities", "")
    if isinstance(source_tla, str):
        target_row["teaching_learning_activities"] = source_tla.strip()
    elif isinstance(source_tla, list):
        target_row["teaching_learning_activities"] = source_tla[:]
    elif isinstance(source_tla, dict):
        target_row["teaching_learning_activities"]["lecture"] = _csv_or_lines_to_list(source_tla.get("lecture", target_row["teaching_learning_activities"].get("lecture", [])))
        target_row["teaching_learning_activities"]["practical_session"] = _csv_or_lines_to_list(source_tla.get("practical_session", target_row["teaching_learning_activities"].get("practical_session", [])))
        target_row["teaching_learning_activities"]["other"] = _csv_or_lines_to_list(source_tla.get("other", target_row["teaching_learning_activities"].get("other", [])))
    target_row["assessment"] = _csv_or_lines_to_list(source.get("assessment", target_row.get("assessment", [])))
    source_res = source.get("learning_resources", "")
    if isinstance(source_res, str):
        target_row["learning_resources"] = source_res.strip()
    elif isinstance(source_res, list):
        target_row["learning_resources"] = source_res[:]
    elif isinstance(source_res, dict):
        target_row["learning_resources"]["clms"] = _csv_or_lines_to_list(source_res.get("clms", target_row["learning_resources"].get("clms", [])))
        target_row["learning_resources"]["textbook"] = _csv_or_lines_to_list(source_res.get("textbook", target_row["learning_resources"].get("textbook", [])))
        target_row["learning_resources"]["website"] = _csv_or_lines_to_list(source_res.get("website", target_row["learning_resources"].get("website", [])))
        target_row["learning_resources"]["journal"] = _csv_or_lines_to_list(source_res.get("journal", target_row["learning_resources"].get("journal", [])))
        target_row["learning_resources"]["other"] = _csv_or_lines_to_list(source_res.get("other", target_row["learning_resources"].get("other", [])))
    target_row["review_status"] = "ai_draft"

    clo_codes = [r.get("clo_code") for r in content.get("clo_alignment_table", []) if r.get("clo_code")]
    if target_row.get("topics") and not target_row.get("mapped_clos") and clo_codes:
        target_row["mapped_clos"] = [clo_codes[0]]

    return content


def enforce_beta_weekly_quality(content, include_week1=False):
    content = ensure_beta_shape(normalize_beta_content(content))
    _remove_validation_warnings_matching(
        content,
        [
            "saved week block(s) were upgraded to template-safe detail",
            "Weekly generation upgraded",
        ],
    )
    clo_codes = [row.get("clo_code") for row in content.get("clo_alignment_table", []) if row.get("clo_code")]
    for row in content["weekly_course_outline"]:
        if row.get("topics") and not row.get("mapped_clos") and clo_codes:
            row["mapped_clos"] = [clo_codes[0]]
    return content, False


def _extend_reference_replacement_lists(bucket, websites, textbooks, journals, other):
    if not isinstance(bucket, dict):
        other.extend(_csv_or_lines_to_list(bucket))
        return
    for raw_key, raw_value in bucket.items():
        key = re.sub(r"[^a-z0-9]+", "_", str(raw_key or "").lower()).strip("_")
        values = _csv_or_lines_to_list(raw_value)
        if not values:
            continue
        if key in {"website", "websites", "web", "online", "online_resources", "url", "urls", "link", "links"}:
            websites.extend(values)
        elif key in {"textbook", "textbooks", "book", "books", "text"}:
            textbooks.extend(values)
        elif key in {"journal", "journals", "article", "articles", "research", "research_articles"}:
            journals.extend(values)
        elif key not in {"clms", "lms", "module", "modules"}:
            other.extend(values)


def build_beta_docx_replacements(content, user_profile=None, generated_at=None):
    content = ensure_beta_shape(normalize_beta_content(content))
    spec = _get_generation_spec(content)
    metadata = content.get("metadata", {})
    signatories = content.get("signatories", {})
    department_signatories = get_department_signatory_settings(department_name=metadata.get("department"))
    generated_at = generated_at or datetime.now()
    today = generated_at.strftime("%B %d, %Y")

    replacements = {}
    replacements.update({key: str(value or "") for key, value in metadata.items()})
    replacements.update({key: str(value or "") for key, value in signatories.items()})

    # Metadata aliases — insertion templates may use various placeholder names for the same field.
    replacements.setdefault("hours_per_week", str(metadata.get("contact_hours_per_week") or metadata.get("hours_per_week") or ""))
    replacements.setdefault("class_hours", str(metadata.get("contact_hours_per_week") or metadata.get("class_hours") or ""))
    replacements.setdefault("course_number", str(metadata.get("course_code") or metadata.get("course_number") or ""))
    replacements.setdefault("descriptive_title", str(metadata.get("course_title") or metadata.get("descriptive_title") or ""))
    replacements.setdefault("units_display", str(metadata.get("unit") or metadata.get("units") or metadata.get("units_display") or ""))
    replacements.setdefault("credit_display", str(metadata.get("unit") or metadata.get("credit_display") or ""))
    replacements.setdefault("pre_requisite", str(metadata.get("pre_requisites") or metadata.get("pre_requisite") or "None"))
    replacements.setdefault("co_requisite", str(metadata.get("co_requisites") or metadata.get("co_requisite") or "None"))

    prepared_by_name = signatories.get("prepared_by_name") or (
        f"{(user_profile or {}).get('first_name', '')} {(user_profile or {}).get('last_name', '')}".strip()
    ) or "Instructor"
    prepared_by_position = signatories.get("prepared_by_position") or (user_profile or {}).get("title") or "Instructor"
    reviewed_by_name = signatories.get("reviewed_by_name") or department_signatories.get("program_coordinator_name") or ""
    reviewed_by_position = signatories.get("reviewed_by_position") or department_signatories.get("program_coordinator_title") or "Program Coordinator"
    endorsed_by_name = signatories.get("endorsed_by_name") or department_signatories.get("dean_name") or ""
    endorsed_by_position = signatories.get("endorsed_by_position") or department_signatories.get("dean_title") or "Dean"
    approved_by_name = signatories.get("approved_by_name") or department_signatories.get("vice_president_name") or ""
    approved_by_position = signatories.get("approved_by_position") or department_signatories.get("vice_president_title") or "Vice President"

    replacements.update(
        {
            "prepared_by_name": prepared_by_name,
            "prepared_by_position": prepared_by_position,
            "reviewed_by_name": reviewed_by_name,
            "reviewed_by_position": reviewed_by_position,
            "endorsed_by_name": endorsed_by_name,
            "endorsed_by_position": endorsed_by_position,
            "approved_by_name": approved_by_name,
            "approved_by_position": approved_by_position,
            "date_submitted": today,
            "date_reviewed": today,
            "last_revised_by_name": prepared_by_name,
            "last_revised_by_position": prepared_by_position,
            "last_revised_date": today,
            "last_updated_by_name": prepared_by_name,
            "last_updated_by_position": prepared_by_position,
            "last_updated_date": today,
            "final_reviewed_by_name": reviewed_by_name,
            "final_reviewed_by_position": reviewed_by_position,
            "final_reviewed_date": today,
            "final_endorsed_by_name": endorsed_by_name,
            "final_endorsed_by_position": endorsed_by_position,
            "final_endorsed_date": today,
            "final_approved_by_name": approved_by_name,
            "final_approved_by_position": approved_by_position,
            "final_approved_date": today,
            "prelim_exam_label": "Preliminary Examination",
            "midterm_exam_label": "Midterm Examination",
            "final_exam_label": "Final Examination",
            "course_requirements_block": (
                "- Complete all required lecture, laboratory, and guided learning activities\n"
                "- Submit the expected weekly outputs, assessments, and applied tasks\n"
                "- Participate in quizzes, consultations, and major examinations\n"
                "- Demonstrate course competencies through the final integrative outputs"
            ),
        }
    )

    consultation_hours = (user_profile or {}).get("consultation_hours")
    if isinstance(consultation_hours, dict):
        day_order = ["monday", "tuesday", "wednesday", "thursday", "friday"]
        slots = []
        for day_name in day_order:
            day_data = consultation_hours.get(day_name)
            if isinstance(day_data, dict) and (day_data.get("time") or day_data.get("room")):
                slots.append({
                    "days": day_name.capitalize(),
                    "time": day_data.get("time") or "",
                    "room": day_data.get("room") or "",
                })
        for index in range(3):
            slot = slots[index] if index < len(slots) and isinstance(slots[index], dict) else {}
            slot_no = index + 1
            replacements[f"consultation_slot_{slot_no}_days"] = str(slot.get("days") or "")
            replacements[f"consultation_slot_{slot_no}_time"] = str(slot.get("time") or "")
            replacements[f"consultation_slot_{slot_no}_room"] = str(slot.get("room") or "")

    # PLO alignment group titles — fills {{plo_group_N_title}} placeholders.
    for gi, group in enumerate(content.get("clo_alignment_groups", []), start=1):
        replacements[f"plo_group_{gi}_title"] = str(group.get("label") or group.get("program_name") or f"Program {gi}")

    alignment_style = content.get("alignment_style") or spec.get("alignment_style") or "clo_based"
    for alignment_id, matrix in (content.get("program_institutional_alignments") or {}).items():
        if not isinstance(matrix, dict):
            continue
        for row_key, row_values in matrix.items():
            if not isinstance(row_values, dict):
                continue
            for col_key, value in row_values.items():
                replacements[f"{alignment_id}_{row_key}_{col_key}"] = _to_checkmark_value(value)

    checkmark_groups = content.get("clo_alignment_groups") if isinstance(content.get("clo_alignment_groups"), list) else []
    if not checkmark_groups and isinstance(content.get("clo_alignment_table"), list):
        checkmark_groups = [{
            "clo_alignment_table": content.get("clo_alignment_table", []),
            "checkmark_po_codes": _get_checkmark_po_codes(content),
        }]
    for group_index, group in enumerate(checkmark_groups, start=1):
        if not isinstance(group, dict):
            continue
        group_rows = group.get("clo_alignment_table") if isinstance(group.get("clo_alignment_table"), list) else []
        group_po_codes = group.get("checkmark_po_codes") if isinstance(group.get("checkmark_po_codes"), list) else _get_checkmark_po_codes(content)
        for row_index, row in enumerate(group_rows, start=1):
            if not isinstance(row, dict):
                continue
            checkmark_data = row.get("checkmark_alignments") if isinstance(row.get("checkmark_alignments"), dict) else {}
            for col_index, po_code in enumerate(group_po_codes, start=1):
                replacements[_compact_clo_checkmark_key(group_index, row_index, col_index)] = "✓" if _to_bool(checkmark_data.get(po_code, False)) else ""

    for index, row in enumerate(content.get("clo_alignment_table", []), start=1):
        clo_code = row.get("clo_code", f"CLO {index}")
        clo_statement = (row.get("clo_statement") or "").strip()
        # In checkmark templates, the code placeholder doubles as the statement cell.
        # Combine both so the full CLO text appears in the DOCX.
        replacements[f"clo_{index}_code"] = f"{clo_code} - {clo_statement}" if clo_statement else clo_code
        replacements[f"clo_{index}_statement"] = row.get("clo_statement", "")
        if alignment_style == "checkmark":
            checkmark_data = row.get("checkmark_alignments") if isinstance(row.get("checkmark_alignments"), dict) else {}
            for po_code, is_checked in checkmark_data.items():
                safe_key = _checkmark_po_key(po_code)
                replacements[f"clo_{index}_{safe_key}"] = "✓" if is_checked else ""
        else:
            replacements[f"clo_{index}_aligned_plos"] = _bullet_lines(row.get("aligned_plos", []))
            replacements[f"clo_{index}_graduate_attributes"] = _bullet_lines(row.get("graduate_attributes", []))
            replacements[f"clo_{index}_core_values"] = _bullet_lines(row.get("core_values", []))
            replacements[f"clo_{index}_pqf_alignment"] = _bullet_lines(row.get("pqf_level_6_alignment", []))
            replacements[f"clo_{index}_aqrf_alignment"] = _bullet_lines(row.get("aqrf_level_6_alignment", []))
            replacements[f"clo_{index}_relevant_sdgs"] = _bullet_lines(row.get("relevant_sdgs", []))

    all_websites = []
    all_textbooks = []
    all_journals = []
    all_other_references = []
    _extend_reference_replacement_lists(
        content.get("references", {}),
        all_websites,
        all_textbooks,
        all_journals,
        all_other_references,
    )

    for row in content.get("weekly_course_outline", []):
        prefix = _week_prefix(row.get("time_frame_label", "Week 1"))
        replacements[f"{prefix}_time_frame"] = row.get("time_frame_label", "")
        replacements[f"{prefix}_ilo"] = _format_ilo_block(row)
        replacements[f"{prefix}_topics"] = _format_topics_for_docx(row.get("topics", []))
        replacements[f"{prefix}_tla"] = _format_tla_block(row)
        replacements[f"{prefix}_assessment"] = _bullet_lines(row.get("assessment", []))
        replacements[f"{prefix}_learning_resources"] = _format_resources_block(row)

        _extend_reference_replacement_lists(
            row.get("learning_resources", {}),
            all_websites,
            all_textbooks,
            all_journals,
            all_other_references,
        )

    # Synthesize compound-week keys from the content's weekly outline.
    # Insertion templates produced from DOCX profiling can contain compound week
    # placeholders (e.g. {{week_4_5_topics}}) while the AI Copilot may generate
    # individual week rows for the same span.
    #
    # We map every week number to the prefix that provides its content (individual
    # or compound), then for every consecutive pair of week numbers create a
    # compound replacement key.  This handles cases where the template groups
    # week rows differently from the AI's default schedule (e.g. the template has
    # {{week_7_8_*}} but the AI produces separate "Week 7" + "Week 8 & 9").
    _WEEK_SUFFIXES = ["time_frame", "ilo", "topics", "tla", "assessment", "learning_resources"]
    week_number_to_prefix = {}
    for row in content.get("weekly_course_outline", []):
        label = row.get("time_frame_label", "")
        prefix = _week_prefix(label) if label else ""
        if prefix and prefix.startswith("week_"):
            parts = prefix.split("_", 1)[1] if "_" in prefix else prefix.split("_", 1)[0]
            numbers = [int(n) for n in re.findall(r"\d+", parts)]
            if numbers:
                for n in range(numbers[0], numbers[-1] + 1):
                    if n not in week_number_to_prefix:
                        week_number_to_prefix[n] = prefix
    all_week_numbers = sorted(week_number_to_prefix.keys())
    for i in range(len(all_week_numbers) - 1):
        a = all_week_numbers[i]
        b = all_week_numbers[i + 1]
        if b != a + 1:
            continue
        combined = f"{a}_{b}"
        a_prefix = week_number_to_prefix[a]
        b_prefix = week_number_to_prefix[b]
        for suffix in _WEEK_SUFFIXES:
            combined_key = f"week_{combined}_{suffix}"
            if combined_key not in replacements:
                a_val = replacements.get(f"{a_prefix}_{suffix}", "")
                b_val = replacements.get(f"{b_prefix}_{suffix}", "")
                if a_val or b_val:
                    replacements[combined_key] = "\n".join(v for v in [a_val, b_val] if v)

    # Back-fill individual week keys for weeks that exist only inside a compound
    # row (e.g. the template has {{week_9_time_frame}} but the AI produced a
    # single "Week 8 & 9" row).  Copy the compound row's content to the
    # individual placeholder so it resolves instead of staying literal.
    for week_number, prefix in week_number_to_prefix.items():
        if prefix == f"week_{week_number}":
            continue  # already has its own individual row
        for suffix in _WEEK_SUFFIXES:
            individual_key = f"week_{week_number}_{suffix}"
            if individual_key not in replacements:
                compound_val = replacements.get(f"{prefix}_{suffix}", "")
                if compound_val:
                    replacements[individual_key] = compound_val

    replacements["references_website_block"] = _bullet_lines(_unique_keep_order(all_websites)[:10])
    replacements["references_textbook_block"] = _bullet_lines(_unique_keep_order(all_textbooks)[:10])
    replacements["references_journal_block"] = _bullet_lines(_unique_keep_order(all_journals)[:10])
    references_all = []
    references_all.extend(_unique_keep_order(all_textbooks)[:10])
    references_all.extend(_unique_keep_order(all_websites)[:10])
    references_all.extend(_unique_keep_order(all_journals)[:10])
    references_all.extend(_unique_keep_order(all_other_references)[:10])
    replacements["references_all_block"] = _bullet_lines(_unique_keep_order(references_all)[:24])

    # Instructor name alias for consultation/signatory tables.
    replacements.setdefault("instructor_name", prepared_by_name)
    replacements.setdefault("instructor_position", prepared_by_position)

    return replacements


def get_beta_final_review_signature(content):
    content = ensure_beta_shape(normalize_beta_content(content))
    payload = {
        "metadata": content.get("metadata", {}),
        "clo_alignment_table": content.get("clo_alignment_table", []),
        "clo_alignment_groups": content.get("clo_alignment_groups", []),
        "weekly_course_outline": content.get("weekly_course_outline", []),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def get_cached_beta_final_review(content):
    content = ensure_beta_shape(normalize_beta_content(content))
    validation = content.get("validation") if isinstance(content.get("validation"), dict) else {}
    cached_signature = str(validation.get("final_review_signature") or "").strip()
    if not cached_signature or cached_signature != get_beta_final_review_signature(content):
        return None
    if "final_review_approved" not in validation:
        return None
    return {
        "approved": bool(validation.get("final_review_approved")),
        "issues": [str(item).strip() for item in _csv_or_lines_to_list(validation.get("final_review_issues", [])) if str(item).strip()],
        "warnings": [str(item).strip() for item in _csv_or_lines_to_list(validation.get("final_review_warnings", [])) if str(item).strip()],
        "summary": str(validation.get("final_review_summary") or "").strip(),
    }


def run_beta_final_review(content):
    content = ensure_beta_shape(normalize_beta_content(content))
    metadata = content["metadata"]
    context = get_static_alignment_context()
    prompt = f"""
{BETA_FINAL_REVIEW_PROMPT}

COURSE METADATA:
{json.dumps(metadata)}

ALIGNMENT CONTEXT:
{json.dumps(context)}

CLO ALIGNMENT TABLE:
{json.dumps(content.get("clo_alignment_table", []))}

CLO ALIGNMENT GROUPS:
{json.dumps(content.get("clo_alignment_groups", []))}

WEEKLY COURSE OUTLINE:
{json.dumps(content.get("weekly_course_outline", []))}

{_generation_shape_contract_text(content)}
{_template_seed_context_text(content)}
"""
    data = None
    for attempt in range(2):
        try:
            data = _generate_json(prompt, "copilot_beta_final_review")
            break
        except ValueError as exc:
            if attempt == 0:
                current_app.logger.warning("Beta final review JSON parse failed, retrying once: %s", exc)
                continue
            current_app.logger.error("Beta final review failed after retry: %s", exc)
            return {
                "approved": False,
                "issues": ["AI final review returned invalid JSON. Please retry the final review."],
                "warnings": [],
                "summary": "AI final review failed to return valid JSON.",
            }
    if not isinstance(data, dict):
        return {
            "approved": False,
            "issues": ["AI final review did not return a valid response."],
            "warnings": [],
            "summary": "",
        }

    approved = bool(data.get("approved"))
    issues = [str(item).strip() for item in _csv_or_lines_to_list(data.get("issues", [])) if str(item).strip()]
    warnings = [str(item).strip() for item in _csv_or_lines_to_list(data.get("warnings", [])) if str(item).strip()]
    summary = str(data.get("summary") or "").strip()

    if not summary:
        summary = "AI final review completed." if approved else "AI final review found issues that should be fixed first."
    if not approved and not issues:
        issues = ["AI final review did not approve the draft, but did not provide specific issues."]

    return {
        "approved": approved and not issues,
        "issues": issues,
        "warnings": warnings,
        "summary": summary,
    }


def run_beta_final_fix(content, issues=None, warnings=None):
    content = ensure_beta_shape(normalize_beta_content(content))
    metadata = content["metadata"]
    context = get_static_alignment_context()
    issues = [str(item).strip() for item in (issues or []) if str(item).strip()]
    warnings = [str(item).strip() for item in (warnings or []) if str(item).strip()]

    prompt = f"""
{BETA_FINAL_FIX_PROMPT}

COURSE METADATA:
{json.dumps(metadata)}

ALIGNMENT CONTEXT:
{json.dumps(context)}

    CURRENT CLO ALIGNMENT TABLE:
    {json.dumps(content.get("clo_alignment_table", []))}

    CURRENT CLO ALIGNMENT GROUPS:
    {json.dumps(content.get("clo_alignment_groups", []))}

    CURRENT WEEKLY COURSE OUTLINE:
    {json.dumps(content.get("weekly_course_outline", []))}

    FINAL REVIEW ISSUES TO FIX:
    {json.dumps(issues)}

    FINAL REVIEW WARNINGS TO CONSIDER:
    {json.dumps(warnings)}

STRICT FIX REQUIREMENTS:
- `mapped_clos` must be a proper array of separate CLO codes, never concatenated into one string
- weekly narrative must not reference CLO codes that are absent from that row's `mapped_clos`
- do not leave generic psychomotor filler such as "Perform a guided task aligned with CLO X"
- fix repeated topics when the mapped CLOs or weekly purpose are different
- preserve the exact CLO row count, CLO codes, CLO group count, group row counts, weekly row count, and weekly labels from the template profile shape below

{_generation_shape_contract_text(content)}
{_template_seed_context_text(content)}
"""
    data = _generate_json(prompt, "copilot_beta_final_fix")
    if not isinstance(data, dict):
        fixed = ensure_beta_shape(normalize_beta_content(content))
        _record_validation_warning(fixed, "AI fix did not return a valid response, so the current draft was preserved.")
        return fixed

    fixed = ensure_beta_shape(normalize_beta_content(content))

    if isinstance(data.get("metadata"), dict):
        for key, value in data["metadata"].items():
            if key in fixed["metadata"]:
                fixed["metadata"][key] = str(value or "").strip()

    generated_clo_rows = _normalize_ai_row_list(
        data,
        ["clo_alignment_table", "clo_rows", "clos", "course_learning_outcomes", "rows"],
    )
    if generated_clo_rows:
        generated_by_code = {
            str(row.get("clo_code") or row.get("code") or row.get("outcome_code") or "").strip(): row
            for row in generated_clo_rows
            if isinstance(row, dict)
            and str(row.get("clo_code") or row.get("code") or row.get("outcome_code") or "").strip()
        }
        fallback_rows = {
            row.get("clo_code"): row
            for row in _build_fallback_clo_rows(fixed["metadata"], _clo_blueprints_from_spec(_get_generation_spec(fixed)))
            if row.get("clo_code")
        }
        for idx, row in enumerate(fixed["clo_alignment_table"]):
            default_code = row.get("clo_code") or f"CLO {idx + 1}"
            default_domain = row.get("domain") or "cognitive"
            source = generated_by_code.get(default_code) or (generated_clo_rows[idx] if idx < len(generated_clo_rows) and isinstance(generated_clo_rows[idx], dict) else {})
            source = source if isinstance(source, dict) else {}
            fallback = fallback_rows.get(default_code, {})
            row["clo_code"] = default_code
            row["domain"] = _normalize_clo_domain(source.get("domain") or fallback.get("domain"), default_domain)
            row["clo_statement"] = str(
                source.get("clo_statement")
                or source.get("statement")
                or source.get("outcome")
                or row.get("clo_statement")
                or fallback.get("clo_statement")
                or ""
            ).strip()
            row["aligned_plos"] = _csv_or_lines_to_list(source.get("aligned_plos", row.get("aligned_plos", [])))
            row["graduate_attributes"] = _csv_or_lines_to_list(source.get("graduate_attributes", row.get("graduate_attributes", [])))
            row["core_values"] = _csv_or_lines_to_list(source.get("core_values", row.get("core_values", [])))
            row["pqf_level_6_alignment"] = _csv_or_lines_to_list(source.get("pqf_level_6_alignment", row.get("pqf_level_6_alignment", [])))
            row["aqrf_level_6_alignment"] = _csv_or_lines_to_list(source.get("aqrf_level_6_alignment", row.get("aqrf_level_6_alignment", [])))
            row["relevant_sdgs"] = _csv_or_lines_to_list(source.get("relevant_sdgs", row.get("relevant_sdgs", [])))
            if source:
                row["review_status"] = "ai_fixed"
        _sync_primary_clo_rows_to_groups(fixed, include_alignment=False)

    generated_weekly_rows = _normalize_ai_row_list(
        data,
        ["weekly_course_outline", "weekly_rows", "course_outline", "rows"],
    )
    if generated_weekly_rows:
        generated_by_label = {
            str(row.get("time_frame_label") or ""): row
            for row in generated_weekly_rows
            if isinstance(row, dict) and row.get("time_frame_label")
        }
        for row in fixed["weekly_course_outline"]:
            source = generated_by_label.get(row["time_frame_label"])
            if not source:
                continue
            row["mapped_clos"] = _csv_or_lines_to_list(source.get("mapped_clos", row.get("mapped_clos", [])))
            if isinstance(row.get("intended_learning_outcomes"), str):
                row["intended_learning_outcomes"] = {}
            ilo = source.get("intended_learning_outcomes", {}) if isinstance(source.get("intended_learning_outcomes"), dict) else {}
            row["intended_learning_outcomes"]["lead_in"] = str(
                ilo.get("lead_in") or row["intended_learning_outcomes"].get("lead_in") or "At the end of the week, students should have the ability to:"
            ).strip()
            row["intended_learning_outcomes"]["cognitive"] = _csv_or_lines_to_list(ilo.get("cognitive", row["intended_learning_outcomes"].get("cognitive", [])))
            row["intended_learning_outcomes"]["affective"] = _csv_or_lines_to_list(ilo.get("affective", row["intended_learning_outcomes"].get("affective", [])))
            row["intended_learning_outcomes"]["psychomotor"] = _csv_or_lines_to_list(ilo.get("psychomotor", row["intended_learning_outcomes"].get("psychomotor", [])))
            row["topics"] = _csv_or_lines_to_list(source.get("topics", row.get("topics", [])))
            tla = source.get("teaching_learning_activities", {}) if isinstance(source.get("teaching_learning_activities"), dict) else {}
            row["teaching_learning_activities"]["lecture"] = _csv_or_lines_to_list(tla.get("lecture", row["teaching_learning_activities"].get("lecture", [])))
            row["teaching_learning_activities"]["practical_session"] = _csv_or_lines_to_list(tla.get("practical_session", row["teaching_learning_activities"].get("practical_session", [])))
            row["teaching_learning_activities"]["other"] = _csv_or_lines_to_list(tla.get("other", row["teaching_learning_activities"].get("other", [])))
            row["assessment"] = _csv_or_lines_to_list(source.get("assessment", row.get("assessment", [])))
            resources = source.get("learning_resources", {}) if isinstance(source.get("learning_resources"), dict) else {}
            row["learning_resources"]["clms"] = _csv_or_lines_to_list(resources.get("clms", row["learning_resources"].get("clms", [])))
            row["learning_resources"]["textbook"] = _csv_or_lines_to_list(resources.get("textbook", row["learning_resources"].get("textbook", [])))
            row["learning_resources"]["website"] = _csv_or_lines_to_list(resources.get("website", row["learning_resources"].get("website", [])))
            row["learning_resources"]["journal"] = _csv_or_lines_to_list(resources.get("journal", row["learning_resources"].get("journal", [])))
            row["learning_resources"]["other"] = _csv_or_lines_to_list(resources.get("other", row["learning_resources"].get("other", [])))
            row["review_status"] = "ai_fixed"

    fixed.setdefault("validation", {})
    fixed["validation"]["final_review_summary"] = "AI repair pass applied suggested corrections from the blocked final review."
    fix_notes = [str(item).strip() for item in _csv_or_lines_to_list(data.get("notes", [])) if str(item).strip()]
    if fix_notes:
        fixed["validation"]["final_fix_notes"] = fix_notes
    return fixed


def beta_display_texts(content):
    content = ensure_beta_shape(normalize_beta_content(content))
    alignment_style = _get_alignment_style(content)
    checkmark_po_codes = _get_checkmark_po_codes(content)
    display = {
        "clo_rows": [],
        "clo_groups": [],
        "weekly_rows": [],
        "program_institutional_alignments": [],
        "alignment_style": alignment_style,
        "checkmark_po_codes": checkmark_po_codes,
    }
    for row_index, row in enumerate(content["clo_alignment_table"], start=1):
        row_data = {**row}
        if alignment_style == "checkmark":
            checks = row.get("checkmark_alignments") if isinstance(row.get("checkmark_alignments"), dict) else {}
            row_data["checkmark_alignments"] = checks
            row_data["checkmark_present_field"] = _checkmark_present_form_key(row_index)
            row_data["checkmark_inputs"] = [
                {
                    "code": po_code,
                    "field": _checkmark_form_key(row_index, po_code),
                    "checked": _to_bool(checks.get(po_code, False)),
                }
                for po_code in checkmark_po_codes
            ]
        else:
            row_data["aligned_plos_text"] = _list_to_text(row.get("aligned_plos", []))
            row_data["graduate_attributes_text"] = _list_to_text(row.get("graduate_attributes", []))
            row_data["core_values_text"] = _list_to_text(row.get("core_values", []))
            row_data["pqf_alignment_text"] = _list_to_text(row.get("pqf_level_6_alignment", []))
            row_data["aqrf_alignment_text"] = _list_to_text(row.get("aqrf_level_6_alignment", []))
            row_data["relevant_sdgs_text"] = _list_to_text(row.get("relevant_sdgs", []))
        display["clo_rows"].append(row_data)
    for group in content.get("clo_alignment_groups", []) or []:
        group_rows = []
        for row_index, row in enumerate(group.get("clo_alignment_table", []) or [], start=1):
            row_data = {**row}
            if alignment_style == "checkmark":
                checks = row.get("checkmark_alignments") if isinstance(row.get("checkmark_alignments"), dict) else {}
                row_data["checkmark_alignments"] = checks
                row_data["checkmark_present_field"] = _checkmark_present_form_key(row_index)
                group_po_codes = group.get("checkmark_po_codes") if isinstance(group.get("checkmark_po_codes"), list) else checkmark_po_codes
                row_data["checkmark_inputs"] = [
                    {
                        "code": po_code,
                        "field": _checkmark_form_key(row_index, po_code),
                        "checked": _to_bool(checks.get(po_code, False)),
                    }
                    for po_code in group_po_codes
                ]
            else:
                row_data["aligned_plos_text"] = _list_to_text(row.get("aligned_plos", []))
                row_data["graduate_attributes_text"] = _list_to_text(row.get("graduate_attributes", []))
                row_data["core_values_text"] = _list_to_text(row.get("core_values", []))
                row_data["pqf_alignment_text"] = _list_to_text(row.get("pqf_level_6_alignment", []))
                row_data["aqrf_alignment_text"] = _list_to_text(row.get("aqrf_level_6_alignment", []))
                row_data["relevant_sdgs_text"] = _list_to_text(row.get("relevant_sdgs", []))
            group_rows.append(row_data)
        display["clo_groups"].append({
            "group_id": group.get("group_id"),
            "label": group.get("label") or "CLO Alignment",
            "program_scope": group.get("program_scope") or {},
            "rows": group_rows,
        })
    for alignment in _program_institutional_specs(content):
        alignment_id = alignment.get("id") or "program_institutional_alignment"
        matrix = content.get("program_institutional_alignments") if isinstance(content.get("program_institutional_alignments"), dict) else {}
        matrix = matrix.get(alignment_id) if isinstance(matrix.get(alignment_id), dict) else {}
        row_keys = [
            normalize_alignment_row_label(item)
            for item in (alignment.get("row_labels_normalized") or [])
            if normalize_alignment_row_label(item)
        ]
        col_keys = [
            normalize_alignment_column_label(item)
            for item in (alignment.get("column_labels_normalized") or [])
            if normalize_alignment_column_label(item)
        ]
        row_labels = alignment.get("row_labels_original") or []
        col_labels = alignment.get("column_labels_original") or []

        columns = []
        for idx, col_key in enumerate(col_keys):
            label = col_labels[idx] if idx < len(col_labels) and col_labels[idx] else col_key
            columns.append({"key": col_key, "label": label})

        rows = []
        for idx, row_key in enumerate(row_keys):
            label = row_labels[idx] if idx < len(row_labels) and row_labels[idx] else row_key
            row_values = matrix.get(row_key) if isinstance(matrix.get(row_key), dict) else {}
            cells = []
            for column in columns:
                col_key = column["key"]
                checked = _to_checkmark_value(row_values.get(col_key, "")) == "✔"
                cells.append({
                    "key": col_key,
                    "label": column["label"],
                    "checked": checked,
                    "field": _program_inst_form_key(alignment_id, row_key, col_key),
                })
            rows.append({
                "key": row_key,
                "label": label,
                "present_field": _program_inst_present_form_key(alignment_id, row_key),
                "cells": cells,
            })

        display["program_institutional_alignments"].append({
            "id": alignment_id,
            "label": alignment.get("label") or "Program to Institutional Alignment",
            "rows": rows,
            "columns": columns,
            "row_count": len(rows),
            "column_count": len(columns),
        })
    for row in content["weekly_course_outline"]:
        # Detect field structure from format_hints or row content type
        spec = content.get("template_generation_spec", {})
        wo = spec.get("weekly_outline", {}) if isinstance(spec, dict) else {}
        hints = (wo.get("format_hints") or {}).get("fields") if isinstance(wo, dict) else None
        hints = hints if isinstance(hints, dict) else {}

        display_row = {
            **row,
            "mapped_clos_text": _list_to_text(row.get("mapped_clos", [])),
            "_display_fields": [],
            "_boilerplate_lead_in": (hints.get("intended_learning_outcomes") or {}).get("boilerplate_lead_in"),
            "_boilerplate_headings_topics": (hints.get("topics") or {}).get("boilerplate_structural_headings") or [],
        }
        # Build display fields for each column
        for field_name in _WEEKLY_FIELDS:
            spec_info = _weekly_field_spec(hints, field_name)
            field_value = row.get(field_name, "")
            label = _WEEKLY_FIELD_LABELS.get(field_name, field_name)

            entry = {"field": field_name, "label": label, "sub_fields": []}
            if spec_info["sub_fields"]:
                # Categorized — build sub-field entries with values
                subs = []
                field_data = row.get(field_name, {})
                if not isinstance(field_data, dict):
                    field_data = {}
                for sf in spec_info["sub_fields"]:
                    sub_val = _list_to_text(field_data.get(sf["key"], []))
                    subs.append({"key": sf["key"], "label": sf["label"], "value": sub_val})
                entry["sub_fields"] = subs
            else:
                # Flat — single value
                if isinstance(field_value, list):
                    entry["value"] = _list_to_text(field_value)
                elif isinstance(field_value, str):
                    entry["value"] = field_value
                elif isinstance(field_value, dict):
                    # Categorized in data but flat in hints — combine all sub-fields
                    combined = []
                    for v in field_value.values():
                        combined.extend(v if isinstance(v, list) else [v])
                    entry["value"] = "\n".join(str(c) for c in combined)
                else:
                    entry["value"] = str(field_value) if field_value else ""
            display_row["_display_fields"].append(entry)

        # Backward-compatible _text keys
        ilo = row.get("intended_learning_outcomes", {})
        if isinstance(ilo, dict):
            display_row["ilo_cognitive_text"] = _list_to_text(ilo.get("cognitive", []))
            display_row["ilo_affective_text"] = _list_to_text(ilo.get("affective", []))
            display_row["ilo_psychomotor_text"] = _list_to_text(ilo.get("psychomotor", []))
        else:
            display_row["ilo_cognitive_text"] = str(ilo) if isinstance(ilo, str) else ""
            display_row["ilo_affective_text"] = ""
            display_row["ilo_psychomotor_text"] = ""
        tla = row.get("teaching_learning_activities", {})
        if isinstance(tla, dict):
            display_row["tla_lecture_text"] = _list_to_text(tla.get("lecture", []))
            display_row["tla_practical_text"] = _list_to_text(tla.get("practical_session", []))
            display_row["tla_other_text"] = _list_to_text(tla.get("other", []))
        else:
            display_row["tla_lecture_text"] = str(tla) if isinstance(tla, str) else ""
            display_row["tla_practical_text"] = ""
            display_row["tla_other_text"] = ""
        resources = row.get("learning_resources", {})
        if isinstance(resources, dict):
            display_row["resources_clms_text"] = _list_to_text(resources.get("clms", []))
            display_row["resources_textbook_text"] = _list_to_text(resources.get("textbook", []))
            display_row["resources_website_text"] = _list_to_text(resources.get("website", []))
            display_row["resources_journal_text"] = _list_to_text(resources.get("journal", []))
            display_row["resources_other_text"] = _list_to_text(resources.get("other", []))
        else:
            display_row["resources_clms_text"] = str(resources) if isinstance(resources, str) else ""
            display_row["resources_textbook_text"] = ""
            display_row["resources_website_text"] = ""
            display_row["resources_journal_text"] = ""
            display_row["resources_other_text"] = ""
        display_row["topics_text"] = _list_to_text(row.get("topics", []))
        display_row["assessment_text"] = _list_to_text(row.get("assessment", []))
        display["weekly_rows"].append(display_row)
    return display
