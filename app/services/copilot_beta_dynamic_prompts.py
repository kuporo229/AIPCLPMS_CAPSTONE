"""
Dynamic prompt builders for the AI Copilot Beta workflow.

Each builder reads template profile data (generation spec, format hints,
template context) and assembles a task-specific system prompt that replaces
the hardcoded defaults in `copilot_beta_prompts.py`.

Key design:
- Feature flag `use_dynamic_copilot_prompts` in `system_settings` controls
  whether these are used.  Defaults to ``false`` for safe rollout.
- Every builder checks `_profile_has_minimum_data(content)` first; if the
  template profile hasn't populated enough data the builder returns ``None``
  and the caller falls back to the hardcoded prompt.
- All prompts are pure strings — no JSON parsing or AI calls inside builders.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from app.services.copilot_beta_prompts import (
    AQRF_LEVEL_6_OPTIONS,
    CORE_VALUE_OPTIONS,
    PQF_LEVEL_6_OPTIONS,
    SDG_CONTEXT,
    SDG_OPTIONS,
    SGA_OPTIONS,
)

# ---------------------------------------------------------------------------
# Universal quality rules — applied to every prompt regardless of template
# ---------------------------------------------------------------------------

_UNIVERSAL_QUALITY_RULES: Dict[str, List[str]] = {
    "clo": [
        "Each CLO statement must be a single measurable, observable sentence.",
        "Use action verbs at the appropriate Bloom's taxonomy level for the assigned domain.",
        "Align with the course title, course description, and service-learning component.",
        "No two CLOs should overlap in what they measure.",
        "Do not reference specific weeks, activities, or resources in CLO statements.",
        "If a source-template CLO statement is present, preserve its academic intent and revise only for clarity or measurable wording.",
    ],
    "alignment": [
        "Only use codes from the provided APPROVED lists.",
        "Alignments must be academically defensible from the CLO statement text alone.",
        "Vary alignments across CLOs; do not repeat the same combination across different CLOs.",
        "A CLO typically aligns with 4-7 program outcomes.",
        "For checkmark matrices: check only the POs each CLO specifically supports based on its statement; be selective, do not check all POs for every CLO.",
    ],
    "po_io": [
        "A program outcome typically aligns with 4-6 institutional outcomes.",
        "Map each PO to every IO it genuinely supports, even indirect connections.",
        "Do not leave a row blank — every PO connects to at least one IO.",
    ],
    "weekly": [
        "Every week fully populated — no empty arrays, no placeholder or generic filler.",
        "Build content resources-first: pick concrete references then derive topics, TLAs, assessments.",
        "Consecutive weeks must show clear progression; do not reuse identical topic/resource clusters.",
        "Every week MUST contain at least 2 items in cognitive, 2 in affective, and 2 in psychomotor within intended_learning_outcomes.",
        "Do not mention week labels or numbers inside content fields.",
        "All wording must be faculty-ready, measurable, and course-specific.",
        "Resources must be week-specific with real citations; never copy-paste boilerplate across rows.",
    ],
    "general": [
        "Return JSON only. No markdown fences, prose, notes, or explanations outside the JSON object.",
        "Do not invent codes, labels, or structure not provided in the prompt.",
        "Respect the exact JSON shape shown. Do not add extra top-level keys.",
        "If a field is mapped_clos, use ONLY CLO codes from the CLO REFERENCE DATA section.",
    ],
}


# ---------------------------------------------------------------------------
# Helper: minimum profile data gate
# ---------------------------------------------------------------------------

def _profile_has_minimum_data(content: Dict[str, Any], required_sections: List[str]) -> bool:
    """Return True when *content* has enough template profile data to build a
    meaningful dynamic prompt for the given *required_sections*.

    ``required_sections`` keys: ``"clo"``, ``"alignment"``, ``"weekly"``.
    """
    spec = content.get("template_generation_spec")
    if not isinstance(spec, dict):
        return False

    if "clo" in required_sections or "alignment" in required_sections:
        clo_groups = spec.get("clo_groups")
        if not isinstance(clo_groups, list) or not clo_groups:
            return False
        g0 = clo_groups[0]
        if not isinstance(g0, dict):
            return False
        rows = g0.get("rows")
        if not isinstance(rows, list) or not rows:
            return False

    if "weekly" in required_sections:
        wo = spec.get("weekly_outline")
        if not isinstance(wo, dict):
            return False
        rows = wo.get("rows")
        if not isinstance(rows, list) or not rows:
            return False

    return True


def _quality_block(*keys: str) -> str:
    """Return a combined quality rules string for the requested categories."""
    lines = []
    for key in keys:
        rules = _UNIVERSAL_QUALITY_RULES.get(key, [])
        for r in rules:
            lines.append(f"- {r}")
    return "\n".join(lines)


def _course_identity(content: Dict[str, Any]) -> str:
    """Return a short identity string for the course."""
    meta = content.get("metadata", {})
    dept = meta.get("department", "?")
    title = meta.get("course_title") or meta.get("title") or "the course"
    code = meta.get("course_code") or meta.get("code") or ""
    code_str = f" ({code})" if code else ""
    dept_str = f" in {dept}" if dept else ""
    return f'"{title}"{code_str}{dept_str}'

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _clo_blueprint_lines(content: Dict[str, Any]) -> str:
    """Return a markdown-ish list of CLO code → domain mappings."""
    spec = content.get("template_generation_spec", {})
    clo_groups = spec.get("clo_groups", [])
    g0 = clo_groups[0] if clo_groups else {}
    rows = g0.get("rows", []) if isinstance(g0, dict) else []
    if not rows:
        return "(No CLO blueprint — using defaults)"
    lines = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        code = r.get("code", "?")
        domain = r.get("domain", "cognitive")
        lines.append(f"  {code} → {domain}")
    return "\n".join(lines)


def _source_clo_seed_text(content: Dict[str, Any]) -> str:
    """Return the template's original CLO statements as prompt text."""
    spec = content.get("template_generation_spec", {})
    clo_groups = spec.get("clo_groups", [])
    g0 = clo_groups[0] if clo_groups else {}
    rows = g0.get("rows", []) if isinstance(g0, dict) else []
    parts = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        label = (r.get("label") or r.get("source_statement") or "").strip()
        if label and len(label) > 5:
            parts.append(f'  {r.get("code", "?")}: "{label}"')
    if not parts:
        return ""
    return "\n".join(parts)


