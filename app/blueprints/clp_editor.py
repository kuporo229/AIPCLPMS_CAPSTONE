import json
import time
from datetime import datetime, timezone
from uuid import UUID as UUIDType

import jwt
from flask import Blueprint, current_app, jsonify, render_template, request, send_file, session
from sqlalchemy.exc import SQLAlchemyError

from app.decorators import login_required, roles_required
from app.extensions import db
from app.models.clp_document import CLPDocument
from app.services.ai_client import AIClient
from app.services.clp_editor_schema import (
    apply_alignment_rows,
    apply_generated_section,
    default_semantic_clp,
    default_tiptap_doc,
    get_tiptap_projection,
    normalize_semantic_clp,
    semantic_summary_for_ai,
    semantic_to_tiptap_doc,
    set_tiptap_projection,
)
from app.services.clp_editor_docx import (
    convert_docx_bytes_to_pdf,
    export_semantic_to_docx_bytes,
    export_tiptap_projection_to_docx_bytes,
    import_docx_to_semantic,
)
from app.services.tiptap_conversion_service import (
    export_tiptap_to_docx_bytes,
    export_tiptap_to_pdf_bytes,
    import_docx_to_tiptap,
    is_tiptap_conversion_configured,
)
from app.services.tiptap_document_server import document_identifier, upsert_tiptap_document


clp_editor_bp = Blueprint("clp_editor", __name__)


def _default_tiptap_json():
    return default_tiptap_doc()


def _default_editor_json(title="", department=""):
    return default_semantic_clp(title=title, department=department, owner_user_id=session.get("user_id"))


def _current_user_uuid():
    return UUIDType(str(session["user_id"]))


def _json_exception(message, exc, status=500):
    current_app.logger.exception("%s: %s", message, exc)
    return jsonify({"ok": False, "error": message, "detail": str(exc)}), status


def _owned_document_or_404(document_id):
    document = CLPDocument.query.filter_by(id=document_id).first_or_404()
    if str(document.owner_id) != str(session["user_id"]):
        return None, (jsonify({"ok": False, "error": "Unauthorized."}), 403)
    return document, None


