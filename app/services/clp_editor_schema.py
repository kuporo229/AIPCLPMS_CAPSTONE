from copy import deepcopy
from datetime import datetime, timezone


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def default_tiptap_doc(title="Course Learning Plan"):
    return {
        "type": "doc",
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 1},
                "content": [{"type": "text", "text": title or "Course Learning Plan"}],
            },
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Start writing your CLP here."}],
            },
        ],
    }


def default_semantic_clp(title="", department="", owner_user_id=None, plan_id=None):
    now = _now_iso()
    tiptap_doc = default_tiptap_doc(title or "Course Learning Plan")
    return {
        "schema": {
            "name": "semantic_clp",
            "version": "2.0.0",
            "content_language": "en",
            "created_at": now,
            "updated_at": now,
            "compatibility": {
                "legacy_flat_keys_supported": True,
                "beta_copilot_supported": True,
                "template_version": "",
            },
        },
        "identity": {
            "plan_id": plan_id,
            "department_id": None,
            "department_name": department,
            "owner_user_id": str(owner_user_id or ""),
            "status": "draft",
            "workflow_type": "semantic_clp",
            "review_stage": "editing",
            "academic_year": "",
            "semester_label": "",
        },
        "course": {
            "course_code": "",
            "course_title": title,
            "descriptive_title": "",
            "course_description": "",
            "type_of_course": "",
            "credit": {
                "units": "",
                "lecture_hours_per_week": "",
                "lab_hours_per_week": "",
                "contact_hours_per_week": "",
                "display": "",
            },
            "requirements": {
                "pre_requisites": [],
                "co_requisites": [],
                "course_requirements": [],
            },
            "delivery": {
                "class_schedule": "",
                "room_assignment": "",
                "modality": "",
                "lms_platform": "CLMS",
                "consultation_hours": [],
            },
            "service_learning": {
                "component": "",
                "community_partner": "",
                "target_sdgs": [],
            },
        },
        "institutional_context": {
            "vision": {
                "section_id": "vision",
                "title": "Vision",
                "blocks": [],
            },
            "mission": {
                "section_id": "mission",
                "title": "Mission",
                "blocks": [],
            },
            "core_values": {
                "section_id": "core_values",
                "title": "Core Values",
                "values": [],
            },
            "graduate_attributes": [],
        },
        "outcomes": {
            "clos": [],
            "plos": [],
            "institutional_outcomes": [],
            "sga": [],
            "sdgs": [],
            "pqf": [],
            "aqrf": [],
        },
        "alignment": {
            "clo_to_plo": [],
            "plo_to_institutional_outcome": [],
            "clo_to_sga": [],
            "clo_to_core_value": [],
            "clo_to_sdg": [],
            "clo_to_pqf": [],
            "clo_to_aqrf": [],
        },
        "weekly_outline": {
            "calendar_model": {
                "total_weeks": 18,
                "export_grouping": [
                    {"group_id": "week_8_9", "week_numbers": [8, 9], "label": "Week 8 & 9"},
                    {"group_id": "week_10_11", "week_numbers": [10, 11], "label": "Week 10 & 11"},
                    {"group_id": "week_14_15", "week_numbers": [14, 15], "label": "Week 14 & 15"},
                    {"group_id": "week_16_17", "week_numbers": [16, 17], "label": "Week 16-17"},
                ],
            },
            "weeks": [],
        },
        "sections": [],
        "tables": [],
        "references": {
            "website": [],
            "textbook": [],
            "journal": [],
            "other": [],
            "deduplicated_export_order": [],
        },
        "signatures": [
            {
                "signature_id": "prepared_by",
                "role": "prepared_by",
                "name": "",
                "position": "",
                "user_id": str(owner_user_id or ""),
                "signature_url": "",
                "date": "",
                "status": "pending",
                "source": "user_profile|department_settings|manual",
            },
            {"signature_id": "reviewed_by", "role": "reviewed_by", "name": "", "position": "", "date": "", "status": "pending"},
            {"signature_id": "endorsed_by", "role": "endorsed_by", "name": "", "position": "", "date": "", "status": "pending"},
            {"signature_id": "approved_by", "role": "approved_by", "name": "", "position": "", "date": "", "status": "pending"},
        ],
        "editor_state": {
            "schema_version": "clp_tiptap_v1",
            "doc": tiptap_doc,
            "last_saved_selection": None,
        },
        "validation": {
            "errors": [],
            "warnings": [],
            "final_review": {
                "approved": False,
                "summary": "",
                "issues": [],
                "warnings": [],
                "signature_hash": "",
            },
        },
        "export": {
            "document": {
                "ready_for_template": False,
                "document_inserted": False,
                "filename": "",
                "storage_bucket": "",
                "template_id": None,
                "template_filename": "",
                "template_source_type": "",
                "generated_at": "",
                "generated_by": "",
            },
            "placeholder_map": {},
            "html_render_hints": {},
            "analytics_projection": {
                "clp_mapping_entries_last_shredded_at": "",
                "supported_source_types": ["CO_PO", "PO_IO", "WLO_CO", "ASSESSMENT_CO", "CO_EXTERNAL"],
            },
        },
        "provenance": {
            "sources": [],
            "change_log": [],
        },
    }