def _format_rules_summary(content: Dict[str, Any]) -> str:
    """Build a CONCISE per-column output format specification."""
    spec = content.get("template_generation_spec", {})
    wo = spec.get("weekly_outline", {}) if isinstance(spec, dict) else {}
    fmt = wo.get("format_hints") if isinstance(wo, dict) else None
    hints = fmt.get("fields") if isinstance(fmt, dict) else None
    if not hints:
        return ""

    field_labels = {"time_frame_label":"Schedule","intended_learning_outcomes":"ILO","topics":"Topics","teaching_learning_activities":"TLA","assessment":"Assessment","learning_resources":"Resources"}

    blocks = []
    for field, info in hints.items():
        label = field_labels.get(field, field)
        style = info.get("output_style", "flat_lines")
        categories = info.get("category_labels", [])
        item_count = info.get("typical_item_count", 0)
        lead_in = info.get("lead_in")
        samples = info.get("samples", [])

        rules = []
        if lead_in:
            rules.append(f'Lead-in (COPY VERBATIM): "{lead_in[:100]}"')
        if item_count > 0:
            rules.append(f'Generate EXACTLY {item_count} items')
        if style == "categorized" and categories:
            rules.append(f'Use dict keys: {", ".join(categories[:4])}')
        elif style == "flat_lines":
            rules.append('Use flat list or multi-line string')
        if samples:
            rules.append(f'Sample: "{str(samples[0])[:100]}"')

        blocks.append(f"{label}: {' | '.join(rules)}")

    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# Prompt Builders
# ---------------------------------------------------------------------------

def _build_seed_clo_plan(content):
    """Determine CLO count/codes from available data when profile is insufficient."""
    clo_table = content.get("clo_alignment_table", [])
    if isinstance(clo_table, list) and clo_table:
        return [{"code": r.get("clo_code", f"CLO {i+1}"), "domain": r.get("domain", "cognitive")}
                for i, r in enumerate(clo_table) if isinstance(r, dict)]
    spec = content.get("template_generation_spec", {})
    clo_groups = spec.get("clo_groups", []) if isinstance(spec, dict) else []
    g0 = clo_groups[0] if isinstance(clo_groups, list) and clo_groups and isinstance(clo_groups[0], dict) else {}
    rows = g0.get("rows", []) if isinstance(g0, dict) else []
    if isinstance(rows, list) and rows:
        return [{"code": r.get("code", f"CLO {i+1}"), "domain": r.get("domain", "cognitive")}
                for i, r in enumerate(rows) if isinstance(r, dict)]
    from app.services.template_generation_spec import DEFAULT_CLO_ROWS
    return [{"code": code, "domain": domain} for code, domain in DEFAULT_CLO_ROWS[:4]]


