import json
import logging
import time
from io import BytesIO

from docx import Document
from flask import current_app
from werkzeug.datastructures import MultiDict
from werkzeug.utils import secure_filename

logger = logging.getLogger(__name__)

from app import STORAGE_BUCKET_NAME, supabase
from app.compat_supabase import read_storage_bytes, write_storage_bytes
from app.services.copilot_beta_service import (
    beta_ai_call_context,
    build_beta_docx_replacements,
    ensure_beta_shape,
    generate_beta_alignment,
    generate_beta_clos,
    generate_beta_weekly,
    generate_single_clo_with_alignment,
    generate_single_week_row,
    get_cached_beta_final_review,
    get_beta_final_review_signature,
    run_beta_final_fix,
    run_beta_final_review,
    strip_runtime_copilot_cache,
    update_beta_content_from_form,
    validate_beta_content,
    _build_template_profile_hints,
)
from app.services.template_generation_spec import build_generation_spec, merge_spec_into_profile
from app.services.docx_service import has_docx_placeholders, replace_placeholders
from app.utils import (
    cleanup_storage_after_commit,
    ensure_plan_document_file,
    get_department_id_by_name,
    log_system_event,
    storage_file_exists,
)


def _strip_all_numbering(doc):
    """Remove ``w:numPr`` from every paragraph in *doc* (body, headers,
    footers, tables) so that no list-marker (especially Wingdings
    checkmark bullets like ``\\uf0fc``) is rendered by OnlyOffice."""
    from docx.oxml.ns import qn

    def _strip(paragraph):
        pPr = paragraph._element.find(qn('w:pPr'))
        if pPr is not None:
            numPr = pPr.find(qn('w:numPr'))
            if numPr is not None:
                pPr.remove(numPr)

    for para in doc.paragraphs:
        _strip(para)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    _strip(para)
                for tbl in cell.tables:
                    for trow in tbl.rows:
                        for tcell in trow.cells:
                            for para in tcell.paragraphs:
                                _strip(para)
    for section in doc.sections:
        for para in section.header.paragraphs if section.header else []:
            _strip(para)
        for para in section.footer.paragraphs if section.footer else []:
            _strip(para)


def _get_plan_profile_hints(plan):
    """Load template profile hints for a plan, or return None."""
    profile_id = plan.get("template_profile_id")
    if not profile_id:
        return None
    try:
        row = supabase.table("teacher_template_profiles").select("profile_data,status").eq("id", profile_id).single().execute()
        if not row.data or row.data.get("status") != "confirmed":
            return None
        import json as _json
        pd = row.data.get("profile_data")
        if isinstance(pd, str):
            pd = _json.loads(pd)
        tp_profile = (pd or {}).get("profile", {})
        if isinstance(tp_profile, dict) and isinstance((pd or {}).get("generation_spec"), dict):
            tp_profile = {**tp_profile, "generation_spec": (pd or {}).get("generation_spec")}
        return _build_template_profile_hints(tp_profile)
    except Exception:
        return None


def _get_plan_template_context(plan):
    """Load extracted template context for a plan, or return None."""
    profile_id = plan.get("template_profile_id")
    if not profile_id:
        return None
    try:
        row = supabase.table("teacher_template_profiles").select("profile_data,status").eq("id", profile_id).single().execute()
        if not row.data or row.data.get("status") != "confirmed":
            return None
        import json as _json
        pd = row.data.get("profile_data")
        if isinstance(pd, str):
            pd = _json.loads(pd)
        template_context = (pd or {}).get("template_context")
        return template_context if isinstance(template_context, dict) else None
    except Exception:
        return None


def _get_plan_generation_spec(plan):
    """Load or derive the saved generation spec for a plan's template profile.

    Prefers the stored generation_spec to avoid re-parsing the source DOCX
    (which takes ~30s+ for complex templates). Only re-derives when the
    stored spec has no clo_groups (missing CLO detection).
    """
    profile_id = plan.get("template_profile_id")
    if not profile_id:
        return None
    try:
        row = supabase.table("teacher_template_profiles").select("profile_data,status,source_storage_path").eq("id", profile_id).single().execute()
        if not row.data or row.data.get("status") != "confirmed":
            return None
        import json as _json
        pd = row.data.get("profile_data")
        if isinstance(pd, str):
            pd = _json.loads(pd)
        # Use stored generation_spec if it already has CLO groups (avoids ~30s DOCX re-parse)
        stored_spec = pd.get("generation_spec") if isinstance(pd, dict) else None
        if stored_spec and isinstance(stored_spec, dict) and stored_spec.get("clo_groups"):
            from app.services.template_generation_spec import normalize_generation_spec
            return normalize_generation_spec(stored_spec)
        source_doc = None
        if row.data.get("source_storage_path"):
            try:
                source_bytes = read_storage_bytes(STORAGE_BUCKET_NAME, row.data.get("source_storage_path"))
                source_doc = Document(BytesIO(bytes(source_bytes)))
            except Exception:
                source_doc = None
        return build_generation_spec(pd or {}, source_doc=source_doc)
    except Exception:
        return None