def _clean_plain_ai_text(value):
    text = (value or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if text.lower().startswith("rewritten text:"):
        text = text.split(":", 1)[1].strip()
    return text.strip('"').strip()


def _parse_ai_json(text):
    cleaned = AIClient.clean_ai_json(text)
    return json.loads(cleaned)


def _version_count(document_id):
    return db.session.execute(
        db.text("SELECT COALESCE(MAX(version_number), 0) FROM clp_document_versions WHERE document_id = :id"),
        {"id": document_id},
    ).scalar() or 0


def _save_editor_version(document, summary):
    version_number = _version_count(document.id) + 1
    db.session.execute(
        db.text(
            """
            INSERT INTO clp_document_versions
              (document_id, version_number, editor_json, tiptap_json, change_summary, actor_id)
            VALUES
              (:document_id, :version_number, CAST(:editor_json AS jsonb), CAST(:tiptap_json AS jsonb), :change_summary, :actor_id)
            """
        ),
        {
            "document_id": document.id,
            "version_number": version_number,
            "editor_json": json.dumps(document.editor_json),
            "tiptap_json": json.dumps(document.tiptap_json),
            "change_summary": summary,
            "actor_id": str(session["user_id"]),
        },
    )
    return version_number


@clp_editor_bp.get("/clp-editor/<int:document_id>")
@login_required
@roles_required("teacher")
def editor_page(document_id):
    document, error = _owned_document_or_404(document_id)
    if error:
        return error
    return render_template("clp_editor.html", document=document)


@clp_editor_bp.post("/api/clp-documents")
@login_required
@roles_required("teacher")
def create_clp_document():
    payload = request.get_json(silent=True) or {}

    title = (payload.get("title") or "Untitled CLP").strip()
    department = (payload.get("department") or "General").strip()

    editor_json = _default_editor_json(title=title, department=department)
    tiptap_json = get_tiptap_projection(editor_json)

    try:
        document = CLPDocument(
            owner_id=_current_user_uuid(),
            department=department,
            title=title,
            status="draft",
            editor_json=editor_json,
            tiptap_json=tiptap_json,
        )

        db.session.add(document)
        db.session.commit()
        try:
            upsert_tiptap_document(document_identifier(document.id), tiptap_json)
        except Exception as exc:
            current_app.logger.warning("TipTap document server sync failed for CLP document %s: %s", document.id, exc)
    except (ValueError, SQLAlchemyError) as exc:
        db.session.rollback()
        return _json_exception("Failed to create CLP document.", exc)

    return jsonify({"ok": True, "document_id": document.id}), 201


@clp_editor_bp.get("/api/clp-documents/<int:document_id>/collab-token")
@login_required
@roles_required("teacher")
def clp_document_collab_token(document_id):
    document, error = _owned_document_or_404(document_id)
    if error:
        return error

    document_name = document_identifier(document.id)
    server_id = current_app.config.get("TIPTAP_DOCUMENT_SERVER_ID")
    server_secret = current_app.config.get("TIPTAP_DOCUMENT_SERVER_SECRET")
    if not server_id or not server_secret:
        return jsonify({"ok": False, "error": "TipTap document server is not configured."}), 503

    now = int(time.time())
    token = jwt.encode(
        {
            "sub": str(session["user_id"]),
            "allowedDocumentNames": [document_name],
            "iat": now,
            "exp": now + 60 * 60 * 6,
        },
        server_secret,
        algorithm="HS256",
    )

    return jsonify(
        {
            "ok": True,
            "document_name": document_name,
            "document_server_id": server_id,
            "token": token,
        }
    )


@clp_editor_bp.get("/api/clp-documents/<int:document_id>")
@login_required
@roles_required("teacher")
def load_clp_document(document_id):
    document, error = _owned_document_or_404(document_id)
    if error:
        return error
    editor_json = normalize_semantic_clp(
        document.editor_json,
        title=document.title,
        department=document.department,
        owner_user_id=document.owner_id,
        plan_id=document.id,
    )
    tiptap_json = get_tiptap_projection(editor_json)

    return jsonify(
        {
            "ok": True,
            "document": {
                "id": document.id,
                "title": document.title,
                "department": document.department,
                "status": document.status,
                "editor_json": editor_json,
                "tiptap_json": tiptap_json,
            },
        }
    )


@clp_editor_bp.put("/api/clp-documents/<int:document_id>")
@login_required
@roles_required("teacher")
def save_clp_document(document_id):
    document, error = _owned_document_or_404(document_id)
    if error:
        return error

    if document.status not in {"draft", "revision"}:
        return jsonify({"ok": False, "error": "Document is not editable."}), 409

    payload = request.get_json(silent=True) or {}
    title = (payload.get("title") or document.title).strip()
    editor_json = payload.get("editor_json")
    tiptap_json = payload.get("tiptap_json")

    if not isinstance(editor_json, dict):
        return jsonify({"ok": False, "error": "editor_json must be an object."}), 400
    if not isinstance(tiptap_json, dict):
        return jsonify({"ok": False, "error": "tiptap_json must be an object."}), 400
    if tiptap_json.get("type") != "doc":
        return jsonify({"ok": False, "error": "Invalid TipTap document."}), 400

    editor_json = normalize_semantic_clp(
        editor_json,
        title=title,
        department=document.department,
        owner_user_id=document.owner_id,
        plan_id=document.id,
    )
    editor_json = set_tiptap_projection(editor_json, tiptap_json)

    try:
        document.title = title
        document.editor_json = editor_json
        document.tiptap_json = tiptap_json
        document.updated_at = datetime.now(timezone.utc)
        if payload.get("save_version"):
            _save_editor_version(document, payload.get("change_summary") or "Manual save")

        db.session.commit()
        try:
            upsert_tiptap_document(document_identifier(document.id), tiptap_json)
        except Exception as exc:
            current_app.logger.warning("TipTap document server sync failed for CLP document %s: %s", document.id, exc)
    except SQLAlchemyError as exc:
        db.session.rollback()
        return _json_exception("Failed to save CLP document.", exc)

    return jsonify(
        {
            "ok": True,
            "document": {
                "id": document.id,
                "title": document.title,
                "updated_at": document.updated_at.isoformat(),
            },
        }
    )


@clp_editor_bp.post("/api/ai/rewrite")
@login_required
@roles_required("teacher")
def ai_rewrite():
    payload = request.get_json(silent=True) or {}
    document_id = payload.get("document_id")
    text = (payload.get("text") or "").strip()

    if not document_id or not text:
        return jsonify({"ok": False, "error": "document_id and text are required."}), 400

    try:
        document_id = int(document_id)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "document_id must be an integer."}), 400

    _, error = _owned_document_or_404(document_id)
    if error:
        return error

    prompt = f"""
Rewrite this Course Learning Plan text.

Rules:
- Return only the rewritten text.
- Keep the meaning.
- Use clear academic language.
- Make outcomes measurable when appropriate.
- Do not add markdown.

Text:
{text}
"""

    try:
        response = AIClient.generate_with_retry(
            AIClient.get_model(),
            [prompt],
            {"temperature": 0.4},
            task_type="clp_editor_rewrite",
            plan_id=None,
            user_id=session.get("user_id"),
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502

    cleaned = _clean_plain_ai_text(response.text)
    if not cleaned:
        return jsonify({"ok": False, "error": "AI returned an empty rewrite."}), 502

    return jsonify({"ok": True, "text": cleaned})


@clp_editor_bp.post("/api/ai/align-table")
@login_required
@roles_required("teacher")
def ai_align_table():
    payload = request.get_json(silent=True) or {}
    document_id = payload.get("document_id")
    rows = payload.get("rows")
    allowed_values = payload.get("allowed_values") or ["I", "R", "M", "A", ""]

    if not document_id or not isinstance(rows, list):
        return jsonify({"ok": False, "error": "document_id and rows are required."}), 400

    document, error = _owned_document_or_404(int(document_id))
    if error:
        return error

    semantic = normalize_semantic_clp(document.editor_json, title=document.title, department=document.department, owner_user_id=document.owner_id, plan_id=document.id)
    prompt = f"""
You are completing a CLO to PLO alignment table for a Course Learning Plan.

Return JSON only. Do not return markdown tables.

Allowed values:
{json.dumps(allowed_values, ensure_ascii=False)}

Rows:
{json.dumps(rows, ensure_ascii=False)}

Document context:
{json.dumps(semantic_summary_for_ai(semantic), ensure_ascii=False)}

Return:
{{
  "rows": [
    {{"source_clo_id": "", "target_plo_id": "", "mapping_value": "", "rationale": ""}}
  ],
  "warnings": []
}}
"""
    try:
        response = AIClient.generate_with_retry(
            AIClient.get_model(),
            [prompt],
            {"temperature": 0.2, "response_mime_type": "application/json"},
            task_type="editor_table_alignment",
            plan_id=document.id,
            user_id=session.get("user_id"),
        )
        data = _parse_ai_json(response.text)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502

    output_rows = data.get("rows") or []
    invalid = [row for row in output_rows if (row.get("mapping_value") or "") not in allowed_values]
    if invalid:
        return jsonify({"ok": False, "error": "AI returned invalid alignment values."}), 422

    semantic = apply_alignment_rows(semantic, output_rows)
    document.editor_json = semantic
    document.tiptap_json = get_tiptap_projection(semantic)
    db.session.commit()
    return jsonify({"ok": True, "rows": output_rows, "warnings": data.get("warnings", [])})


@clp_editor_bp.post("/api/ai/generate-section")
@login_required
@roles_required("teacher")
def ai_generate_section():
    payload = request.get_json(silent=True) or {}
    document_id = payload.get("document_id")
    section_key = (payload.get("section_key") or "").strip()
    instruction = (payload.get("instruction") or "").strip()

    if not document_id or not section_key or not instruction:
        return jsonify({"ok": False, "error": "document_id, section_key, and instruction are required."}), 400

    document, error = _owned_document_or_404(int(document_id))
    if error:
        return error

    semantic = normalize_semantic_clp(document.editor_json, title=document.title, department=document.department, owner_user_id=document.owner_id, plan_id=document.id)
    prompt = f"""
Generate one section of a Course Learning Plan.

Return JSON only. Do not include markdown.

Section key: {section_key}
Instruction: {instruction}

Document context:
{json.dumps(semantic_summary_for_ai(semantic), ensure_ascii=False)}

Return:
{{
  "section_key": "{section_key}",
  "content": {{}},
  "warnings": []
}}
"""
    try:
        response = AIClient.generate_with_retry(
            AIClient.get_model(),
            [prompt],
            {"temperature": 0.5, "response_mime_type": "application/json"},
            task_type=f"editor_section_generation:{section_key}",
            plan_id=document.id,
            user_id=session.get("user_id"),
        )
        data = _parse_ai_json(response.text)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502

    semantic = apply_generated_section(semantic, section_key, data.get("content"))
    semantic = set_tiptap_projection(semantic, semantic_to_tiptap_doc(semantic))
    document.editor_json = semantic
    document.tiptap_json = get_tiptap_projection(semantic)
    db.session.commit()
    return jsonify({"ok": True, "section_key": section_key, "content": data.get("content"), "warnings": data.get("warnings", [])})


@clp_editor_bp.get("/api/clp-documents/<int:document_id>/versions")
@login_required
@roles_required("teacher")
def list_versions(document_id):
    document, error = _owned_document_or_404(document_id)
    if error:
        return error
    rows = db.session.execute(
        db.text(
            """
            SELECT version_number, change_summary, created_at
            FROM clp_document_versions
            WHERE document_id = :id
            ORDER BY version_number DESC
            """
        ),
        {"id": document.id},
    ).mappings().all()
    return jsonify({"ok": True, "versions": [dict(row) for row in rows]})


@clp_editor_bp.post("/api/clp-documents/<int:document_id>/versions")
@login_required
@roles_required("teacher")
def create_version(document_id):
    document, error = _owned_document_or_404(document_id)
    if error:
        return error
    summary = (request.get_json(silent=True) or {}).get("change_summary") or "Manual version"
    version_number = _save_editor_version(document, summary)
    db.session.commit()
    return jsonify({"ok": True, "version_number": version_number})


@clp_editor_bp.post("/api/clp-documents/<int:document_id>/versions/<int:version_number>/restore")
@login_required
@roles_required("teacher")
def restore_version(document_id, version_number):
    document, error = _owned_document_or_404(document_id)
    if error:
        return error
    row = db.session.execute(
        db.text(
            """
            SELECT editor_json, tiptap_json
            FROM clp_document_versions
            WHERE document_id = :id AND version_number = :version
            """
        ),
        {"id": document.id, "version": version_number},
    ).mappings().first()
    if not row:
        return jsonify({"ok": False, "error": "Version not found."}), 404
    document.editor_json = row["editor_json"]
    document.tiptap_json = row["tiptap_json"]
    document.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({"ok": True})


@clp_editor_bp.post("/api/clp-documents/<int:document_id>/import-docx")
@login_required
@roles_required("teacher")
def import_docx(document_id):
    document, error = _owned_document_or_404(document_id)
    if error:
        return error
    upload = request.files.get("file")
    if not upload or not upload.filename.lower().endswith(".docx"):
        return jsonify({"ok": False, "error": "A .docx file is required."}), 400
    try:
        if is_tiptap_conversion_configured():
            converted = import_docx_to_tiptap(upload.stream, upload.filename)
            semantic = normalize_semantic_clp(
                document.editor_json,
                title=document.title,
                department=document.department,
                owner_user_id=document.owner_id,
                plan_id=document.id,
            )
            set_tiptap_projection(semantic, converted["doc"])
            semantic.setdefault("validation", {}).setdefault("warnings", [])
            semantic["validation"]["warnings"].extend(converted.get("warnings") or [])
            semantic["validation"]["warnings"].append(
                "Imported with TipTap Cloud Conversion. Semantic CLP field mapping remains manual for this feasibility test."
            )
        else:
            semantic = import_docx_to_semantic(
                upload.stream,
                title=document.title,
                department=document.department,
                owner_user_id=document.owner_id,
                plan_id=document.id,
            )
        document.editor_json = semantic
        document.tiptap_json = get_tiptap_projection(semantic)
        document.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        try:
            upsert_tiptap_document(document_identifier(document.id), document.tiptap_json)
        except Exception as exc:
            current_app.logger.warning("TipTap document server sync failed for CLP document %s: %s", document.id, exc)
    except Exception as exc:
        db.session.rollback()
        return _json_exception("Failed to import DOCX.", exc)
    return jsonify(
        {
            "ok": True,
            "warnings": semantic.get("validation", {}).get("warnings", []),
            "document": {
                "id": document.id,
                "editor_json": document.editor_json,
                "tiptap_json": document.tiptap_json,
            },
        }
    )


@clp_editor_bp.post("/api/clp-documents/<int:document_id>/export-docx")
@login_required
@roles_required("teacher")
def export_docx(document_id):
    document, error = _owned_document_or_404(document_id)
    if error:
        return error
    semantic = normalize_semantic_clp(document.editor_json, title=document.title, department=document.department, owner_user_id=document.owner_id, plan_id=document.id)
    try:
        if is_tiptap_conversion_configured():
            buffer = export_tiptap_to_docx_bytes(get_tiptap_projection(semantic))
        else:
            buffer = export_semantic_to_docx_bytes(semantic)
            if not buffer.getbuffer().nbytes:
                buffer = export_tiptap_projection_to_docx_bytes(semantic)
    except Exception as exc:
        return _json_exception("Failed to export DOCX.", exc)
    return send_file(buffer, mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document", as_attachment=True, download_name=f"{document.title or 'clp'}.docx")


@clp_editor_bp.post("/api/clp-documents/<int:document_id>/export-pdf")
@login_required
@roles_required("teacher")
def export_pdf(document_id):
    document, error = _owned_document_or_404(document_id)
    if error:
        return error
    semantic = normalize_semantic_clp(document.editor_json, title=document.title, department=document.department, owner_user_id=document.owner_id, plan_id=document.id)
    try:
        if is_tiptap_conversion_configured():
            pdf_buffer = export_tiptap_to_pdf_bytes(get_tiptap_projection(semantic))
        else:
            pdf_buffer = convert_docx_bytes_to_pdf(export_semantic_to_docx_bytes(semantic))
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 501
    pdf_buffer.seek(0)
    return send_file(pdf_buffer, mimetype="application/pdf", as_attachment=True, download_name=f"{document.title or 'clp'}.pdf")