def build_dynamic_clo_prompt(content: Dict[str, Any]) -> Optional[str]:
    """Generate a dynamic system prompt for CLO generation."""
    if not _profile_has_minimum_data(content, ["clo"]):
        return _build_seed_clo_prompt(content)

    spec = content.get("template_generation_spec", {})
    clo_groups = spec.get("clo_groups", [])
    g0 = clo_groups[0]
    rows = g0.get("rows", [])
    meta = content.get("metadata", {})

    # Build CLO plan
    clo_plan = [{"code": r.get("code", f"CLO {i+1}"), "domain": r.get("domain", "cognitive")}
                for i, r in enumerate(rows) if isinstance(r, dict)]
    clo_plan_json = json.dumps(clo_plan)

    # Source CLO statements
    source_seed = _source_clo_seed_text(content)

    # Program outcomes from template context (embedded in spec program_scope)
    pos = []
    scope = g0.get("program_scope", {})
    if isinstance(scope, dict):
        pos = scope.get("items", [])
    pos_json = json.dumps(pos)

    return f"""You are generating Course Learning Outcomes for {_course_identity(content)}.

CRITICAL — CLO BLUEPRINT: The template requires exactly {len(clo_plan)} CLOs with
these pre-assigned codes and Bloom's domains.  You MUST return exactly these
in this order — do NOT change codes or domains:

{_clo_blueprint_lines(content)}

{"SOURCE TEMPLATE CLO STATEMENTS (preserve academic intent; revise only for clarity or measurable wording):" if source_seed else ""}
{source_seed}

{"PROGRAM OUTCOMES (scope guidance — your CLOs should prepare students for these):" if pos else ""}
{pos_json}

QUALITY RULES:
{_quality_block("clo", "general")}

Return JSON only with this exact shape:
{{"clo_alignment_table": [{", ".join(f'{{"clo_code": "{r["code"]}", "domain": "{r["domain"]}", "clo_statement": "..."}}' for r in clo_plan)}]}}
""".strip()


def _build_seed_clo_prompt(content):
    clo_plan = _build_seed_clo_plan(content)
    source_seed = _source_clo_seed_text(content)
    return f"""You are generating Course Learning Outcomes for {_course_identity(content)}.

CRITICAL — CLO BLUEPRINT: The template requires exactly {len(clo_plan)} CLOs with
these pre-assigned codes and Bloom's domains.  You MUST return exactly these
in this order — do NOT change codes or domains:

{chr(10).join(f'  {r["code"]} → {r["domain"]}' for r in clo_plan)}

{"SOURCE TEMPLATE CLO STATEMENTS (preserve academic intent; revise only for clarity or measurable wording):" + chr(10) + source_seed if source_seed else ""}

QUALITY RULES:
{_quality_block("clo", "general")}

Return JSON only with this exact shape:
{{"clo_alignment_table": [{", ".join(f'{{"clo_code": "{r["code"]}", "domain": "{r["domain"]}", "clo_statement": "..."}}' for r in clo_plan)}]}}
""".strip()


def build_dynamic_alignment_checkmark_prompt(content: Dict[str, Any]) -> Optional[str]:
    """Generate a dynamic system prompt for checkmark CLO-PO alignment."""
    if not _profile_has_minimum_data(content, ["alignment"]):
        return _build_seed_alignment_checkmark_prompt(content)

    spec = content.get("template_generation_spec", {})
    groups = content.get("clo_alignment_groups", [])
    if not groups:
        groups = spec.get("clo_groups", [])
    meta = content.get("metadata", {})

    group_blocks = []
    for idx, g in enumerate(groups):
        if not isinstance(g, dict):
            continue
        group_id = g.get("group_id") or g.get("id") or f"clo_group_{idx + 1}"
        label = g.get("label", "CLO Alignment")
        po_codes = g.get("checkmark_po_codes", [])
        scope = g.get("program_scope", {})
        items = scope.get("items", []) if isinstance(scope, dict) else []
        po_descriptions = {item.get("code", ""): item.get("description", "") for item in items if isinstance(item, dict)}

        clo_rows = g.get("clo_alignment_table", [])
        clo_list = []
        for r in clo_rows:
            if isinstance(r, dict):
                clo_list.append({"clo_code": r.get("clo_code", "?"), "domain": r.get("domain", "?"),
                                  "statement": (r.get("clo_statement", "") or "")[:200]})

        block = f"""ALIGNMENT GROUP: "{label}"
  GROUP ID: {group_id}
  PO CODES: {json.dumps(po_codes)}
  PO DESCRIPTIONS: {json.dumps(po_descriptions)}
  CLO ROWS TO ALIGN: {json.dumps(clo_list)}"""
        group_blocks.append(block)

    return f"""You are generating CLO-to-PO checkmark alignment for {_course_identity(content)}.

ALIGNMENT STYLE: CHECKMARK MATRIX — one row per CLO, each column is a PO code.
  Value "✓" → the CLO supports that PO; empty string → does not.

{chr(10).join(group_blocks)}

QUALITY RULES:
{_quality_block("alignment", "general")}

Return JSON only with this exact shape:
{{"alignment_groups": [{{"group_id": "clo_group_1", "alignment_matrix": [{{"clo_code": "CLO 1", "checked_pos": ["PO_CODE_1", "PO_CODE_3", "PO_CODE_5", "PO_CODE_7"]}}, ...]}}]}}
""".strip()