def normalize_semantic_clp(value, title="", department="", owner_user_id=None, plan_id=None):
    base = default_semantic_clp(title=title, department=department, owner_user_id=owner_user_id, plan_id=plan_id)
    if not isinstance(value, dict):
        return base

    if value.get("schema", {}).get("name") != "semantic_clp":
        legacy_tiptap = value.get("editor_state", {}).get("tiptap_json")
        if isinstance(legacy_tiptap, dict):
            base["editor_state"]["doc"] = legacy_tiptap
        base["course"]["course_code"] = value.get("course", {}).get("course_code", "")
        base["course"]["course_title"] = value.get("course", {}).get("course_title") or title
        base["course"]["course_description"] = value.get("course", {}).get("course_description", "")
        base["course"]["credit"]["units"] = value.get("course", {}).get("units", "")
        base["identity"]["department_name"] = value.get("course", {}).get("department") or department
        base["institutional_context"]["vision"]["blocks"] = _string_to_blocks(value.get("institutional_context", {}).get("vision", ""))
        base["institutional_context"]["mission"]["blocks"] = _string_to_blocks(value.get("institutional_context", {}).get("mission", ""))
        base["institutional_context"]["core_values"]["values"] = [
            {"code": "", "label": item, "description": "", "source": "manual"}
            for item in value.get("institutional_context", {}).get("core_values", [])
        ]
        base["outcomes"]["clos"] = [
            {"clo_id": item.get("code", ""), "code": item.get("code", ""), "category": "course_learning_outcome", "domain": "", "statement": item.get("statement", ""), "level": "", "measurable_verb": "", "teacher_locked": False, "review_status": "draft", "provenance": []}
            for item in value.get("outcomes", {}).get("clos", [])
            if isinstance(item, dict)
        ]
        base["outcomes"]["plos"] = [
            {"plo_id": item.get("code", ""), "code": item.get("code", ""), "label": "", "description": item.get("statement", ""), "department_id": None, "source": "manual"}
            for item in value.get("outcomes", {}).get("plos", [])
            if isinstance(item, dict)
        ]
        return base

    normalized = deepcopy(value)
    normalized.setdefault("schema", base["schema"])
    normalized.setdefault("identity", base["identity"])
    normalized.setdefault("course", base["course"])
    normalized.setdefault("institutional_context", base["institutional_context"])
    normalized.setdefault("outcomes", base["outcomes"])
    normalized.setdefault("alignment", base["alignment"])
    normalized.setdefault("weekly_outline", base["weekly_outline"])
    normalized.setdefault("sections", [])
    normalized.setdefault("tables", [])
    normalized.setdefault("references", base["references"])
    normalized.setdefault("signatures", base["signatures"])
    normalized.setdefault("editor_state", base["editor_state"])
    normalized.setdefault("validation", base["validation"])
    normalized.setdefault("export", base["export"])
    normalized.setdefault("provenance", base["provenance"])
    normalized["schema"]["updated_at"] = _now_iso()
    normalized["course"]["course_title"] = normalized["course"].get("course_title") or title
    normalized["identity"]["department_name"] = normalized["identity"].get("department_name") or department
    normalized["identity"]["owner_user_id"] = normalized["identity"].get("owner_user_id") or str(owner_user_id or "")
    normalized["identity"]["plan_id"] = normalized["identity"].get("plan_id") or plan_id
    normalized["editor_state"].setdefault("schema_version", "clp_tiptap_v1")
    normalized["editor_state"].setdefault("doc", default_tiptap_doc(title))
    return normalized


def get_tiptap_projection(semantic_clp):
    doc = (semantic_clp or {}).get("editor_state", {}).get("doc")
    if isinstance(doc, dict) and doc.get("type") == "doc":
        return doc
    return default_tiptap_doc((semantic_clp or {}).get("course", {}).get("course_title", "Course Learning Plan"))


def set_tiptap_projection(semantic_clp, tiptap_doc):
    updated = deepcopy(semantic_clp)
    updated.setdefault("editor_state", {})["schema_version"] = "clp_tiptap_v1"
    updated["editor_state"]["doc"] = tiptap_doc
    updated["schema"]["updated_at"] = _now_iso()
    return updated


