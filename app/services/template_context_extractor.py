"""
Extract academic reference context from CLP DOCX templates.

Template profiles describe where content should be written. This module
extracts the academic context already written in the template, such as PLOs,
SDGs, graduate attributes, core values, PQF, and AQRF rows, so AI generation
can prefer the template's own framework over hardcoded defaults.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from docx.document import Document as _Document


_PLO_RE = re.compile(r"\b(?:PLO|PO|PL)\s*0?\d+\b", re.IGNORECASE)
_PROGRAM_OUTCOME_CODE_RE = re.compile(
    r"\b(?:PLO|PO|PL|BPED|BSED[A-Z]*|BS[A-Z]{2,8}|[A-Z]{3,12})\s*0*\d+\b",
    re.IGNORECASE,
)
_SDG_RE = re.compile(r"\b(SDG|GOAL)\s*0?\d+\b|SUSTAINABLE DEVELOPMENT", re.IGNORECASE)
_PQF_RE = re.compile(r"\bPQF\b|PHILIPPINE QUALIFICATION", re.IGNORECASE)
_AQRF_RE = re.compile(r"\bAQRF\b|ASEAN QUALIFICATION", re.IGNORECASE)
_GRAD_ATTR_RE = re.compile(r"GRADUATE\s*/?\s*ATTRIBUTE|GRADUATE ATTRIBUTE|\bSGA\b", re.IGNORECASE)
_CORE_VALUE_RE = re.compile(r"CORE\s+VALUES?|INSTITUTIONAL\s+VALUES?|SALETTINIAN\s+VALUES?", re.IGNORECASE)
_INSTITUTIONAL_OUTCOME_RE = re.compile(r"INSTITUTIONAL\s+OUTCOMES?|UNIVERSITY\s+OUTCOMES?", re.IGNORECASE)
_REFERENCE_RE = re.compile(r"REFERENCES?|BIBLIOGRAPHY|BOOKS?|JOURNALS?|WEBSITES?|CLMS", re.IGNORECASE)
_KNOWN_CORE_VALUES = ["FAITH", "RECONCILIATION", "INTEGRITY", "EXCELLENCE", "SOLIDARITY"]
_KNOWN_GRADUATE_ATTRIBUTES = [
    "Transformative Leaders",
    "Reconcilers",
    "Industry Competent",
    "Research-Oriented",
    "Information and Communication Technology Proficient",
    "Critical Thinkers",
    "Holistic Persons",
]

# Canonical descriptions for known frameworks — used to enrich extractions
# that only have codes/titles but are missing descriptions.

_KNOWN_CORE_VALUE_DESCRIPTIONS = {
    "FAITH": "Living faith through academic excellence, spiritual growth, and commitment to Christian values in daily life and professional practice.",
    "RECONCILIATION": "Promoting healing, forgiveness, and restoration of relationships through dialogue, compassion, and peace-building in communities.",
    "INTEGRITY": "Upholding honesty, ethical conduct, transparency, and moral uprightness in all academic and professional endeavors.",
    "EXCELLENCE": "Pursuing the highest standards of quality, continuous improvement, and outstanding achievement in education, research, and service.",
    "SOLIDARITY": "Standing together with the marginalized, fostering social responsibility, and building inclusive communities through cooperation and mutual support.",
}

_KNOWN_GRADUATE_ATTRIBUTE_DESCRIPTIONS = {
    "Transformative Leaders": "Graduates who lead positive change in their communities, organizations, and professions through innovative thinking and ethical decision-making.",
    "Reconcilers": "Graduates who promote peace, healing, and reconciliation in diverse settings, bridging differences through dialogue and understanding.",
    "Industry Competent": "Graduates who possess the technical skills, professional knowledge, and practical competencies required by industry standards and employer expectations.",
    "Research-Oriented": "Graduates who apply scientific methods, critical inquiry, and evidence-based approaches to solve problems and generate new knowledge.",
    "Information and Communication Technology Proficient": "Graduates who effectively use modern ICT tools, digital platforms, and emerging technologies to enhance productivity and innovation.",
    "Critical Thinkers": "Graduates who analyze information objectively, evaluate evidence systematically, and make well-reasoned judgments in complex situations.",
    "Holistic Persons": "Graduates who demonstrate balanced development across intellectual, emotional, spiritual, physical, and social dimensions of life.",
}

_KNOWN_PQF_LEVEL_6_DESCRIPTIONS = {
    "PQF 1": "Knowledge: Demonstrate broad and coherent knowledge and understanding in a specific field of study or practice.",
    "PQF 2": "Skills: Apply a wide range of technical, creative, and analytical skills in planning and completing tasks, including formulating responses to well-defined abstract and concrete problems.",
    "PQF 3": "Values: Exercise self-management within the guidelines of established practice. Supervise the work of others in assigned roles. Take responsibility for own outputs relative to specified quality standards. Manage own learning needs within a structured environment.",
    "PQF 4": "Application: Apply knowledge and skills with substantial depth in some areas to activities in a field of work or study.",
    "PQF 5": "Degree of Independence: Work effectively under guidance in a peer relationship with qualified practitioners, manage own work activities, and assume limited responsibility in a structured environment.",
    "PQF 6": "Comprehensive Competency: Demonstrate comprehensive knowledge, understanding, and skills required for a specific field, applying these effectively in professional and community settings.",
}

_KNOWN_AQRF_LEVEL_6_DESCRIPTIONS = {
    "AQRF 1": "Knowledge and Understanding: Apply in-depth knowledge of a field of study, including critical understanding of theories, principles, and concepts.",
    "AQRF 2": "Application and Action: Plan and execute a major project or equivalent activities, using a range of technical and analytical skills in a broad range of activities.",
    "AQRF 3": "Communication Skills: Communicate knowledge, understanding, skills, and activities to peers and supervisors using appropriate methods.",
    "AQRF 4": "Autonomy and Responsibility: Manage and supervise in contexts of work and study activities where there is unpredictable change. Review and develop performance of self and others.",
    "AQRF 5": "Lifelong Learning: Evaluate and develop own learning needs and those of others to undertake further study with a high degree of autonomy.",
    "AQRF 6": "Professional and Ethical Practice: Exercise professional and ethical responsibility in complex and unpredictable situations within a specific field of work or study.",
}

_KNOWN_SDG_TITLES = {
    "1": "No Poverty",
    "2": "Zero Hunger",
    "3": "Good Health and Well-being",
    "4": "Quality Education",
    "5": "Gender Equality",
    "6": "Clean Water and Sanitation",
    "7": "Affordable and Clean Energy",
    "8": "Decent Work and Economic Growth",
    "9": "Industry, Innovation and Infrastructure",
    "10": "Reduced Inequalities",
    "11": "Sustainable Cities and Communities",
    "12": "Responsible Consumption and Production",
    "13": "Climate Action",
    "14": "Life Below Water",
    "15": "Life on Land",
    "16": "Peace, Justice and Strong Institutions",
    "17": "Partnerships for the Goals",
}

_KNOWN_SDG_DESCRIPTIONS = {
    "1": "End poverty in all its forms everywhere. Ensure that all people have equal rights to economic resources, basic services, and social protection.",
    "2": "End hunger, achieve food security and improved nutrition, and promote sustainable agriculture.",
    "3": "Ensure healthy lives and promote well-being for all at all ages. Strengthen the prevention and treatment of health risks.",
    "4": "Ensure inclusive and equitable quality education and promote lifelong learning opportunities for all.",
    "5": "Achieve gender equality and empower all women and girls. End all forms of discrimination and violence against women.",
    "6": "Ensure availability and sustainable management of water and sanitation for all.",
    "7": "Ensure access to affordable, reliable, sustainable, and modern energy for all.",
    "8": "Promote sustained, inclusive, and sustainable economic growth, full and productive employment, and decent work for all.",
    "9": "Build resilient infrastructure, promote inclusive and sustainable industrialization, and foster innovation.",
    "10": "Reduce inequality within and among countries. Promote social, economic, and political inclusion of all.",
    "11": "Make cities and human settlements inclusive, safe, resilient, and sustainable.",
    "12": "Ensure sustainable consumption and production patterns. Reduce waste generation through prevention, reduction, recycling, and reuse.",
    "13": "Take urgent action to combat climate change and its impacts through education, awareness, and institutional capacity.",
    "14": "Conserve and sustainably use the oceans, seas, and marine resources for sustainable development.",
    "15": "Protect, restore, and promote sustainable use of terrestrial ecosystems, sustainably manage forests, combat desertification, and halt biodiversity loss.",
    "16": "Promote peaceful and inclusive societies for sustainable development, provide access to justice for all, and build effective, accountable, and inclusive institutions.",
    "17": "Strengthen the means of implementation and revitalize the global partnership for sustainable development.",
}


def extract_template_context(doc: _Document) -> Dict[str, Any]:
    """Extract template-owned academic context from a DOCX document."""
    context = {
        "program_outcomes": [],
        "institutional_outcomes": [],
        "sdg_context": [],
        "graduate_attributes": [],
        "core_values": [],
        "pqf_alignment": [],
        "aqrf_alignment": [],
        "references": {
            "books": [],
            "journals": [],
            "websites": [],
            "clms": [],
            "other": [],
        },
        "institutional_context": {
            "vision": "",
            "mission": "",
            "core_competencies": [],
            "institutional_objectives": [],
        },
        "detected_tables": [],
        "warnings": [],
    }

    for table_index, table in enumerate(doc.tables):
        rows = _table_rows(table)
        if not rows:
            continue
        table_text = "\n".join(" | ".join(row) for row in rows)
        _extract_inline_institutional_context(table_text, context)
        _merge_inline_program_outcomes(context, table_text)
        if _is_metadata_table(rows):
            _merge_alignment_context(context, {"sdg_context": _extract_sdg_from_metadata_rows(rows)})
        if _is_clo_alignment_table(rows):
            _merge_alignment_context(context, _extract_alignment_table_context(rows))
            context["detected_tables"].append({
                "table_index": table_index,
                "kind": "alignment_context",
                "confidence": 0.88,
                "row_count": len(rows),
                "sample": table_text[:500],
            })
            continue
        kind = _classify_table(rows, table_text)
        if not kind:
            continue

        extracted = _extract_rows_for_kind(kind, rows)
        if _has_extracted_values(kind, extracted):
            _merge_context(context, kind, extracted)
            context["detected_tables"].append({
                "table_index": table_index,
                "kind": kind,
                "confidence": _confidence_for(kind, rows, table_text),
                "row_count": len(rows),
                "sample": table_text[:500],
            })

    _extract_reference_paragraphs(doc, context)
    _dedupe_context(context)
    _summarize_warnings(context)
    return context


def build_alignment_bundle_from_template_context(template_context: Dict[str, Any]) -> Dict[str, Any]:
    """Convert extracted template context into copilot alignment bundle shape."""
    template_context = template_context or {}
    program_outcomes = []
    for group in template_context.get("program_outcomes") or []:
        for item in group.get("items") or []:
            code = str(item.get("code") or "").strip()
            description = str(item.get("description") or item.get("title") or "").strip()
            if code:
                program_outcomes.append({"code": code, "description": description})

    def option_labels(key):
        return [
            _item_label(item)
            for item in template_context.get(key) or []
            if _item_label(item)
        ]

    def detail_rows(key):
        rows = []
        for item in template_context.get(key) or []:
            label = _item_label(item)
            if label:
                rows.append({"label": label, "meaning": str(item.get("description") or "").strip()})
        return rows

    sdg_context = {}
    for item in template_context.get("sdg_context") or []:
        code = _item_label(item)
        if code:
            sdg_context[code] = {
                "title": str(item.get("title") or code).strip(),
                "guidance": str(item.get("description") or "").strip(),
            }

    return {
        "program_outcomes": program_outcomes,
        "program_headers": [row["code"] for row in program_outcomes],
        "institutional_outcomes": [
            {"code": _item_label(item), "description": str(item.get("description") or "").strip()}
            for item in template_context.get("institutional_outcomes") or []
            if _item_label(item)
        ],
        "institutional_headers": option_labels("institutional_outcomes"),
        "alignment_context": {
            "institutional_outcome_options": option_labels("institutional_outcomes"),
            "institutional_outcome_details": detail_rows("institutional_outcomes"),
            "sga_options": option_labels("graduate_attributes"),
            "sga_details": detail_rows("graduate_attributes"),
            "core_value_options": option_labels("core_values"),
            "core_value_details": detail_rows("core_values"),
            "pqf_level_6_options": option_labels("pqf_alignment"),
            "pqf_level_6_details": detail_rows("pqf_alignment"),
            "aqrf_level_6_options": option_labels("aqrf_alignment"),
            "aqrf_level_6_details": detail_rows("aqrf_alignment"),
            "sdg_options": option_labels("sdg_context"),
            "sdg_context": sdg_context,
            "institutional_context": template_context.get("institutional_context") or {},
        },
        "template_context": template_context,
        "source": "template_profile",
    }


def merge_template_bundle_with_admin(template_bundle: Dict[str, Any], admin_bundle: Dict[str, Any]) -> Dict[str, Any]:
    """Prefer template-derived context category-by-category, falling back to admin."""
    if not template_bundle:
        return admin_bundle
    merged = {
        "program_outcomes": admin_bundle.get("program_outcomes", []),
        "program_headers": admin_bundle.get("program_headers", []),
        "alignment_context": dict(admin_bundle.get("alignment_context", {}) or {}),
        "template_context": template_bundle.get("template_context", {}),
        "source": "template_profile_with_admin_fallback",
    }
    if template_bundle.get("program_outcomes"):
        merged["program_outcomes"] = template_bundle["program_outcomes"]
        merged["program_headers"] = template_bundle.get("program_headers", [])
    if template_bundle.get("institutional_outcomes"):
        merged["institutional_outcomes"] = template_bundle["institutional_outcomes"]
        merged["institutional_headers"] = template_bundle.get("institutional_headers", [])

    template_ctx = template_bundle.get("alignment_context", {}) or {}
    for key, value in template_ctx.items():
        if isinstance(value, dict) and value:
            merged["alignment_context"][key] = value
        elif isinstance(value, list) and value:
            merged["alignment_context"][key] = value
    return merged


def context_counts(template_context: Dict[str, Any]) -> Dict[str, int]:
    template_context = template_context or {}
    return {
        "program_outcomes": sum(len(group.get("items") or []) for group in template_context.get("program_outcomes") or []),
        "institutional_outcomes": len(template_context.get("institutional_outcomes") or []),
        "sdg_context": len(template_context.get("sdg_context") or []),
        "graduate_attributes": len(template_context.get("graduate_attributes") or []),
        "core_values": len(template_context.get("core_values") or []),
        "pqf_alignment": len(template_context.get("pqf_alignment") or []),
        "aqrf_alignment": len(template_context.get("aqrf_alignment") or []),
        "references": sum(len(v or []) for v in (template_context.get("references") or {}).values()),
        "institutional_context": _institutional_context_count(template_context.get("institutional_context") or {}),
    }


def _table_rows(table) -> List[List[str]]:
    rows = []
    for row in table.rows:
        values = [_clean(cell.text) for cell in row.cells]
        if any(values):
            rows.append(values)
    return rows


def _classify_table(rows: List[List[str]], table_text: str) -> Optional[str]:
    first_rows = "\n".join(" | ".join(row) for row in rows[:3])
    if _is_metadata_table(rows) or _is_clo_alignment_table(rows) or _is_weekly_outline_table(rows):
        return None
    if _INSTITUTIONAL_OUTCOME_RE.search(table_text):
        return "institutional_outcomes"
    if _is_institutional_context_text(table_text):
        return None
    if (
        len(rows) >= 3
        and _PROGRAM_OUTCOME_CODE_RE.search(table_text)
        and re.search(r"PROGRAM\s+(?:LEARNING\s+)?OUTCOMES?", first_rows, re.IGNORECASE)
    ):
        return "program_outcomes"
    if _SDG_RE.search(table_text):
        return "sdg_context"
    # Guard: if the table is dense with PO codes it is a program outcomes block,
    # not a graduate_attributes table — even if grad attribute names appear in it
    # (EDUC templates embed both Holistic Persons text AND the BPED PO list together).
    if _GRAD_ATTR_RE.search(table_text):
        po_code_count = len(_PROGRAM_OUTCOME_CODE_RE.findall(table_text))
        if po_code_count >= 5:
            return "program_outcomes"
        return "graduate_attributes"
    if _CORE_VALUE_RE.search(table_text):
        return "core_values"
    if _PQF_RE.search(table_text):
        return "pqf_alignment"
    if _AQRF_RE.search(table_text):
        return "aqrf_alignment"
    if _REFERENCE_RE.search(first_rows):
        return "references"
    return None


def _extract_rows_for_kind(kind: str, rows: List[List[str]]) -> Any:
    if kind == "program_outcomes":
        return {"program_code": _infer_program_code(rows), "program_name": _infer_program_name(rows), "items": _extract_code_rows(rows, _PROGRAM_OUTCOME_CODE_RE)}
    if kind == "institutional_outcomes":
        return _extract_institutional_outcomes(rows)
    if kind == "sdg_context":
        return _extract_sdg_table_with_targets(rows)
    if kind == "graduate_attributes":
        return _extract_framework_table_with_details(rows, re.compile(r"\b(?:GA|SGA)\s*0?\d*\b", re.IGNORECASE), allow_title_only=True)
    if kind == "core_values":
        return _extract_framework_table_with_details(rows, re.compile(r"\b(?:CV)\s*0?\d*\b", re.IGNORECASE), allow_title_only=True)
    if kind == "pqf_alignment":
        return _extract_framework_table_with_details(rows, re.compile(r"\bPQF\s*0?\d+\b", re.IGNORECASE), fallback_prefix="PQF")
    if kind == "aqrf_alignment":
        return _extract_framework_table_with_details(rows, re.compile(r"\bAQRF\s*0?\d+\b", re.IGNORECASE), fallback_prefix="AQRF")
    if kind == "references":
        return _extract_references_from_rows(rows)
    return []


def _merge_inline_program_outcomes(context: Dict[str, Any], text: str) -> None:
    groups = _extract_inline_program_outcomes(text)
    if groups:
        context.setdefault("program_outcomes", []).extend(groups)


def _extract_inline_program_outcomes(text: str) -> List[Dict[str, Any]]:
    """Extract dynamic program-outcome groups from prose-heavy cells.

    Handles blocks such as "PROGRAM OUTCOMES ... BPED 1 - ... BPED 2 - ..."
    without assuming a particular department, program code, or count.
    """
    clean = _clean(text)
    if not re.search(r"PROGRAM\s+(?:LEARNING\s+)?OUTCOMES?", clean, re.IGNORECASE):
        return []
    matches = list(_PROGRAM_OUTCOME_CODE_RE.finditer(clean))
    if len(matches) < 2:
        return []

    grouped: Dict[str, Dict[str, Any]] = {}
    for index, match in enumerate(matches):
        raw_code = _normalize_program_outcome_code(match.group(0))
        if not raw_code:
            continue
        prefix = _program_code_prefix(raw_code)
        if not prefix:
            continue
        if prefix in {"PLO", "PO", "PL", "LEVEL"}:
            continue
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(clean)
        description = _clean(clean[match.end():next_start].strip(" :-–—;,."))
        if not description:
            continue
        group = grouped.setdefault(prefix, {
            "program_code": prefix,
            "program_name": _infer_inline_program_name(clean, prefix),
            "items": [],
        })
        group["items"].append({
            "code": raw_code,
            "title": raw_code,
            "description": description,
        })

    return [group for group in grouped.values() if len(group.get("items") or []) >= 2]


def _normalize_program_outcome_code(value: str) -> str:
    match = re.match(r"\s*([A-Z]{2,12}|PLO|PO|PL)\s*0*(\d+)\s*$", str(value or ""), re.IGNORECASE)
    if not match:
        return ""
    prefix = match.group(1).upper()
    if prefix in {"CLO", "SDG", "PQF", "AQRF", "CV"}:
        return ""
    if prefix == "PL":
        prefix = "PLO"
    return f"{prefix} {int(match.group(2))}"


def _program_code_prefix(code: str) -> str:
    match = re.match(r"([A-Z]{2,12}|PLO|PO)\s+\d+$", code or "")
    return match.group(1) if match else ""


def _infer_inline_program_name(text: str, prefix: str) -> str:
    prefix_re = re.escape(prefix)
    match = re.search(
        rf"(?:Bachelor|minimum standards?)[^.:\n|]{{0,180}}?\b{prefix_re}\b",
        text,
        re.IGNORECASE,
    )
    if match:
        candidate = re.sub(r"\s+", " ", match.group(0)).strip(" :-–—")
        candidate = re.sub(r"^The\s+minimum\s+standards?\s+for\s+the\s+", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s+are\s+expressed.*$", "", candidate, flags=re.IGNORECASE)
        if 4 <= len(candidate) <= 120:
            return candidate
    return prefix


def _extract_institutional_outcomes(rows: List[List[str]]) -> List[Dict[str, str]]:
    """Extract institution-wide outcomes from prose or table layouts.

    Many CLP templates store these as title-like labels followed by prose in a
    single cell, for example "Transformative Leaders. Active involvement ...".
    Prefer known outcome names when present, then fall back to generic
    title-description parsing for other schools' layouts.
    """
    text = _clean(" ".join(" ".join(row) for row in rows))
    text = _INSTITUTIONAL_OUTCOME_RE.sub("", text, count=1).strip(" :-–—")
    text = _trim_to_institutional_outcomes_block(text)
    known_items = _extract_named_outcome_block(text, _KNOWN_GRADUATE_ATTRIBUTES)
    if known_items:
        return known_items

    items: List[Dict[str, str]] = []
    for row in rows:
        for cell in row:
            clean = _clean(_INSTITUTIONAL_OUTCOME_RE.sub("", cell, count=1).strip(" :-–—"))
            item = _parse_named_description(clean)
            if item:
                items.append(item)
    if items:
        return items

    return [
        {"code": item, "title": item, "description": _lookup_known_description(item)}
        for item in _split_institutional_items(text)
        if item and not _INSTITUTIONAL_OUTCOME_RE.fullmatch(item)
    ]


def _trim_to_institutional_outcomes_block(text: str) -> str:
    """Stop institutional-outcome prose before the next template section."""
    clean = _clean(text)
    boundary_re = re.compile(
        r"\b("
        r"PROGRAM\s+(?:LEARNING\s+)?OUTCOMES?|"
        r"COURSE\s+INFORMATION|"
        r"COURSE\s+(?:LEARNING\s+)?OUTCOMES?|"
        r"COURSE\s+REQUIREMENTS|"
        r"WEEKLY\s+(?:COURSE\s+)?(?:LEARNING\s+)?(?:PLAN|OUTLINE)|"
        r"ASSESSMENT|"
        r"REFERENCES?|"
        r"CONSULTATION"
        r")\b",
        re.IGNORECASE,
    )
    match = boundary_re.search(clean)
    if match:
        return _clean(clean[:match.start()].rstrip(" |:-–—"))
    return clean


def _extract_named_outcome_block(text: str, known_titles: List[str]) -> List[Dict[str, str]]:
    clean = _clean(text)
    if not clean:
        return []
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(item) for item in known_titles) + r")\b\s*[-–—:.]?",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(clean))
    items: List[Dict[str, str]] = []
    for index, match in enumerate(matches):
        title = _canonical_title(match.group(1), known_titles)
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(clean)
        description = _clean(clean[start:end].strip(" :-–—."))
        items.append({
            "code": title,
            "title": title,
            "short_code": _short_code_for_title(title),
            "description": description or _lookup_known_description(title),
        })
    return items


def _canonical_title(value: str, known_titles: List[str]) -> str:
    for title in known_titles:
        if title.lower() == str(value or "").strip().lower():
            return title
    return _clean(value)


def _short_code_for_title(title: str) -> str:
    clean = _clean(title)
    if not clean:
        return ""
    words = re.findall(r"[A-Za-z0-9]+", clean)
    if not words:
        return clean[:1].upper()
    if len(words) == 1:
        return words[0][:1].upper()
    return "".join(word[:1].upper() for word in words if word).strip()


def _extract_alignment_table_context(rows: List[List[str]]) -> Dict[str, List[Dict[str, str]]]:
    """Extract framework choices embedded as columns in CLO alignment tables."""
    header = _find_alignment_header(rows)
    if not header:
        return {}
    col_map = _alignment_column_map(header)
    extracted = {
        "sdg_context": [],
        "graduate_attributes": [],
        "core_values": [],
        "pqf_alignment": [],
        "aqrf_alignment": [],
    }
    for row in rows:
        if _is_alignment_header_or_separator(row):
            continue
        for key, col_idx in col_map.items():
            if col_idx >= len(row):
                continue
            text = _clean(row[col_idx])
            if not text:
                continue
            if key == "sdg_context":
                extracted[key].extend(_extract_sdg_items_from_text(text))
            elif key == "graduate_attributes":
                extracted[key].extend(_extract_named_options(text, _KNOWN_GRADUATE_ATTRIBUTES))
            elif key == "core_values":
                extracted[key].extend(_extract_named_options(text, _KNOWN_CORE_VALUES))
            elif key == "pqf_alignment":
                extracted[key].extend(_extract_code_or_text_options(text, re.compile(r"\bPQF\s*0?\d+\b", re.IGNORECASE)))
            elif key == "aqrf_alignment":
                extracted[key].extend(_extract_code_or_text_options(text, re.compile(r"\bAQRF\s*0?\d+\b", re.IGNORECASE)))
    return extracted


def _find_alignment_header(rows: List[List[str]]) -> List[str]:
    for row in rows[:4]:
        joined = " ".join(row).upper()
        if "COURSE LEARNING OUTCOMES" in joined and ("RELEVANT SDG" in joined or "ALIGNED PROGRAM" in joined):
            return row
    return []


def _alignment_column_map(header: List[str]) -> Dict[str, int]:
    mapping = {}
    for idx, cell in enumerate(header):
        upper = cell.upper()
        if "GRADUATE" in upper and "ATTRIBUTE" in upper:
            mapping["graduate_attributes"] = idx
        elif "CORE" in upper and "VALUE" in upper:
            mapping["core_values"] = idx
        elif "PQF" in upper:
            mapping["pqf_alignment"] = idx
        elif "AQRF" in upper:
            mapping["aqrf_alignment"] = idx
        elif "SDG" in upper:
            mapping["sdg_context"] = idx
    return mapping


def _is_alignment_header_or_separator(row: List[str]) -> bool:
    joined = " ".join(row).upper()
    first_nonempty = next((_clean(cell).upper() for cell in row if _clean(cell)), "")
    if "COURSE LEARNING OUTCOMES" in joined:
        return True
    return first_nonempty in {"COGNITIVE", "AFFECTIVE", "PSYCHOMOTOR"}


def _extract_named_options(text: str, known_options: List[str]) -> List[Dict[str, str]]:
    found = []
    text_norm = _clean(text)
    for option in known_options:
        if re.search(rf"\b{re.escape(option)}\b", text_norm, re.IGNORECASE):
            desc = _lookup_known_description(option)
            found.append({"code": option, "title": option, "description": desc})
    if found:
        return found
    chunks = re.split(r"[,;/\n]+|\s{2,}", text_norm)
    results = []
    for chunk in chunks:
        chunk = chunk.strip()
        if chunk:
            desc = _lookup_known_description(chunk)
            results.append({"code": chunk, "title": chunk, "description": desc})
    return results


def _extract_code_or_text_options(text: str, code_re) -> List[Dict[str, str]]:
    codes = [match.group(0).upper().replace("  ", " ") for match in code_re.finditer(text)]
    if codes:
        return [{"code": code, "title": code, "description": _lookup_known_description(code)} for code in codes]
    clean = _clean(text)
    if not clean:
        return []
    desc = _lookup_known_description(clean)
    return [{"code": clean, "title": clean, "description": desc}]


def _extract_sdg_from_metadata_rows(rows: List[List[str]]) -> List[Dict[str, str]]:
    items = []
    for row in rows:
        if not row:
            continue
        if "SDG" in row[0].upper() and len(row) > 1:
            items.extend(_extract_sdg_items_from_text(" ".join(row[1:])))
    return items


def _extract_sdg_items_from_text(text: str) -> List[Dict[str, str]]:
    text = _clean(text)
    matches = list(re.finditer(r"\bSDG\s*0?\d+\b", text, re.IGNORECASE))
    items = []
    for idx, match in enumerate(matches):
        number = re.search(r"\d+", match.group(0)).group(0).lstrip("0") or "0"
        code = f"SDG {number}"
        next_start = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        fragment = _clean(text[match.end():next_start].lstrip(": -–,").rstrip(",; "))
        # Treat punctuation-only fragments as empty
        if fragment and not re.search(r"[a-zA-Z0-9]", fragment):
            fragment = ""
        known_title = _KNOWN_SDG_TITLES.get(number, "")
        known_desc = _KNOWN_SDG_DESCRIPTIONS.get(number, "")
        title = fragment or known_title or code
        description = fragment if (fragment and fragment.lower() != code.lower()) else known_desc
        items.append({"code": code, "title": title, "description": description})
    return items


def _extract_sdg_table_with_targets(rows: List[List[str]]) -> List[Dict[str, Any]]:
    """Extract SDG goals AND their detailed targets from template tables.

    Handles the common DOCX layout where SDG tables contain:
    - Goal header rows like "Goal 4: Quality Education"
    - Numbered target rows like "1. Free primary and secondary education"
    - Multi-column layouts where targets span across columns

    Returns items with full targets list for each SDG, giving the AI
    complete context from the actual template.
    """
    _GOAL_HEADER_RE = re.compile(
        r"\b(?:SDG|GOAL)\s*0?(\d+)\s*[:\-–—.]?\s*(.*)",
        re.IGNORECASE,
    )
    _NUMBERED_ITEM_RE = re.compile(r"^\s*(\d+)\s*[.):\-–]\s*(.+)", re.MULTILINE)

    items: List[Dict[str, Any]] = {}  # keyed by goal number
    current_goal_number: Optional[str] = None

    for row in rows:
        joined = " ".join(cell.strip() for cell in row if cell.strip())
        if not joined:
            continue

        # Check if this row is a goal header
        goal_match = _GOAL_HEADER_RE.search(joined)
        if goal_match:
            number = goal_match.group(1).lstrip("0") or "0"
            title_text = _clean(goal_match.group(2)) if goal_match.group(2) else ""
            code = f"SDG {number}"
            current_goal_number = number

            # If we haven't seen this goal yet, create it
            if number not in items:
                known_title = _KNOWN_SDG_TITLES.get(number, "")
                known_desc = _KNOWN_SDG_DESCRIPTIONS.get(number, "")
                items[number] = {
                    "code": code,
                    "title": title_text or known_title or code,
                    "description": known_desc or title_text or "",
                    "targets": [],
                }
            elif title_text and not items[number].get("title"):
                items[number]["title"] = title_text
            continue

        # Check for table title/header row (skip it)
        upper = joined.upper()
        if any(kw in upper for kw in [
            "SUSTAINABLE DEVELOPMENT",
            "SDG AND TARGETS",
            "GOALS AND TARGETS",
        ]) and not _GOAL_HEADER_RE.search(joined):
            continue

        # Try to extract numbered targets from this row's cells
        row_targets = []
        for cell in row:
            cell_text = cell.strip()
            if not cell_text:
                continue
            # Each cell may contain a numbered item like "1. Free primary..."
            num_match = _NUMBERED_ITEM_RE.match(cell_text)
            if num_match:
                target_num = num_match.group(1)
                target_text = _clean(num_match.group(2))
                if target_text:
                    row_targets.append(f"{target_num}. {target_text}")
            else:
                # Cell might contain text without numbering — capture if substantial
                cleaned = _clean(cell_text)
                if cleaned and len(cleaned) > 5 and not _looks_like_header(cleaned):
                    row_targets.append(cleaned)

        # Attach targets to the current goal, or try to match an SDG in the row
        if row_targets:
            if current_goal_number and current_goal_number in items:
                items[current_goal_number]["targets"].extend(row_targets)
            else:
                # Maybe this row has an SDG reference we can match
                sdg_in_row = re.search(r"\bSDG\s*0?(\d+)\b", joined, re.IGNORECASE)
                if sdg_in_row:
                    num = sdg_in_row.group(1).lstrip("0") or "0"
                    if num not in items:
                        code = f"SDG {num}"
                        items[num] = {
                            "code": code,
                            "title": _KNOWN_SDG_TITLES.get(num, code),
                            "description": _KNOWN_SDG_DESCRIPTIONS.get(num, ""),
                            "targets": [],
                        }
                    items[num]["targets"].extend(row_targets)

    # Build final result list, ordered by SDG number
    result = []
    for number in sorted(items.keys(), key=lambda x: int(x) if x.isdigit() else 999):
        item = items[number]
        # Build a comprehensive description from targets if we have them
        targets = item.get("targets", [])
        if targets:
            # Deduplicate targets
            seen = set()
            unique_targets = []
            for t in targets:
                t_normalized = re.sub(r"^\d+\.\s*", "", t).strip().lower()
                if t_normalized not in seen:
                    seen.add(t_normalized)
                    unique_targets.append(t)
            item["targets"] = unique_targets
            # Build rich description that includes both the goal description and targets
            base_desc = item.get("description", "")
            target_list = "; ".join(unique_targets)
            if base_desc:
                item["description"] = f"{base_desc} | Targets: {target_list}"
            else:
                item["description"] = f"Targets: {target_list}"
        result.append(item)

    # If no structured extraction happened, fall back to text-based extraction
    if not result:
        all_text = " ".join(" ".join(row) for row in rows)
        result = _extract_sdg_items_from_text(all_text)

    return result


def _extract_framework_table_with_details(
    rows: List[List[str]],
    code_re,
    fallback_prefix: Optional[str] = None,
    allow_title_only: bool = False,
) -> List[Dict[str, str]]:
    """Extract framework items with full detail from template tables.

    Like _extract_code_rows but also captures rows without codes as sub-items
    or detail rows belonging to the previous code item, giving the AI
    complete content from the actual template. Falls back to known descriptions
    only when the template itself doesn't provide them.
    """
    items: List[Dict[str, str]] = []
    _NUMBERED_RE = re.compile(r"^\s*\d+\s*[.):\-–]")

    for row in rows:
        joined = " ".join(row)
        if _looks_like_header(joined):
            continue

        code = _find_code(row, code_re)
        if not code and fallback_prefix:
            code = _find_prefixed_number(row, fallback_prefix)
        title, description = _title_description(row, code)

        if code or (allow_title_only and title):
            # Look up known description as fallback only
            final_desc = description
            label = code or title
            if not final_desc or final_desc == label or final_desc == title:
                known = _lookup_known_description(label)
                if known and known != label:
                    final_desc = known
            items.append({
                "code": code or title,
                "title": title or code or "",
                "description": final_desc,
            })
        elif items and not code:
            # Non-code row — might be detail/sub-items for the previous item
            # Capture numbered items or substantive text
            row_text_parts = []
            for cell in row:
                cell_text = cell.strip()
                if cell_text and len(cell_text) > 3:
                    row_text_parts.append(_clean(cell_text))
            if row_text_parts:
                detail_text = "; ".join(row_text_parts)
                prev = items[-1]
                if prev["description"] and prev["description"] != prev["code"]:
                    prev["description"] = f"{prev['description']}; {detail_text}"
                else:
                    prev["description"] = detail_text

    return items



    for key in ["sdg_context", "graduate_attributes", "core_values", "pqf_alignment", "aqrf_alignment"]:
        values = extracted.get(key) or []
        if values:
            context[key].extend(values)


def _extract_code_rows(rows: List[List[str]], code_re, fallback_prefix: Optional[str] = None, allow_title_only: bool = False) -> List[Dict[str, str]]:
    items = []
    for row in rows:
        joined = " ".join(row)
        if _looks_like_header(joined):
            continue
        code = _find_code(row, code_re)
        if not code and fallback_prefix:
            code = _find_prefixed_number(row, fallback_prefix)
        title, description = _title_description(row, code)
        if code or (allow_title_only and title):
            items.append({
                "code": code or title,
                "title": title or code or "",
                "description": description,
            })
    return items


def _extract_references_from_rows(rows: List[List[str]]) -> Dict[str, List[str]]:
    refs = {"books": [], "journals": [], "websites": [], "clms": [], "other": []}
    current = "other"
    for row in rows:
        text = _clean(" ".join(row))
        if not text:
            continue
        current = _reference_bucket(text, current)
        if not _REFERENCE_RE.fullmatch(text.upper()):
            refs[current].append(text)
    return refs


def _extract_reference_paragraphs(doc: _Document, context: Dict[str, Any]) -> None:
    current = None
    for para in doc.paragraphs:
        text = _clean(para.text)
        if not text:
            continue
        _extract_inline_institutional_context(text, context)
        bucket = _reference_bucket(text, current)
        if bucket != current and _REFERENCE_RE.search(text):
            current = bucket
            continue
        if current and (_looks_like_reference(text) or current != "other"):
            context["references"].setdefault(current, []).append(text)

    _extract_paragraph_frameworks(doc, context)


def _merge_context(context: Dict[str, Any], kind: str, extracted: Any) -> None:
    if kind == "program_outcomes":
        context["program_outcomes"].append(extracted)
    elif kind == "references":
        for key, values in extracted.items():
            context["references"].setdefault(key, []).extend(values)
    else:
        context[kind].extend(extracted)


def _merge_alignment_context(context: Dict[str, Any], extracted: Dict[str, Any]) -> None:
    if not extracted:
        return
    for key in ["sdg_context", "graduate_attributes", "core_values", "pqf_alignment", "aqrf_alignment"]:
        values = extracted.get(key) or []
        if values:
            context[key].extend(values)


def _has_extracted_values(kind: str, extracted: Any) -> bool:
    if kind == "program_outcomes":
        return bool(extracted.get("items"))
    if kind == "references":
        return any(extracted.values())
    return bool(extracted)


def _confidence_for(kind: str, rows: List[List[str]], table_text: str) -> float:
    if kind == "program_outcomes" and len(_extract_code_rows(rows, _PLO_RE)) >= 3:
        return 0.95
    if kind in {"sdg_context", "pqf_alignment", "aqrf_alignment"}:
        return 0.85
    return 0.75


def _lookup_known_description(code_or_title: str) -> str:
    """Look up a known canonical description for a framework code/title.

    Checks Core Values, Graduate Attributes, PQF, AQRF, and SDG dictionaries.
    Returns the canonical description if found, otherwise returns the input itself.
    """
    if not code_or_title:
        return ""
    clean = code_or_title.strip()
    upper = clean.upper()

    # Core Values
    if upper in _KNOWN_CORE_VALUE_DESCRIPTIONS:
        return _KNOWN_CORE_VALUE_DESCRIPTIONS[upper]

    # Graduate Attributes (case-insensitive title match)
    for key, desc in _KNOWN_GRADUATE_ATTRIBUTE_DESCRIPTIONS.items():
        if key.lower() == clean.lower():
            return desc

    # PQF
    if upper in _KNOWN_PQF_LEVEL_6_DESCRIPTIONS:
        return _KNOWN_PQF_LEVEL_6_DESCRIPTIONS[upper]
    # Normalize PQF codes (e.g., "PQF1" -> "PQF 1")
    pqf_match = re.match(r"PQF\s*(\d+)", upper)
    if pqf_match:
        normalized = f"PQF {pqf_match.group(1)}"
        if normalized in _KNOWN_PQF_LEVEL_6_DESCRIPTIONS:
            return _KNOWN_PQF_LEVEL_6_DESCRIPTIONS[normalized]

    # AQRF
    if upper in _KNOWN_AQRF_LEVEL_6_DESCRIPTIONS:
        return _KNOWN_AQRF_LEVEL_6_DESCRIPTIONS[upper]
    aqrf_match = re.match(r"AQRF\s*(\d+)", upper)
    if aqrf_match:
        normalized = f"AQRF {aqrf_match.group(1)}"
        if normalized in _KNOWN_AQRF_LEVEL_6_DESCRIPTIONS:
            return _KNOWN_AQRF_LEVEL_6_DESCRIPTIONS[normalized]

    # SDG
    sdg_match = re.match(r"SDG\s*(\d+)", upper)
    if sdg_match:
        number = sdg_match.group(1).lstrip("0") or "0"
        if number in _KNOWN_SDG_DESCRIPTIONS:
            return _KNOWN_SDG_DESCRIPTIONS[number]

    return clean


def _enrich_with_known_descriptions(context: Dict[str, Any]) -> None:
    """Post-processing pass: fill in missing descriptions from known references.

    When extraction only captured codes/titles (description == code), this
    replaces the self-referencing description with the canonical one.
    """
    for key in ["institutional_outcomes", "sdg_context", "graduate_attributes", "core_values", "pqf_alignment", "aqrf_alignment"]:
        items = context.get(key) or []
        for item in items:
            label = _item_label(item)
            desc = str(item.get("description") or "").strip()
            # If description is missing, same as code, or same as title, enrich it
            if not desc or desc == label or desc == item.get("title", ""):
                enriched = _lookup_known_description(label)
                if enriched and enriched != label:
                    item["description"] = enriched


def _dedupe_context(context: Dict[str, Any]) -> None:
    for key in ["institutional_outcomes", "sdg_context", "graduate_attributes", "core_values", "pqf_alignment", "aqrf_alignment"]:
        context[key] = _dedupe_items(context.get(key) or [])
    context["program_outcomes"] = _merge_program_outcome_groups(context.get("program_outcomes") or [])
    for group in context.get("program_outcomes") or []:
        group["items"] = _dedupe_items(group.get("items") or [])
    refs = context.get("references") or {}
    for key, values in refs.items():
        refs[key] = _unique(values)
    _enrich_with_known_descriptions(context)


def _merge_program_outcome_groups(groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        raw_code = str(group.get("program_code") or "").strip()
        raw_name = str(group.get("program_name") or "").strip()
        code = raw_code.upper() or raw_name.lower()
        items = group.get("items") if isinstance(group.get("items"), list) else []
        if not code or not items:
            continue
        if code not in merged:
            merged[code] = {
                "program_code": raw_code,
                "program_name": raw_name or raw_code,
                "items": [],
            }
            order.append(code)
        target = merged[code]
        name = raw_name
        if name and (target.get("program_name") == code or len(name) < len(str(target.get("program_name") or ""))):
            target["program_name"] = name
        target["items"].extend(items)
    return [merged[code] for code in order]


def _summarize_warnings(context: Dict[str, Any]) -> None:
    counts = context_counts(context)
    if not counts["program_outcomes"]:
        context["warnings"].append("No PLO/PO table was detected in the template.")
    for key in ["sdg_context", "graduate_attributes", "core_values", "pqf_alignment", "aqrf_alignment"]:
        if not counts[key]:
            context["warnings"].append(f"No {key.replace('_', ' ')} context was detected in the template.")


def _is_metadata_table(rows: List[List[str]]) -> bool:
    labels = " ".join((row[0] if row else "") for row in rows[:12]).upper()
    hits = sum(1 for marker in ["COURSE CODE", "COURSE TITLE", "COURSE DESCRIPTION", "TARGET SDG", "CONTACT HOURS", "ROOM ASSIGNMENT"] if marker in labels)
    return hits >= 2


def _is_clo_alignment_table(rows: List[List[str]]) -> bool:
    text = "\n".join(" | ".join(row) for row in rows[:4]).upper()
    return ("COURSE LEARNING OUTCOMES" in text or "CLO" in text) and "ALIGNED PROGRAM" in text


def _is_weekly_outline_table(rows: List[List[str]]) -> bool:
    text = "\n".join(" | ".join(row) for row in rows[:6]).upper()
    return "TIME FRAME" in text and "TEACHING-LEARNING" in text and "LEARNING RESOURCES" in text


def _extract_paragraph_frameworks(doc: _Document, context: Dict[str, Any]) -> None:
    current = None
    for para in doc.paragraphs:
        text = _clean(para.text)
        upper = text.upper()
        if not text:
            continue
        if upper == "VISION":
            current = "vision"
            continue
        if upper == "MISSION":
            current = "mission"
            continue
        if upper == "CORE VALUES":
            current = "core_values"
            continue
        if upper == "CORE COMPETENCIES":
            current = "core_competencies"
            continue
        if upper == "INSTITUTIONAL OBJECTIVES":
            current = "institutional_objectives"
            continue
        if upper == "INSTITUTIONAL OUTCOMES":
            current = "institutional_outcomes"
            continue
        if "SALETTINIAN GRADUATE ATTRIBUTES" in upper:
            current = "graduate_attributes"
            continue
        if upper.startswith("PQF") or "PHILIPPINE QUALIFICATIONS FRAMEWORK" in upper:
            current = "pqf_alignment"
            continue
        if upper.startswith("ASEAN QUALIFICATIONS REFERENCE FRAMEWORK") or upper.startswith("AQRF LEVEL"):
            current = "aqrf_alignment"
            continue

        if current == "vision":
            if _is_major_context_heading(upper):
                current = None
                continue
            _append_institutional_text(context, "vision", text)
        elif current == "mission":
            if _is_major_context_heading(upper):
                current = None
                continue
            _append_institutional_text(context, "mission", text)
        elif current == "core_values":
            if upper.startswith("CORE COMPETENCIES"):
                current = None
                continue
            item = _parse_named_description(text)
            if item:
                context["core_values"].append(item)
        elif current == "core_competencies":
            if _is_major_context_heading(upper):
                current = None
                continue
            _append_institutional_item(context, "core_competencies", text)
        elif current == "institutional_objectives":
            if _is_major_context_heading(upper):
                current = None
                continue
            if not upper.startswith("IN KEEPING WITH"):
                _append_institutional_item(context, "institutional_objectives", text)
        elif current == "institutional_outcomes":
            if _is_major_context_heading(upper):
                current = None
                continue
            for item in _extract_institutional_outcomes([[text]]):
                context["institutional_outcomes"].append(item)
        elif current == "graduate_attributes":
            if (
                upper.startswith("IT PROGRAM")
                or upper.startswith("PROGRAM OUTCOMES")
                or upper.startswith("PROGRAM LEARNING OUTCOMES")
                or upper.startswith("THE MINIMUM STANDARDS FOR THE BACHELOR")
                or upper.startswith("THE MINIMUM STANDARD FOR THE BACHELOR")
                or upper.startswith("BACHELOR OF")
                or upper.startswith("COURSE INFORMATION")
                or upper.startswith("COURSE LEARNING OUTCOMES")
                or upper.startswith("COURSE REQUIREMENTS")
                or upper.startswith("ASSESSMENT")
                or upper.startswith("REFERENCES")
                or upper.startswith("CONSULTATION")
                or upper.startswith("AN IT GRADUATE")
                or "CHED CMD" in upper
                # Stop when the paragraph starts with a PO code sequence (e.g. BPED 1 –, BSEDENG 1 –)
                or bool(_PROGRAM_OUTCOME_CODE_RE.match(upper[:20]))
            ):
                current = None
                continue
            item = _parse_named_description(text)
            if item:
                context["graduate_attributes"].append(item)
        elif current == "pqf_alignment":
            item = _parse_code_description(text, re.compile(r"\bPQF\s*0?\d+\b", re.IGNORECASE))
            if item:
                context["pqf_alignment"].append(item)
        elif current == "aqrf_alignment":
            item = _parse_code_description(text, re.compile(r"\bAQRF\s*0?\d+\b", re.IGNORECASE))
            if item:
                context["aqrf_alignment"].append(item)


def _is_institutional_context_text(text: str) -> bool:
    upper = _clean(text).upper()
    return upper.startswith("INSTITUTIONAL OBJECTIVES") or upper.startswith("INSTITUTIONAL OUTCOMES") or (
        upper.startswith("VISION")
        and ("MISSION" in upper or "CORE VALUES" in upper or "CORE COMPETENCIES" in upper or "INSTITUTIONAL OUTCOMES" in upper)
    )


def _extract_inline_institutional_context(text: str, context: Dict[str, Any]) -> None:
    """Parse institutional blocks when headings and values live in one table cell."""
    clean = _clean(text)
    if not _is_institutional_context_text(clean):
        return

    headings = [
        "VISION",
        "MISSION",
        "INSTITUTIONAL OUTCOMES",
        "CORE VALUES",
        "CORE COMPETENCIES",
        "INSTITUTIONAL OBJECTIVES",
    ]
    pattern = re.compile(r"\b(" + "|".join(re.escape(item) for item in headings) + r")\b")
    matches = list(pattern.finditer(clean))
    for index, match in enumerate(matches):
        heading = match.group(1).upper()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(clean)
        body = _clean(clean[start:end].strip(" :-–—"))
        if not body:
            continue
        if heading == "VISION":
            _append_institutional_text(context, "vision", body)
        elif heading == "MISSION":
            _append_institutional_text(context, "mission", body)
        elif heading == "INSTITUTIONAL OUTCOMES":
            body = _trim_to_institutional_outcomes_block(body)
            for item in _extract_named_outcome_block(body, _KNOWN_GRADUATE_ATTRIBUTES) or _extract_institutional_outcomes([[body]]):
                context["institutional_outcomes"].append(item)
        elif heading == "CORE VALUES":
            for item in _parse_core_values_block(body):
                context["core_values"].append(item)
        elif heading == "CORE COMPETENCIES":
            for item in _split_institutional_items(body):
                _append_institutional_item(context, "core_competencies", item)
        elif heading == "INSTITUTIONAL OBJECTIVES":
            for item in _split_institutional_items(body):
                if not item.upper().startswith("IN KEEPING WITH"):
                    _append_institutional_item(context, "institutional_objectives", item)


def _parse_core_values_block(text: str) -> List[Dict[str, str]]:
    clean = _clean(text)
    value_pattern = re.compile(
        r"\b(" + "|".join(re.escape(item) for item in _KNOWN_CORE_VALUES) + r")\b\s*[-–—:]?",
        re.IGNORECASE,
    )
    matches = list(value_pattern.finditer(clean))
    items: List[Dict[str, str]] = []
    for index, match in enumerate(matches):
        title = match.group(1).upper()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(clean)
        description = _clean(clean[start:end].strip(" :-–—"))
        items.append({
            "code": title,
            "title": title.title(),
            "description": description or _lookup_known_description(title),
        })
    return items


def _split_institutional_items(text: str) -> List[str]:
    clean = _clean(text)
    if not clean:
        return []
    parts = re.split(r"(?=\bTo\s+[a-z])", clean)
    if len(parts) > 1:
        return [_clean(part) for part in parts if _clean(part)]

    known_competencies = [
        "Researched-Based Oriented Learning",
        "ICT-Integrated Learning",
        "Industry-Based Oriented Learning",
        "Oriented Toward Transformative Learning",
        "Oriented Toward Integrative Learning",
    ]
    found = [item for item in known_competencies if re.search(re.escape(item), clean, re.IGNORECASE)]
    return found or [clean]


def _parse_named_description(text: str) -> Optional[Dict[str, str]]:
    if _looks_like_header(text) or len(text) < 3:
        return None
    for sep in [". ", " - ", " – ", " —", "— ", " –", "– ", ": ", "\t", "-", "–", "—"]:
        if sep in text:
            left, right = text.split(sep, 1)
            left = _clean(left)
            right = _clean(right)
            if left and right and len(left) <= 80:
                return {"code": left, "title": left, "description": right}
    # If no separator found, check if the text matches a known framework item
    clean = _clean(text)
    known_desc = _lookup_known_description(clean)
    if known_desc and known_desc != clean:
        return {"code": clean, "title": clean, "description": known_desc}
    return None


def _is_major_context_heading(upper: str) -> bool:
    return upper in {
        "VISION",
        "MISSION",
        "INSTITUTIONAL OUTCOMES",
        "CORE VALUES",
        "CORE COMPETENCIES",
        "INSTITUTIONAL OBJECTIVES",
        "SALETTINIAN GRADUATE ATTRIBUTES (SGA)",
        "SALETTINIAN GRADUATE ATTRIBUTES",
        "PROGRAM OUTCOMES",
    } or upper.startswith((
        "PROGRAM LEARNING OUTCOMES",
        "PROGRAM OUTCOMES",
        "COURSE INFORMATION",
        "COURSE LEARNING OUTCOMES",
        "COURSE REQUIREMENTS",
        "ASSESSMENT",
        "REFERENCES",
        "CONSULTATION",
        "THE MINIMUM STANDARDS FOR THE BACHELOR",
        "THE MINIMUM STANDARD FOR THE BACHELOR",
    ))


def _append_institutional_text(context: Dict[str, Any], key: str, text: str) -> None:
    inst = context.setdefault("institutional_context", {})
    current = _clean(inst.get(key, ""))
    inst[key] = _clean(f"{current} {text}") if current else text


def _append_institutional_item(context: Dict[str, Any], key: str, text: str) -> None:
    clean = _strip_list_marker(text)
    if not clean:
        return
    inst = context.setdefault("institutional_context", {})
    inst.setdefault(key, []).append(clean)


def _strip_list_marker(text: str) -> str:
    return _clean(re.sub(r"^\s*(?:\d+[\.)]|[a-zA-Z][\.)]|[•·●\-])\s*", "", text or ""))


def _institutional_context_count(inst: Dict[str, Any]) -> int:
    if not isinstance(inst, dict):
        return 0
    count = 0
    if inst.get("vision"):
        count += 1
    if inst.get("mission"):
        count += 1
    count += len(inst.get("core_competencies") or [])
    count += len(inst.get("institutional_objectives") or [])
    return count


def _parse_code_description(text: str, code_re) -> Optional[Dict[str, str]]:
    match = code_re.search(text)
    if not match:
        return None
    code = _clean(match.group(0)).upper()
    description = _clean(text[match.end():].lstrip(": -–"))
    return {"code": code, "title": code, "description": description}


def _item_label(item: Dict[str, Any]) -> str:
    return str(item.get("code") or item.get("title") or "").strip()


def _dedupe_items(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = {}
    output = []
    for item in items:
        label = _item_label(item).lower()
        if not label:
            continue
        if label in seen:
            existing_idx = seen[label]
            if _item_detail_score(item) > _item_detail_score(output[existing_idx]):
                output[existing_idx] = item
            continue
        seen[label] = len(output)
        output.append(item)
    return output


def _item_detail_score(item: Dict[str, str]) -> int:
    label = _item_label(item)
    desc = str(item.get("description") or "").strip()
    title = str(item.get("title") or "").strip()
    score = len(desc)
    if desc and desc.lower() != label.lower():
        score += 1000
    if title and title.lower() != label.lower():
        score += 100
    return score


def _unique(values: List[str]) -> List[str]:
    seen = set()
    output = []
    for value in values or []:
        clean = _clean(value)
        key = clean.lower()
        if clean and key not in seen:
            seen.add(key)
            output.append(clean)
    return output


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def _looks_like_header(text: str) -> bool:
    text = _clean(text)
    if not text:
        return True
    upper = text.upper()
    header_words = ["CODE", "DESCRIPTION", "OUTCOME", "OUTCOMES", "ATTRIBUTE", "ATTRIBUTES", "VALUE", "VALUES", "TITLE"]
    if upper == text and any(word in upper for word in header_words):
        return True
    tokens = re.findall(r"[A-Z]+", upper)
    return bool(tokens) and len(tokens) <= 6 and all(token in header_words or token in {"CORE", "PROGRAM", "GRADUATE"} for token in tokens)


def _find_code(row: List[str], code_re) -> str:
    for cell in row:
        match = code_re.search(cell)
        if match:
            return _clean(match.group(0)).upper().replace("  ", " ")
    return ""


def _find_prefixed_number(row: List[str], prefix: str) -> str:
    pattern = re.compile(rf"\b{re.escape(prefix)}\s*0?\d+\b", re.IGNORECASE)
    return _find_code(row, pattern)


def _title_description(row: List[str], code: str) -> tuple[str, str]:
    cleaned = [_clean(cell) for cell in row if _clean(cell)]
    if code:
        no_code = [cell for cell in cleaned if code.lower() not in cell.lower()]
    else:
        no_code = cleaned
    if not no_code:
        return "", ""
    title = no_code[0]
    # Concatenate all non-code cells beyond the first as the description
    # to capture multi-cell content (e.g., | PQF 1 | Knowledge | Demonstrate understanding of... |)
    if len(no_code) > 1:
        description = " — ".join(no_code) if len(no_code) <= 3 else " ".join(no_code[1:])
    else:
        description = title
    return title, description


def _infer_program_code(rows: List[List[str]]) -> str:
    text = " ".join(" ".join(row) for row in rows[:3])
    code_match = _PROGRAM_OUTCOME_CODE_RE.search(text)
    if code_match:
        code = re.sub(r"\s*0*\d+\b", "", code_match.group(0).upper()).strip()
        if code and code not in {"PLO", "PO", "PL", "CLO", "SDG", "PQF", "AQRF", "CV"}:
            return code
    match = re.search(r"\bBS[A-Z]{2,5}\b|\b[A-Z]{2,6}\b(?=\s+PROGRAM OUTCOMES?)", text, re.IGNORECASE)
    if match and match.group(0).upper() not in {"CODE", "PROGRAM"}:
        return match.group(0).upper()
    return ""


def _infer_program_name(rows: List[List[str]]) -> str:
    text = " ".join(" ".join(row) for row in rows).lower()
    signals = [
        ("Nursing", ("nursing", "health sciences", "delivery of care", "nursing theories")),
        ("Accountancy", ("accounting", "taxation", "audit", "financial accounting", "accounting information")),
        ("Marketing", ("marketing", "consumer", "market", "sales", "organizational contexts")),
        ("Architecture", ("architecture", "architectural", "built environment", "design and construction")),
        ("Information Technology", ("computing", "information technology", "software", "computer")),
    ]
    for label, keywords in signals:
        if any(keyword in text for keyword in keywords):
            return label
    program_code = _infer_program_code(rows)
    code_names = {
        "BPED": "Physical Education",
        "BSEDENG": "English Education",
    }
    if program_code in code_names:
        return code_names[program_code]
    for row in rows[:3]:
        for cell in row:
            if re.search(r"PROGRAM OUTCOMES?|BACHELOR|SCIENCE|ARTS|NURSING|BUSINESS|TECHNOLOGY", cell, re.IGNORECASE):
                cleaned = re.sub(r"\bCODE\b|\bPROGRAM OUTCOMES?\b", "", cell, flags=re.IGNORECASE)
                cleaned = _clean(cleaned)
                if (
                    cleaned
                    and len(cleaned) <= 80
                    and not re.fullmatch(r"(?:[A-Z]\s*){2,12}", cleaned)
                ):
                    return cleaned
    return ""


def _reference_bucket(text: str, current: Optional[str]) -> str:
    upper = text.upper()
    if "BOOK" in upper or "TEXTBOOK" in upper:
        return "books"
    if "JOURNAL" in upper:
        return "journals"
    if "WEBSITE" in upper or "URL" in upper or "HTTP" in upper:
        return "websites"
    if "CLMS" in upper:
        return "clms"
    return current or "other"


def _looks_like_reference(text: str) -> bool:
    return bool(re.search(r"https?://|www\.|\.com|\.edu|\.org|\(\d{4}\)|\b\d{4}\b", text, re.IGNORECASE))