def _build_seed_alignment_checkmark_prompt(content):
    """Seed-based fallback for checkmark alignment when profile data is insufficient."""
    clo_table = content.get("clo_alignment_table", [])
    if not isinstance(clo_table, list) or not clo_table:
        groups = content.get("clo_alignment_groups", [])
        if isinstance(groups, list) and groups:
            clo_table = groups[0].get("clo_alignment_table", []) if isinstance(groups[0], dict) else []

    clo_list = []
    for r in clo_table:
        if isinstance(r, dict):
            clo_list.append({"clo_code": r.get("clo_code", "?"), "domain": r.get("domain", "?"),
                              "statement": (r.get("clo_statement", "") or "")[:200]})

    spec = content.get("template_generation_spec", {})
    po_codes = []
    groups_spec = spec.get("clo_groups", []) if isinstance(spec, dict) else []
    if isinstance(groups_spec, list) and groups_spec:
        g0 = groups_spec[0] if isinstance(groups_spec[0], dict) else {}
        po_codes = g0.get("checkmark_po_codes", [])
    if not po_codes:
        content_groups = content.get("clo_alignment_groups", [])
        if isinstance(content_groups, list) and content_groups:
            g0c = content_groups[0] if isinstance(content_groups[0], dict) else {}
            po_codes = g0c.get("checkmark_po_codes", [])

    po_block = json.dumps(po_codes) if po_codes else "(No PO codes detected — use the template's PO column headers)"

    return f"""You are generating CLO-to-PO checkmark alignment for {_course_identity(content)}.

ALIGNMENT STYLE: CHECKMARK MATRIX — one row per CLO, each column is a PO code.
  Value "✓" → the CLO supports that PO; empty string → does not.

PO CODES: {po_block}

CLO ROWS TO ALIGN:
{json.dumps(clo_list)}

QUALITY RULES:
{_quality_block("alignment", "general")}

Return JSON only with this exact shape:
{{"alignment_groups": [{{"group_id": "clo_group_1", "alignment_matrix": [{{"clo_code": "CLO 1", "checked_pos": ["PO_CODE_1", "PO_CODE_3"]}}, ...]}}]}}
""".strip()


def build_dynamic_alignment_clo_based_prompt(content: Dict[str, Any]) -> Optional[str]:
    """Generate a dynamic system prompt for CLO-based alignment."""
    if not _profile_has_minimum_data(content, ["alignment"]):
        return _build_seed_alignment_clo_based_prompt(content)

    meta = content.get("metadata", {})
    spec = content.get("template_generation_spec", {})
    clo_groups = spec.get("clo_groups", [])
    g0 = clo_groups[0] if clo_groups else {}
    scope = g0.get("program_scope", {}) if isinstance(g0, dict) else {}
    items = scope.get("items", []) if isinstance(scope, dict) else []

    # Build approved lists from content's alignment_context
    alignment_context = content.get("alignment_context", {})
    sga = alignment_context.get("sga_options", SGA_OPTIONS)
    cv = alignment_context.get("core_value_options", CORE_VALUE_OPTIONS)
    pqf = alignment_context.get("pqf_level_6_options", PQF_LEVEL_6_OPTIONS)
    aqrf = alignment_context.get("aqrf_level_6_options", AQRF_LEVEL_6_OPTIONS)
    sdg = alignment_context.get("sdg_options", SDG_OPTIONS)
    sdg_ctx = alignment_context.get("sdg_context", SDG_CONTEXT)
    inst = alignment_context.get("institutional_context", {})

    return f"""You are generating CLO alignment data for {_course_identity(content)}.

Map each existing CLO to the APPROVED lists only. Never invent codes outside these lists.

APPROVED LISTS:
- Graduate Attributes (SGA): {json.dumps(sga)}
- Core Values: {json.dumps(cv)}
- PQF Level 6: {json.dumps(pqf)}
- AQRF Level 6: {json.dumps(aqrf)}
- SDGs: {json.dumps(sdg)}

SDG GUIDANCE:
{json.dumps(sdg_ctx)}

INSTITUTIONAL CONTEXT:
{json.dumps(inst)}

QUALITY RULES:
{_quality_block("alignment", "general")}

Return JSON only with this exact shape:
{{"target_sdgs_display": ["SDG 4", "SDG 9"],
  "clo_alignment_table": [{{"clo_code": "CLO 1",
    "aligned_plos": [], "graduate_attributes": [], "core_values": [],
    "pqf_level_6_alignment": [], "aqrf_level_6_alignment": [], "relevant_sdgs": []}}, ...]}}
""".strip()