def _task_client():
    return current_app.config.get('SUPABASE_SERVICE') or supabase


def _update_task(task_id, percent=None, label=None, payload_patch=None, status=None, error_message=None):
    client = _task_client()
    update_data = {}
    if percent is not None:
        update_data["progress_percent"] = int(percent)
    if label is not None:
        update_data["progress_label"] = str(label)
    if status is not None:
        update_data["status"] = status
    if error_message is not None:
        update_data["error_message"] = error_message
    if payload_patch is not None:
        current = client.table("background_tasks").select("payload").eq("id", task_id).single().execute()
        payload = current.data.get("payload") if current.data and isinstance(current.data.get("payload"), dict) else {}
        payload.update(payload_patch)
        update_data["payload"] = payload
    if update_data:
        client.table("background_tasks").update(update_data).eq("id", task_id).execute()


def _make_beta_ai_preview_callback(task_id):
    state = {"text": "", "last_flush": 0.0}
    max_chars = 12000
    min_interval = 1.0

    def callback(task_key, text="", force=False):
        chunk = str(text or "")
        if chunk:
            state["text"] = (state["text"] + chunk)[-max_chars:]
        if not state["text"]:
            return
        now = time.time()
        if not force and (now - state["last_flush"]) < min_interval:
            return
        state["last_flush"] = now
        _update_task(
            task_id,
            payload_patch={
                "ai_live_preview": state["text"],
                "ai_live_task": task_key,
                "ai_live_updated_at": now,
            },
        )

    return callback


def _serialize_form_data(form_data):
    serialized = {}
    for key in form_data.keys():
        serialized[key] = form_data.getlist(key)
    return serialized


def _rebuild_form_data(serialized):
    pairs = []
    for key, values in (serialized or {}).items():
        if isinstance(values, list):
            for value in values:
                pairs.append((key, value))
        else:
            pairs.append((key, values))
    return MultiDict(pairs)


def _get_beta_output_template_path():
    configured = current_app.config.get("BETA_OUTPUT_TEMPLATE_PATH")
    if configured:
        from pathlib import Path
        return Path(configured)
    from pathlib import Path
    return Path(current_app.root_path).parent / "new_template.docx"


def _required_beta_template_placeholders():
    return {
        "{{course_title}}",
        "{{target_sdgs_display}}",
        "{{clo_1_statement}}",
        "{{clo_1_aligned_plos}}",
        "{{week_1_topics}}",
        "{{week_1_learning_resources}}",
    }


def _resolve_beta_output_template(plan):
    required_placeholders = _required_beta_template_placeholders()
    configured_path = _get_beta_output_template_path()
    candidates = []

    if configured_path.exists():
        candidates.append(("configured", configured_path.name, configured_path.read_bytes()))

    department_name = plan.get("department")
    department_id = get_department_id_by_name(department_name, local_supabase=_task_client()) if department_name else None
    template_rows = []
    if department_id:
        result = _task_client().table("templates").select("filename,name").eq("department_id", department_id).order("created_at", desc=True).execute()
        template_rows.extend(result.data or [])
    default_result = _task_client().table("templates").select("filename,name").eq("is_default", True).order("created_at", desc=True).execute()
    for row in default_result.data or []:
        if row not in template_rows:
            template_rows.append(row)

    for row in template_rows:
        filename = row.get("filename")
        if not filename:
            continue
        try:
            candidates.append(("library", row.get("name") or filename, read_storage_bytes(STORAGE_BUCKET_NAME, filename)))
        except Exception:
            continue

    for source_type, label, template_bytes in candidates:
        valid, _ = has_docx_placeholders(template_bytes, required_placeholders=required_placeholders)
        if valid:
            return template_bytes, label, source_type

    raise ValueError(
        "No valid beta placeholder template is configured. The available template files are static CLP documents, not placeholder templates."
    )


def _get_plan(plan_id):
    res = _task_client().table("course_learning_plans").select("*").eq("id", plan_id).single().execute()
    return res.data


def _save_plan(plan_id, content, plan=None, **extra):
    persistent_content = strip_runtime_copilot_cache(content)
    data = {
        "content": json.dumps(persistent_content),
    }
    if plan:
        data["subject"] = persistent_content.get("metadata", {}).get("course_title") or plan.get("subject")
        data["department"] = persistent_content.get("metadata", {}).get("department") or plan.get("department")
    data.update(extra)
    try:
        _task_client().table("course_learning_plans").update(data).eq("id", plan_id).execute()
    except Exception as exc:
        logger.error("Beta task failed to save plan %s: %s", plan_id, exc)
        raise


def _sanitize_action(payload):
    action = str(payload.get("action") or "").strip()
    allowed_actions = {
        "generate_all",
        "generate_clos",
        "generate_alignment",
        "generate_weekly",
        "regenerate_clo_row",
        "regenerate_week_row",
        "fix_final_review",
        "finalize_clp",
        "finalize_document",
    }
    if action not in allowed_actions:
        raise ValueError(f"Unsupported beta action: {action}")
    return action