def semantic_to_tiptap_doc(semantic_clp):
    title = (semantic_clp or {}).get("course", {}).get("course_title") or "Course Learning Plan"
    content = [
        {
            "type": "heading",
            "attrs": {"level": 1},
            "content": [{"type": "text", "text": title}],
        }
    ]

    description = (semantic_clp or {}).get("course", {}).get("course_description")
    if description:
        content.extend(_paragraph_nodes("Course Description", description))

    for key in ("vision", "mission"):
        section = (semantic_clp or {}).get("institutional_context", {}).get(key, {})
        blocks = section.get("blocks") or []
        content.append({"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": section.get("title") or key.title()}]})
        for block in blocks:
            content.extend(_block_to_tiptap_nodes(block))

    sections = (semantic_clp or {}).get("sections") or []
    for section in sections:
        title_text = section.get("title") or "Section"
        content.append({"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": title_text}]})
        for block in section.get("blocks") or []:
            content.extend(_block_to_tiptap_nodes(block))

    return {"type": "doc", "content": content}


def semantic_summary_for_ai(semantic_clp):
    return {
        "course": (semantic_clp or {}).get("course", {}),
        "clos": (semantic_clp or {}).get("outcomes", {}).get("clos", []),
        "plos": (semantic_clp or {}).get("outcomes", {}).get("plos", []),
        "alignment": (semantic_clp or {}).get("alignment", {}),
        "weekly_outline": (semantic_clp or {}).get("weekly_outline", {}),
    }


def apply_alignment_rows(semantic_clp, rows):
    updated = deepcopy(semantic_clp)
    existing = updated.setdefault("alignment", {}).setdefault("clo_to_plo", [])
    by_pair = {
        (item.get("source_clo_id"), item.get("target_plo_id")): item
        for item in existing
        if isinstance(item, dict)
    }
    for row in rows or []:
        source = row.get("source_clo_id") or row.get("clo_code") or row.get("clo_id")
        target = row.get("target_plo_id") or row.get("plo_code") or row.get("plo_id")
        if not source or not target:
            continue
        item = by_pair.get((source, target))
        if not item:
            item = {
                "source_clo_id": source,
                "target_plo_id": target,
                "mapping_value": "",
                "weight": 1,
                "rationale": "",
                "evidence_refs": [],
            }
            existing.append(item)
            by_pair[(source, target)] = item
        item["mapping_value"] = row.get("mapping_value") or row.get("value") or ""
        item["rationale"] = row.get("rationale", item.get("rationale", ""))
    updated["schema"]["updated_at"] = _now_iso()
    return updated


def apply_generated_section(semantic_clp, section_key, content):
    updated = deepcopy(semantic_clp)
    if section_key == "weekly_outline":
        weekly = updated.setdefault("weekly_outline", {})
        if isinstance(content, dict) and isinstance(content.get("weeks"), list):
            weekly["weeks"] = content["weeks"]
        elif isinstance(content, list):
            weekly["weeks"] = content
    else:
        sections = updated.setdefault("sections", [])
        section = {
            "section_id": section_key,
            "section_type": "freeform",
            "title": section_key.replace("_", " ").title(),
            "order": len(sections) + 1,
            "department_scope": {"department_id": None, "required": False, "template_placeholder": ""},
            "blocks": _content_to_blocks(content),
            "provenance": [],
        }
        sections.append(section)
    updated["schema"]["updated_at"] = _now_iso()
    return updated


def _string_to_blocks(value):
    if not value:
        return []
    return [{"block_id": "", "block_type": "paragraph", "content": str(value), "items": [], "table_ref": "", "metadata": {}}]


def _paragraph_nodes(title, text):
    return [
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": title}]},
        {"type": "paragraph", "content": [{"type": "text", "text": str(text)}]},
    ]


def _block_to_tiptap_nodes(block):
    if not isinstance(block, dict):
        return []
    block_type = block.get("block_type") or block.get("type")
    if block_type == "heading":
        return [{"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": str(block.get("content", ""))}]}]
    if block_type == "list":
        items = block.get("items") or []
        return [{
            "type": "bulletList",
            "content": [
                {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": str(item)}]}]}
                for item in items
            ],
        }]
    if block_type == "table":
        rows = block.get("rows") or []
        return [_table_rows_to_tiptap(rows)] if rows else []
    content = str(block.get("content", "") or "")
    return [{"type": "paragraph", "content": [{"type": "text", "text": content}]}] if content else []


def _table_rows_to_tiptap(rows):
    return {
        "type": "table",
        "content": [
            {
                "type": "tableRow",
                "content": [
                    {"type": "tableCell", "content": [{"type": "paragraph", "content": [{"type": "text", "text": str(cell)}]}]}
                    for cell in row
                ],
            }
            for row in rows
        ],
    }


def _content_to_blocks(content):
    if isinstance(content, dict):
        if isinstance(content.get("blocks"), list):
            return content["blocks"]
        return _string_to_blocks(content)
    if isinstance(content, list):
        return [{"block_id": "", "block_type": "paragraph", "content": str(item), "items": [], "table_ref": "", "metadata": {}} for item in content]
    return _string_to_blocks(content)