def _build_seed_alignment_clo_based_prompt(content):
    """Seed-based fallback for CLO-based alignment when profile data is insufficient."""
    clo_table = content.get("clo_alignment_table", [])
    if not isinstance(clo_table, list) or not clo_table:
        groups = content.get("clo_alignment_groups", [])
        if isinstance(groups, list) and groups:
            clo_table = groups[0].get("clo_alignment_table", []) if isinstance(groups[0], dict) else []
    clo_list = []
    for r in clo_table:
        if isinstance(r, dict):
            clo_list.append({"clo_code": r.get("clo_code", "?"), "domain": r.get("domain", "?"),
                              "statement": (r.get("clo_statement", "") or "")[:200]})

    alignment_context = content.get("alignment_context", {})
    sga = alignment_context.get("sga_options", SGA_OPTIONS)
    cv = alignment_context.get("core_value_options", CORE_VALUE_OPTIONS)
    pqf = alignment_context.get("pqf_level_6_options", PQF_LEVEL_6_OPTIONS)
    aqrf = alignment_context.get("aqrf_level_6_options", AQRF_LEVEL_6_OPTIONS)
    sdg = alignment_context.get("sdg_options", SDG_OPTIONS)
    sdg_ctx = alignment_context.get("sdg_context", SDG_CONTEXT)
    inst = alignment_context.get("institutional_context", {})

    return f"""You are generating CLO alignment data for {_course_identity(content)}.

CLO ROWS:
{json.dumps(clo_list)}

Map each existing CLO to the APPROVED lists only. Never invent codes outside these lists.

APPROVED LISTS:
- Graduate Attributes (SGA): {json.dumps(sga)}
- Core Values: {json.dumps(cv)}
- PQF Level 6: {json.dumps(pqf)}
- AQRF Level 6: {json.dumps(aqrf)}
- SDGs: {json.dumps(sdg)}

SDG GUIDANCE:
{json.dumps(sdg_ctx)}

INSTITUTIONAL CONTEXT:
{json.dumps(inst)}

QUALITY RULES:
{_quality_block("alignment", "general")}

Return JSON only with this exact shape:
{{"target_sdgs_display": ["SDG 4", "SDG 9"],
  "clo_alignment_table": [{{"clo_code": "CLO 1",
    "aligned_plos": [], "graduate_attributes": [], "core_values": [],
    "pqf_level_6_alignment": [], "aqrf_level_6_alignment": [], "relevant_sdgs": []}}, ...]}}
""".strip()


def _build_architecture_block(fields_hints: Dict[str, Any], seed_rows: list) -> str:
    """Build a per-column architecture example from the template's first data row,
    using only the columns actually detected in the template."""
    if not fields_hints or not seed_rows:
        return "(No template seed content)"

    detected_fields = list(fields_hints.keys())
    field_labels = {
        "time_frame_label": "Schedule",
        "intended_learning_outcomes": "Learning Outcomes",
        "topics": "Topic Outline",
        "teaching_learning_activities": "Methodology",
        "assessment": "Assessment",
        "learning_resources": "Learning Resources",
    }

    # Map detected fields to their standard index positions
    field_order = [f for f in field_labels if f in fields_hints]

    arch_rows = []
    for r in seed_rows[:2]:
        if not isinstance(r, dict):
            continue
        label = r.get("label", "?")
        preview = r.get("source_preview", [])
        if not isinstance(preview, list):
            continue
        row_lines = [f"  TEMPLATE ROW [{label}]:"]
        # Find which preview index corresponds to each detected field
        for field_key in field_order:
            col_idx = list(field_labels.keys()).index(field_key)
            if col_idx < len(preview) and preview[col_idx]:
                col_label = field_labels.get(field_key, field_key)
                row_lines.append(f"    [{col_label}]: {preview[col_idx]}")
        if len(row_lines) > 1:
            arch_rows.append("\n".join(row_lines))

    return "\n\n".join(arch_rows[:1]) if arch_rows else "(No template seed content)"