def _sanitize_index(payload, key, min_val, max_val):
    try:
        value = int(payload.get(key) or 0)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {key} value.")
    if value < min_val or value > max_val:
        raise ValueError(f"Invalid {key} value: must be between {min_val} and {max_val}.")
    return value


def run_beta_action_task(app_context, task_id, plan_id, user_id, payload):
    client = _task_client()
    action = _sanitize_action(payload)
    form_data = _rebuild_form_data(payload.get("form_data"))
    preview_callback = _make_beta_ai_preview_callback(task_id)
    logger.info("Beta task %s started: action=%s plan_id=%s user_id=%s", task_id, action, plan_id, user_id)

    plan = _get_plan(plan_id)
    if not plan:
        logger.warning("Beta task %s: plan %s not found", task_id, plan_id)
        raise ValueError("Beta plan not found.")
    if plan.get("user_id") != user_id:
        logger.warning("Beta task %s: user %s unauthorized for plan %s", task_id, user_id, plan_id)
        raise PermissionError("Unauthorized beta task access.")

    _update_task(task_id, 3, "Loading beta draft")
    content = update_beta_content_from_form(plan.get("content") and json.loads(plan["content"]) or {}, form_data)
    _update_task(task_id, 6, "Reading course metadata")
    profile_hints = _get_plan_profile_hints(plan)
    template_context = _get_plan_template_context(plan)
    generation_spec = _get_plan_generation_spec(plan)
    if generation_spec:
        content["template_generation_spec"] = generation_spec
        content["template_profile_warnings"] = generation_spec.get("warnings", [])
        content = ensure_beta_shape(content)
    if profile_hints:
        _update_task(task_id, 8, "Template profile loaded")
    if template_context:
        _update_task(task_id, 9, "Template academic context loaded")

    if action == "generate_all":
        clo_count = len(content.get("clo_alignment_table") or [])
        weekly_count = len(content.get("weekly_course_outline") or [])
        _update_task(task_id, 10, "Preparing full beta generation")
        _update_task(task_id, 15, f"Sending to AI — generating {clo_count or 'template-detected'} CLO rows")
        with beta_ai_call_context(plan_id=plan_id, user_id=user_id, preview_callback=preview_callback):
            updated = generate_beta_clos(content, template_profile_hints=profile_hints, template_context=template_context)
        _update_task(task_id, 35, "Validating CLO statements")
        _, _, updated = validate_beta_content(updated)

        _update_task(task_id, 42, "Sending to AI — building alignment matrix")
        with beta_ai_call_context(plan_id=plan_id, user_id=user_id, preview_callback=preview_callback):
            updated = generate_beta_alignment(updated, template_context=template_context)
        _update_task(task_id, 62, "Validating alignment matrix")
        _, _, updated = validate_beta_content(updated)

        _update_task(task_id, 70, f"Sending to AI — generating {weekly_count or 'template-detected'} weekly rows")
        with beta_ai_call_context(plan_id=plan_id, user_id=user_id, preview_callback=preview_callback):
            updated = generate_beta_weekly(updated, template_profile_hints=profile_hints, template_context=template_context)
        _update_task(task_id, 88, "Validating weekly outline")
        _, _, updated = validate_beta_content(updated)

        _update_task(task_id, 94, "Saving generated draft for review")
        _save_plan(plan_id, updated, plan=plan, status="beta_review")
        logger.info("Beta task %s: generate_all completed for plan %s", task_id, plan_id)
        _update_task(task_id, 100, "Full AI generation complete", payload_patch={"redirect_url": f"/teacher/copilot/review/{plan_id}"})
        return

    if action == "generate_clos":
        clo_count = len(content.get("clo_alignment_table") or [])
        _update_task(task_id, 10, "Preparing CLO generation prompt")
        _update_task(task_id, 15, f"Sending to AI — generating {clo_count or 'template-detected'} CLO rows")
        with beta_ai_call_context(plan_id=plan_id, user_id=user_id, preview_callback=preview_callback):
            updated = generate_beta_clos(content, template_profile_hints=profile_hints, template_context=template_context)
        _update_task(task_id, 70, "AI returned CLO data")
        _update_task(task_id, 78, "Validating CLO statements & alignment")
        _, _, updated = validate_beta_content(updated)
        _update_task(task_id, 90, "Saving validated CLO rows to draft")
        _save_plan(plan_id, updated, plan=plan, status="beta_review")
        logger.info("Beta task %s: generate_clos completed for plan %s", task_id, plan_id)
        _update_task(task_id, 100, "CLO generation complete", payload_patch={"redirect_url": f"/teacher/copilot/review/{plan_id}"})
        return

    if action == "generate_alignment":
        _update_task(task_id, 10, "Preparing alignment matrix prompt")
        _update_task(task_id, 18, "Sending to AI — building CO-PO-IO mappings")
        with beta_ai_call_context(plan_id=plan_id, user_id=user_id, preview_callback=preview_callback):
            updated = generate_beta_alignment(content, template_context=template_context)
        _update_task(task_id, 70, "AI returned alignment data")
        _update_task(task_id, 80, "Validating alignment matrix integrity")
        _, _, updated = validate_beta_content(updated)
        _update_task(task_id, 92, "Saving validated alignment to draft")
        _save_plan(plan_id, updated, plan=plan, status="beta_review")
        logger.info("Beta task %s: generate_alignment completed for plan %s", task_id, plan_id)
        _update_task(task_id, 100, "Alignment generation complete", payload_patch={"redirect_url": f"/teacher/copilot/review/{plan_id}"})
        return

    if action == "generate_weekly":
        weekly_count = len(content.get("weekly_course_outline") or [])
        _update_task(task_id, 10, "Preparing weekly outline prompt")
        _update_task(task_id, 15, f"Sending to AI — generating {weekly_count or 'template-detected'} weekly rows")
        with beta_ai_call_context(plan_id=plan_id, user_id=user_id, preview_callback=preview_callback):
            updated = generate_beta_weekly(content, template_profile_hints=profile_hints, template_context=template_context)
        _update_task(task_id, 70, "AI returned weekly outline")
        _update_task(task_id, 80, "Validating week labels & content")
        _, _, updated = validate_beta_content(updated)
        _update_task(task_id, 92, "Saving validated weekly outline to draft")
        _save_plan(plan_id, updated, plan=plan, status="beta_review")
        logger.info("Beta task %s: generate_weekly completed for plan %s", task_id, plan_id)
        _update_task(task_id, 100, "Weekly outline generation complete", payload_patch={"redirect_url": f"/teacher/copilot/review/{plan_id}"})
        return

    if action == "regenerate_clo_row":
        clo_rows = content.get("clo_alignment_table") if isinstance(content.get("clo_alignment_table"), list) else []
        clo_index = _sanitize_index(payload, "clo_index", 1, len(clo_rows) if clo_rows else 0)
        _update_task(task_id, 12, f"Preparing CLO {clo_index} regeneration")
        _update_task(task_id, 20, f"Sending to AI — regenerating CLO {clo_index}")
        with beta_ai_call_context(plan_id=plan_id, user_id=user_id, preview_callback=preview_callback):
            content = generate_single_clo_with_alignment(content, clo_index, template_context=template_context)
        _update_task(task_id, 72, f"AI returned updated CLO {clo_index}")
        _update_task(task_id, 82, "Validating updated CLO row & alignment")
        _, _, content = validate_beta_content(content)
        _update_task(task_id, 92, "Saving updated CLO to draft")
        _save_plan(plan_id, content, plan=plan, status="beta_review")
        logger.info("Beta task %s: regenerate_clo_row %s completed for plan %s", task_id, clo_index, plan_id)
        _update_task(task_id, 100, f"CLO {clo_index} regeneration complete", payload_patch={"redirect_url": f"/teacher/copilot/review/{plan_id}"})
        return

    if action == "regenerate_week_row":
        weekly_rows = content.get("weekly_course_outline") if isinstance(content.get("weekly_course_outline"), list) else []
        week_index = _sanitize_index(payload, "week_index", 1, len(weekly_rows) if weekly_rows else 0)
        target_label = weekly_rows[week_index - 1].get('time_frame_label', f'week {week_index}') if weekly_rows else f'week {week_index}'
        _update_task(task_id, 12, f"Preparing {target_label} regeneration")
        _update_task(task_id, 20, f"Sending to AI — regenerating {target_label}")
        with beta_ai_call_context(plan_id=plan_id, user_id=user_id, preview_callback=preview_callback):
            content = generate_single_week_row(content, week_index)
        _update_task(task_id, 72, f"AI returned updated {target_label}")
        _update_task(task_id, 82, "Validating updated weekly block")
        _, _, content = validate_beta_content(content)
        _update_task(task_id, 92, "Saving updated week to draft")
        _save_plan(plan_id, content, plan=plan, status="beta_review")
        logger.info("Beta task %s: regenerate_week_row %s completed for plan %s", task_id, week_index, plan_id)
        _update_task(task_id, 100, f"{target_label} regeneration complete", payload_patch={"redirect_url": f"/teacher/copilot/review/{plan_id}"})
        return

    if action == "fix_final_review":
        _update_task(task_id, 8, "Analyzing flagged review issues")
        validation = content.get("validation") if isinstance(content.get("validation"), dict) else {}
        issues = validation.get("final_review_issues", []) if isinstance(validation.get("final_review_issues"), list) else []
        if not issues:
            issues = validation.get("errors", []) if isinstance(validation.get("errors"), list) else []
        review_warnings = validation.get("final_review_warnings", []) if isinstance(validation.get("final_review_warnings"), list) else []
        visible_warnings = validation.get("warnings", []) if isinstance(validation.get("warnings"), list) else []
        warning_targets = [
            str(item).strip()
            for item in list(review_warnings or []) + list(visible_warnings or [])
            if str(item).strip()
        ]
        warning_targets = list(dict.fromkeys(warning_targets))
        if not issues and not warning_targets:
            _update_task(task_id, 100, "No issues to fix", payload_patch={"redirect_url": f"/teacher/copilot/review/{plan_id}"})
            return
        target_count = len(issues) + len(warning_targets)
        _update_task(task_id, 18, f"Found {target_count} item(s) to repair")
        _update_task(task_id, 25, "Sending to AI — applying repair pass")
        with beta_ai_call_context(plan_id=plan_id, user_id=user_id, preview_callback=preview_callback):
            fixed_content = run_beta_final_fix(content, issues=issues, warnings=warning_targets)
        fixed_content.setdefault("validation", {})
        resolved_warnings = [
            str(item).strip()
            for item in fixed_content["validation"].get("resolved_warnings", [])
            if str(item).strip()
        ]
        fixed_content["validation"]["resolved_warnings"] = list(dict.fromkeys(resolved_warnings + warning_targets))
        fixed_content["validation"]["errors"] = []
        fixed_content["validation"]["warnings"] = []
        fixed_content["validation"]["final_review_issues"] = []
        fixed_content["validation"]["final_review_warnings"] = []
        fixed_content["validation"]["final_review_summary"] = ""
        _update_task(task_id, 48, "AI repair pass returned")
        _update_task(task_id, 55, "Validating repaired content")
        base_errors, base_warnings, fixed_content = validate_beta_content(fixed_content, require_final_ready=False)
        _update_task(task_id, 65, "Sending to AI — running final review")
        _update_task(task_id, 72, "Waiting for final review verdict")
        with beta_ai_call_context(plan_id=plan_id, user_id=user_id, preview_callback=preview_callback):
            final_review = run_beta_final_review(fixed_content)
        fixed_content["validation"]["final_review_summary"] = final_review.get("summary", "")
        final_warnings = [
            item for item in (final_review.get("warnings", []) or [])
            if str(item).strip() not in fixed_content["validation"]["resolved_warnings"]
        ]
        fixed_content["validation"]["final_review_warnings"] = final_warnings
        base_warnings = [
            item for item in (base_warnings or [])
            if str(item).strip() not in fixed_content["validation"]["resolved_warnings"]
        ]
        if final_review.get("approved"):
            fixed_content["validation"]["errors"] = base_errors
            fixed_content["validation"]["warnings"] = list(dict.fromkeys(base_warnings + final_warnings))
        else:
            review_issues = final_review.get("issues", []) or ["AI repair improved the draft, but final review still found issues to fix."]
            fixed_content["validation"]["final_review_issues"] = review_issues
            fixed_content["validation"]["errors"] = review_issues
            fixed_content["validation"]["warnings"] = list(dict.fromkeys(base_warnings + final_warnings))
        fixed_content["review_stage"] = fixed_content.get("review_stage") or content.get("review_stage", "weekly_generated")
        _save_plan(plan_id, fixed_content, plan=plan, status="beta_review")
        logger.info("Beta task %s: fix_final_review completed for plan %s (approved=%s)", task_id, plan_id, final_review.get("approved"))
        _update_task(task_id, 100, "Repair complete", payload_patch={"redirect_url": f"/teacher/copilot/review/{plan_id}"})
        return

    if action == "finalize_clp":
        _update_task(task_id, 15, "Running full content validation")
        _update_task(task_id, 25, "Checking CLOs, alignment & weekly outline")
        errors, warnings, validated_content = validate_beta_content(content, require_final_ready=True)
        validated_content.setdefault("validation", {})
        validated_content["validation"]["final_review_issues"] = []
        validated_content["validation"]["final_review_warnings"] = []
        validated_content["validation"]["final_review_summary"] = ""
        if errors:
            _save_plan(plan_id, validated_content, plan=plan)
            summary = "; ".join(str(item).strip() for item in errors[:3] if str(item).strip())
            if len(errors) > 3:
                summary = f"{summary}; and {len(errors) - 3} more." if summary else f"{len(errors)} validation issues remain."
            error_message = summary or "Beta draft is not ready yet. Fix the validation issues before marking it ready."
            _update_task(
                task_id,
                100,
                "Validation blocked",
                payload_patch={"redirect_url": f"/teacher/copilot/review/{plan_id}"},
                status="failed",
                error_message=error_message,
            )
            return
        validated_content["beta_ready_for_template"] = True
        if not content.get("beta_document_inserted"):
            validated_content["beta_document_inserted"] = False
            validated_content["beta_document_filename"] = ""
        validated_content["review_stage"] = "beta_ready"
        _update_task(task_id, 85, "All checks passed — marking draft as ready")
        _save_plan(plan_id, validated_content, plan=plan, status="beta_ready")
        logger.info("Beta task %s: finalize_clp completed for plan %s", task_id, plan_id)
        _update_task(task_id, 100, "Draft marked ready", payload_patch={"redirect_url": f"/teacher/copilot/review/{plan_id}"})
        return

    if action == "finalize_document":
        _update_task(task_id, 8, "Running full content validation")
        _update_task(task_id, 15, "Checking CLOs, alignment & weekly outline")
        errors, warnings, validated_content = validate_beta_content(content, require_final_ready=True)
        validated_content.setdefault("validation", {})
        validated_content["validation"]["final_review_issues"] = []
        validated_content["validation"]["final_review_warnings"] = []
        validated_content["validation"]["final_review_summary"] = ""
        if errors:
            _update_task(task_id, 22, f"Found {len(errors)} issue(s) — running AI repair")
            _update_task(task_id, 28, "Sending to AI — applying auto-repair")
            with beta_ai_call_context(plan_id=plan_id, user_id=user_id, preview_callback=preview_callback):
                repaired_content = run_beta_final_fix(content, issues=errors, warnings=warnings)
            repaired_errors, repaired_warnings, repaired_content = validate_beta_content(repaired_content, require_final_ready=True)
            repaired_content.setdefault("validation", {})
            repaired_content["validation"]["final_review_issues"] = []
            repaired_content["validation"]["final_review_warnings"] = []
            repaired_content["validation"]["final_review_summary"] = "AI repair pass ran automatically before template insertion."
            if repaired_errors:
                _save_plan(plan_id, repaired_content, plan=plan)
                summary = "; ".join(str(item).strip() for item in repaired_errors[:3] if str(item).strip())
                if len(repaired_errors) > 3:
                    summary = f"{summary}; and {len(repaired_errors) - 3} more." if summary else f"{len(repaired_errors)} validation issues remain."
                error_message = summary or "Template insertion is still blocked until the beta draft passes validation."
                _update_task(
                    task_id,
                    100,
                    "Insertion blocked",
                    payload_patch={"redirect_url": f"/teacher/copilot/review/{plan_id}"},
                    status="failed",
                    error_message=error_message,
                )
                return
            validated_content = repaired_content
            warnings = repaired_warnings

        template_label = ""
        template_source = ""

        _update_task(task_id, 42, "Sending to AI — running final review")
        cached_review = get_cached_beta_final_review(validated_content)
        if cached_review:
            final_review = cached_review
        else:
            with beta_ai_call_context(plan_id=plan_id, user_id=user_id, preview_callback=preview_callback):
                final_review = run_beta_final_review(validated_content)
        validated_content["validation"]["final_review_signature"] = get_beta_final_review_signature(validated_content)
        validated_content["validation"]["final_review_approved"] = bool(final_review.get("approved"))
        validated_content["validation"]["final_review_summary"] = final_review.get("summary", "")
        validated_content["validation"]["final_review_warnings"] = final_review.get("warnings", [])
        review_issues = []
        if not final_review.get("approved"):
            review_issues = final_review.get("issues", []) or ["Final AI review did not approve this draft yet."]
            validated_content["validation"]["final_review_issues"] = []
            validated_content["validation"]["errors"] = []
            validated_content["validation"]["final_review_warnings"] = list(dict.fromkeys(
                (validated_content["validation"].get("final_review_warnings") or []) + review_issues
            ))
            validated_content["validation"]["warnings"] = list(dict.fromkeys(
                (validated_content["validation"].get("warnings") or []) + final_review.get("warnings", []) + review_issues
            ))

        _update_task(task_id, 55, "Final review returned")
        _update_task(task_id, 58, "Preparing document content")
        _update_task(task_id, 60, "Building populated DOCX")
        user_profile = {}
        user_res = client.table("users").select("first_name,last_name,title,consultation_hours").eq("id", user_id).single().execute()
        if user_res.data:
            user_profile = user_res.data

        # Check for a profile-based rendering path.
        profile_used = False
        insertion_doc = None
        template_profile_id = plan.get("template_profile_id")
        enforce_profile_template = bool(template_profile_id)
        if template_profile_id:
            try:
                profile_row = client.table("teacher_template_profiles").select("*").eq("id", template_profile_id).eq("status", "confirmed").single().execute()
                if profile_row.data:
                    template_label = profile_row.data.get("name", "Custom Template Profile")
                    template_source = "template_profile"
                    _update_task(task_id, 35, f"Using template profile: {str(template_label)[:40]}")
                    import json as _json
                    profile_data_raw = profile_row.data.get("profile_data")
                    if isinstance(profile_data_raw, str):
                        profile_data_raw = _json.loads(profile_data_raw)

                    # ── Optional insertion-template base ──
                    insertion_path = (profile_data_raw or {}).get("insertion_template_path")
                    if insertion_path:
                        try:
                            insertion_bytes = read_storage_bytes(STORAGE_BUCKET_NAME, insertion_path)
                            if insertion_bytes:
                                insertion_doc = Document(BytesIO(bytes(insertion_bytes)))
                                template_label = profile_row.data.get("name", "Custom Template Profile")
                                template_source = "template_profile"
                                logger.info(
                                    "Beta task %s: loaded insertion template (placeholder-based) from profile %s (%d bytes)",
                                    task_id, template_profile_id, len(insertion_bytes),
                                )
                        except Exception as ins_exc:
                            logger.warning(
                                "Beta task %s: insertion template load failed for profile %s: %s",
                                task_id, template_profile_id, ins_exc,
                            )

                    # ── Locator-based apply_profile ──
                    # Always prefer the refreshed profile/spec writer for
                    # table-heavy templates. Insertion templates are kept only
                    # as a fallback because older generated insertion copies may
                    # contain placeholders for metadata but not for dynamic
                    # table regions.
                    tp_profile = (profile_data_raw or {}).get("profile", {})
                    source_path = profile_row.data.get("source_storage_path")
                    if tp_profile and source_path:
                        source_bytes = read_storage_bytes(STORAGE_BUCKET_NAME, source_path)
                        if source_bytes:
                            source_doc_for_spec = Document(BytesIO(bytes(source_bytes)))
                            generation_spec = build_generation_spec(profile_data_raw or {}, source_doc=source_doc_for_spec)
                            tp_profile = merge_spec_into_profile(tp_profile, generation_spec)
                            validated_content["template_generation_spec"] = generation_spec
                            validated_content["template_profile_warnings"] = generation_spec.get("warnings", [])
                            validated_content = ensure_beta_shape(validated_content)
                            # Persist the merged profile + spec back so future
                            # renders skip the DOCX parse (best-effort).
                            try:
                                persisted_profile_data = dict(profile_data_raw or {})
                                persisted_profile_data['profile'] = tp_profile
                                persisted_profile_data['generation_spec'] = generation_spec
                                client.table("teacher_template_profiles").update({
                                    "profile_data": _json.dumps(persisted_profile_data),
                                }).eq("id", template_profile_id).execute()
                                logger.info("Beta task %s: persisted refreshed generation_spec for profile %s",
                                            task_id, template_profile_id)
                            except Exception as persist_exc:
                                logger.warning("Beta task %s: failed to persist generation_spec for profile %s: %s",
                                               task_id, template_profile_id, persist_exc)
                            from app.services.template_content_adapter import build_profile_content, validate_profile_coverage
                            from app.services.template_writer import apply_profile
                            # Run coverage validation before rendering.
                            coverage_gaps = validate_profile_coverage(validated_content, tp_profile, user_profile=user_profile)
                            if coverage_gaps:
                                existing_warnings = validated_content.get("validation", {}).get("warnings", [])
                                for gap in coverage_gaps:
                                    prefixed = f"[Template Profile] {gap}"
                                    if prefixed not in existing_warnings:
                                        existing_warnings.append(prefixed)
                                validated_content.setdefault("validation", {})["warnings"] = existing_warnings
                                logger.info("Beta task %s: %d coverage gaps for plan %s: %s", task_id, len(coverage_gaps), plan_id, coverage_gaps[:5])
                            profile_content = build_profile_content(validated_content, tp_profile, user_profile=user_profile)
                            if insertion_doc is not None:
                                doc = insertion_doc
                            else:
                                doc = Document(BytesIO(bytes(source_bytes)))
                            doc, write_results = apply_profile(doc, tp_profile, profile_content)
                            ok_count = sum(1 for v in write_results.values() if v == 'ok')
                            fail_count = sum(1 for v in write_results.values() if v != 'ok')
                            failed_sections = {k: v for k, v in write_results.items() if v != 'ok'}

                            # Error budget: any failure in a critical section means we
                            # cannot trust this render. Fall back to the placeholder
                            # template path instead of shipping a half-written DOCX.
                            critical_prefixes = ('clo_', 'weekly', 'course_outline', 'meta:')
                            critical_failures = {
                                k: v for k, v in failed_sections.items()
                                if k.startswith(critical_prefixes) or k in ('weekly_course_outline', 'clo_alignment_table')
                            }
                            if critical_failures:
                                logger.warning(
                                    "Beta task %s: apply_profile had %d critical failures, falling back to placeholder template: %s",
                                    task_id, len(critical_failures), critical_failures,
                                )
                                try:
                                    log_system_event(
                                        'template_profile', 'warning',
                                        'Profile rendering fallback (critical section failures)',
                                        details={
                                            'plan_id': plan_id, 'task_id': task_id,
                                            'critical_failures': dict(list(critical_failures.items())[:10]),
                                            'fail_count': fail_count, 'ok_count': ok_count,
                                        },
                                    )
                                except Exception:
                                    pass
                                profile_used = False
                                validated_content["beta_profile_render_ok"] = ok_count
                                validated_content["beta_profile_render_fail"] = fail_count
                                validated_content["beta_profile_critical_failures"] = list(critical_failures.keys())[:10]
                            else:
                                template_label = profile_row.data.get("name", "Custom Template Profile")
                                template_source = "template_profile"
                                profile_used = True
                                validated_content["beta_profile_render_ok"] = ok_count
                                validated_content["beta_profile_render_fail"] = fail_count
                                validated_content["beta_profile_coverage_gaps"] = len(coverage_gaps) if coverage_gaps else 0
                                if fail_count:
                                    logger.warning("Beta task %s: apply_profile had %d non-critical failures: %s",
                                                   task_id, fail_count, failed_sections)
                                logger.info("Beta task %s: profile-based rendering ok=%d fail=%d for plan %s",
                                            task_id, ok_count, fail_count, plan_id)
            except Exception as profile_exc:
                logger.warning("Beta task %s: profile-based rendering failed: %s", task_id, profile_exc)
                profile_used = False
                try:
                    log_system_event('template_profile', 'warning', 'Profile rendering fallback',
                        details={'plan_id': plan_id, 'task_id': task_id, 'error': str(profile_exc)[:200]})
                except Exception:
                    pass

        if enforce_profile_template and not profile_used and insertion_doc is None:
            raise ValueError(
                "Selected template profile could not be rendered. "
                "Please re-confirm or re-profile the template profile and try again."
            )

        if not profile_used:
            _update_task(task_id, 35, "Resolving output template")
            # When the profile-based path didn't apply (e.g. no source_path)
            # but an insertion template was loaded, fall back to that
            # placeholder-rich document instead of a stock template.
            if insertion_doc is not None:
                doc = insertion_doc
                _update_task(task_id, 38, "Using insertion template")
                profile_used = True
            else:
                template_bytes, template_label, template_source = _resolve_beta_output_template(plan)
                _update_task(task_id, 38, f"Using template: {template_label[:40]}")
                doc = Document(BytesIO(bytes(template_bytes)))
            
        # Always run a placeholder replacement pass.
        # If profile_used is True, this safely sweeps up any {{placeholders}} (like dates/names) 
        # that the layout-based `apply_profile` missed or couldn't reach.
        # If profile_used is False, this acts as the primary rendering mechanism.
        replacements = build_beta_docx_replacements(validated_content, user_profile=user_profile)
        doc = replace_placeholders(doc, replacements)

        # Strip ALL list numbering (numPr) from every paragraph in the
        # document.  Many CLP templates use Wingdings checkmark bullets
        # (lvlText=\uf0fc) as list markers; modern OnlyOffice servers
        # that don't ship Wingdings crash on these characters.  Removing
        # numPr leaves the paragraph text visually intact and avoids the
        # rendering error entirely.
        _strip_all_numbering(doc)

        output = BytesIO()
        doc.save(output)
        output.seek(0)

        subject_name = secure_filename(validated_content.get("metadata", {}).get("course_title") or plan.get("subject") or f"beta_clp_{plan_id}") or f"beta_clp_{plan_id}"
        new_filename = f"{user_id}/{subject_name}_{int(time.time())}.docx"
        old_filename = plan.get("filename")

        _update_task(task_id, 72, "DOCX rendered successfully")
        _update_task(task_id, 78, "Uploading generated DOCX to storage")
        write_storage_bytes(STORAGE_BUCKET_NAME, new_filename, output.read())
        if not storage_file_exists(STORAGE_BUCKET_NAME, new_filename):
            raise FileNotFoundError(f"Generated template file was not found after upload: {new_filename}")

        validated_content["beta_ready_for_template"] = True
        validated_content["beta_document_inserted"] = True
        validated_content["beta_document_filename"] = new_filename
        validated_content["beta_template_source_label"] = template_label
        validated_content["beta_template_source_type"] = template_source
        validated_content["beta_template_copy_mode"] = True
        validated_content["review_stage"] = "beta_inserted"
        validated_content["validation"]["warnings"] = list(dict.fromkeys((validated_content["validation"].get("warnings") or []) + final_review.get("warnings", [])))

        _update_task(task_id, 85, "Upload complete")
        _update_task(task_id, 90, "Saving document metadata to plan")
        _save_plan(plan_id, validated_content, plan=plan, filename=new_filename, status="beta_ready")

        if old_filename and old_filename != new_filename:
            cleanup_storage_after_commit(old_filename, context="Beta finalize document", user_id=user_id, plan_id=plan_id)

        ensure_plan_document_file({**plan, "id": plan_id, "filename": new_filename})

        log_system_event(
            "workflow",
            "info",
            "Beta copilot draft rendered into DOCX template",
            details={"plan_id": plan_id, "template_label": template_label, "template_source": template_source, "filename": new_filename},
            user_id=user_id,
            plan_id=plan_id,
        )
        logger.info("Beta task %s: finalize_document completed for plan %s (template=%s, file=%s)", task_id, plan_id, template_label, new_filename)
        redirect_url = f"/teacher/copilot/beta/review/{plan_id}"
        _update_task(task_id, 96, "Running post-insertion checks")
        _update_task(task_id, 100, "Document finalized", payload_patch={"redirect_url": redirect_url})
        return

    raise ValueError(f"Unsupported beta action: {action}")