def _build_preservation_rules(fields_hints: Dict[str, Any]) -> str:
    """Build per-column preservation rules dynamically from what the template
    scanner actually detected. Only includes rules for columns that exist."""
    if not fields_hints:
        return ""

    field_labels = {"time_frame_label":"Schedule","intended_learning_outcomes":"ILO","topics":"Topics","teaching_learning_activities":"TLA","assessment":"Assessment","learning_resources":"Resources"}
    sections = []
    for field, info in fields_hints.items():
        if not isinstance(info, dict): continue
        label = field_labels.get(field, field)
        rules = []
        lead_in = info.get("lead_in")
        item_count = info.get("typical_item_count", 0)

        if lead_in:
            rules.append(f'COPY VERBATIM: "{str(lead_in)[:120]}"')
        if item_count > 0 and field != "time_frame_label":
            rules.append(f'Generate EXACTLY {item_count} items')
        if field == "teaching_learning_activities":
            rules.append('Short labels, 2-5 words each, NO sentences')
        if field == "assessment":
            rules.append('Preserve sentence opening pattern from template')
        if field == "topics" and info.get("has_hierarchy"):
            rules.append('Preserve heading hierarchy: Unit/Lesson/Module')

        if rules:
            sections.append(f"{label}: {' | '.join(rules)}")

    return "\n".join(sections)


def _build_xcolumn_rules(fields_hints: Dict[str, Any]) -> str:
    """Build cross-column consistency rules for detected columns."""
    has_ilo = "intended_learning_outcomes" in fields_hints
    has_topics = "topics" in fields_hints
    has_assessment = "assessment" in fields_hints
    has_resources = "learning_resources" in fields_hints
    has_tla = "teaching_learning_activities" in fields_hints

    rules = []
    if has_ilo and has_topics:
        rules.append("Learning Outcomes list specific CLOs -> those CLOs must be covered in Topics")
    if has_ilo and has_assessment:
        rules.append("Assessments must evaluate the listed CLOs from Learning Outcomes")
    if has_topics and has_resources:
        rules.append("Resources must support the listed Topics")
    if has_ilo and has_tla:
        rules.append("Methodology must be appropriate for the listed CLOs (cognitive -> lecture/discussion; skills -> lab/practice)")

    if not rules:
        return ""
    lines = ["Each week's columns must be INTERNALLY CONSISTENT:"]
    lines.extend(f"  - {r}" for r in rules)
    return "\n".join(lines)



def _strict_item_count_block(content: Dict[str, Any]) -> str:
    """Build a PER-COLUMN STRICT ITEM COUNT block from format_hints."""
    spec = content.get("template_generation_spec", {})
    wo = spec.get("weekly_outline", {}) if isinstance(spec, dict) else {}
    fmt = wo.get("format_hints") if isinstance(wo, dict) else None
    hints = fmt.get("fields") if isinstance(fmt, dict) else None
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
        label = field_labels.get(field, field)
        cap = info.get("typical_item_count", 0)
        if cap <= 0:
            continue
        item_fmt = info.get("item_format", "inline")
        flat_short = info.get("is_flat_short_labels", False)

        style_desc = ""
        is_topics = (field == "topics")
        if not is_topics and info.get("output_style") == "categorized" and info.get("category_labels"):
            style_desc = f" ({', '.join(info['category_labels'][:4])})"
        elif item_fmt == "complete_sentences":
            style_desc = ", numbered, complete sentences"
        elif item_fmt == "semicolon_list":
            style_desc = ", semicolon list"
        elif item_fmt == "label_fragments":
            style_desc = ", label fragments"
        if flat_short:
            style_desc += ", short labels only"

        lines.append(f"  {label}: EXACTLY {cap} items{style_desc} — never exceed this cap")

    if not lines:
        return ""
    return "\nPER-COLUMN STRICT ITEM COUNT — never exceed:\n" + "\n".join(lines)


def build_dynamic_weekly_prompt(content: Dict[str, Any]) -> Optional[str]:
    """Generate a dynamic system prompt for weekly outline generation, fully
    driven by the template profile's column-level format hints."""
    if not _profile_has_minimum_data(content, ["weekly"]):
        return _build_seed_weekly_prompt(content)

    spec = content.get("template_generation_spec", {})
    wo = spec.get("weekly_outline", {})
    week_labels = [r.get("label") for r in content.get("weekly_course_outline", [])
                   if isinstance(r, dict) and r.get("label")]
    if not week_labels:
        week_labels = [r.get("label") for r in wo.get("rows", [])
                       if isinstance(r, dict) and r.get("label")]

    format_summary = _format_rules_summary(content)
    strict_count_summary = _strict_item_count_block(content)
    meta = content.get("metadata", {})

    # CLO reference data
    clo_rows = content.get("clo_alignment_table", [])
    clo_ref = []
    for r in clo_rows:
        if isinstance(r, dict):
            clo_ref.append({"code": r.get("clo_code", "?"), "domain": r.get("domain", "?"),
                            "statement": (r.get("clo_statement", "") or "")[:200]})

    # ── Per-column architecture examples from template ──
    seed_rows = wo.get("rows", [])
    # Get format hints for dynamic sections
    fmt = wo.get("format_hints") if isinstance(wo, dict) else {}
    fields_hints = fmt.get("fields") if isinstance(fmt, dict) else {}

    # ── Per-column architecture examples from template (only detected columns) ──
    seed_rows = wo.get("rows", [])
    architecture_block = _build_architecture_block(fields_hints, seed_rows)
    preservation_rules = _build_preservation_rules(fields_hints)
    xcolumn_rules = _build_xcolumn_rules(fields_hints)

    return f"""You are generating the weekly course outline for {_course_identity(content)}.

TEMPLATE STRUCTURE:
  Row count: {len(week_labels)}
  Labels in order: {json.dumps(week_labels)}
  Return exactly one row per label in this exact order.

{format_summary}

{strict_count_summary}

{preservation_rules}

{xcolumn_rules}

CLO REFERENCE DATA:
{json.dumps(clo_ref)}

INTENDED LEARNING OUTCOMES REQUIREMENT:
  Every week MUST contain `intended_learning_outcomes` with:
  - lead_in (string): "At the end of the week, students should have the ability to:"
  - cognitive (array): at least 2 items
  - affective (array): at least 2 items
  - psychomotor (array): at least 2 items

EXAM / REVIEW WEEK REQUIREMENTS:
  If a week label contains "exam", "midterm", "prelim", "finals", "final", "periodical",
  or similar assessment keywords, you MUST include ALL of the following:
  - learning_resources.clms: Include "Exam instructions and coverage", "Grading rubric"
  - learning_resources.other: Include "Consultation/remediation schedule"
  - teaching_learning_activities: Must include "Review and synthesis" activities
  - assessment: Must include the major examination or performance output
  - topics: Include "Exam coverage review" topics

  For pre-exam weeks (review, synthesis, consultation, recap, integration):
  - Include consultation resources (CLMS or other) with review materials
  - Include readiness check or mock exam assessments

QUALITY RULES:
{_quality_block("weekly", "general")}

Return JSON only with one top-level key `weekly_course_outline`.
""".strip()


def _build_seed_weekly_prompt(content):
    """Seed-based fallback for weekly outline when profile data is insufficient."""
    clo_table = content.get("clo_alignment_table", [])
    clo_ref = []
    for r in clo_table:
        if isinstance(r, dict):
            clo_ref.append({"code": r.get("clo_code", "?"), "domain": r.get("domain", "?"),
                            "statement": (r.get("clo_statement", "") or "")[:200]})

    weekly_rows = content.get("weekly_course_outline", [])
    week_labels = [r.get("time_frame_label") or r.get("label", "?") for r in weekly_rows
                   if isinstance(r, dict)]
    if not week_labels:
        spec = content.get("template_generation_spec", {})
        wo = spec.get("weekly_outline", {}) if isinstance(spec, dict) else {}
        wo_rows = wo.get("rows", []) if isinstance(wo, dict) else []
        week_labels = [r.get("label", f"Week {i+1}") for i, r in enumerate(wo_rows) if isinstance(r, dict)]
    if not week_labels:
        from app.services.copilot_beta_prompts import WEEK_ROW_LABELS
        week_labels = WEEK_ROW_LABELS[:18]

    format_hint = ""
    spec = content.get("template_generation_spec", {})
    wo = spec.get("weekly_outline", {}) if isinstance(spec, dict) else {}
    fmt = wo.get("format_hints") if isinstance(wo, dict) else {}
    if fmt:
        format_hint = _format_rules_summary(content)

    return f"""You are generating the weekly course outline for {_course_identity(content)}.

TEMPLATE STRUCTURE:
  Row count: {len(week_labels)}
  Labels in order: {json.dumps(week_labels)}
  Return exactly one row per label in this exact order.

{"FORMAT HINTS (from template detection):" + chr(10) + format_hint if format_hint else ""}

CLO REFERENCE DATA:
{json.dumps(clo_ref) if clo_ref else "(No CLOs generated yet — infer from course title/description)"}

INTENDED LEARNING OUTCOMES REQUIREMENT:
  Every week MUST contain `intended_learning_outcomes` with:
  - lead_in (string): "At the end of the week, students should have the ability to:"
  - cognitive (array): at least 2 items
  - affective (array): at least 2 items
  - psychomotor (array): at least 2 items

EXAM / REVIEW WEEK REQUIREMENTS:
  If a week label contains "exam", "midterm", "prelim", "finals", "final", "periodical",
  or similar assessment keywords, you MUST include ALL of the following:
  - learning_resources.clms: Include "Exam instructions and coverage", "Grading rubric"
  - learning_resources.other: Include "Consultation/remediation schedule"
  - teaching_learning_activities: Must include "Review and synthesis" activities
  - assessment: Must include the major examination or performance output
  - topics: Include "Exam coverage review" topics

QUALITY RULES:
{_quality_block("weekly", "general")}

Return JSON only with one top-level key `weekly_course_outline`.
""".strip()


def build_dynamic_po_io_prompt(content: Dict[str, Any]) -> Optional[str]:
    """Generate a dynamic system prompt for Program Outcomes to Institutional
    Outcomes checkmark alignment."""
    spec = content.get("template_generation_spec", {})
    inst_specs = spec.get("program_institutional_alignments", []) if isinstance(spec.get("program_institutional_alignments"), list) else []
    if not inst_specs:
        return _build_seed_po_io_prompt(content)

    meta = content.get("metadata", {})
    contract_blocks = []
    for item in inst_specs:
        if not isinstance(item, dict):
            continue
        contract_blocks.append(json.dumps({
            "id": item.get("id"),
            "row_labels": item.get("row_labels_normalized") or item.get("row_labels_original") or [],
            "column_labels": item.get("column_labels_normalized") or item.get("column_labels_original") or [],
        }))

    return f"""You are generating Program Outcomes to Institutional Outcomes checkmark alignment for {_course_identity(content)}.

ALIGNMENT STYLE: CHECKMARK MATRIX — one row per Program Outcome, each column is an Institutional Outcome code.
  Value "✔" → the PO supports that IO; empty string → does not.

TEMPLATE MATRIX CONTRACTS (use only these ids, row labels, and column labels):
{json.dumps(contract_blocks)}

DENSITY RULE: A program outcome typically aligns with 4-6 institutional outcomes.
Human-reviewed CLPs average 4-5 checkmarks per PO row. Do NOT be conservative.

When a PO mentions assessment, ethics, technology, communication, research, professionalism,
or leadership — these almost always connect to multiple IOs. Mark them all.

QUALITY RULES:
{_quality_block("po_io", "general")}

Return JSON only with one top-level key `program_institutional_alignments`.
""".strip()


def _build_seed_po_io_prompt(content):
    """Seed-based fallback for PO-IO alignment when no template spec is available."""
    meta = content.get("metadata", {})
    alignment_bundle = content.get("_alignment_bundle") or {}
    program_outcomes = alignment_bundle.get("program_outcomes", [])
    inst_context = content.get("alignment_context", {}).get("institutional_context", {})
    institutional_outcomes = inst_context.get("institutional_outcomes", [])

    row_labels = [po.get("code", f"PO {i+1}") for i, po in enumerate(program_outcomes) if isinstance(po, dict)]
    col_labels = [io.get("code", f"IO {i+1}") for i, io in enumerate(institutional_outcomes) if isinstance(io, dict)]

    if not row_labels:
        row_labels = ["PO 1", "PO 2", "PO 3", "PO 4", "PO 5", "PO 6", "PO 7"]
    if not col_labels:
        col_labels = ["IO 1", "IO 2", "IO 3", "IO 4", "IO 5", "IO 6"]

    contract = {"id": "po_io_matrix_1", "row_labels": row_labels, "column_labels": col_labels}

    return f"""You are generating Program Outcomes to Institutional Outcomes checkmark alignment for {_course_identity(content)}.

ALIGNMENT STYLE: CHECKMARK MATRIX — one row per Program Outcome, each column is an Institutional Outcome code.
  Value "✔" → the PO supports that IO; empty string → does not.

TEMPLATE MATRIX CONTRACTS (use only these ids, row labels, and column labels):
{json.dumps([contract])}

DENSITY RULE: A program outcome typically aligns with 4-6 institutional outcomes.
Human-reviewed CLPs average 4-5 checkmarks per PO row. Do NOT be conservative.

When a PO mentions assessment, ethics, technology, communication, research, professionalism,
or leadership — these almost always connect to multiple IOs. Mark them all.

QUALITY RULES:
{_quality_block("po_io", "general")}

Return JSON only with one top-level key `program_institutional_alignments`.
""".strip()
