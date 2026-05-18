# app/utils.py

import os
import json
from io import BytesIO
from datetime import datetime, timedelta, timezone
import jwt
import httpx
import time
import traceback
from psycopg2 import errors as psycopg_errors, sql
from flask import current_app, session, request, has_app_context, has_request_context, send_file
from docx import Document
from app.compat_supabase import create_client, delete_storage_paths, write_storage_bytes, get_storage_full_path
from app.services.docx_service import replace_placeholders, flatten_json, get_template_filepath

from app import (
    supabase, STORAGE_BUCKET_NAME, ALLOWED_EXTENSIONS, 
    PROGRAM_OUTCOMES, COURSE_OUTCOMES, 
    INSTITUTIONAL_OUTCOMES_HEADERS, PROGRAM_OUTCOMES_HEADERS,
    cache, executor,
    )


# ── User-friendly error messages ──

_KNOWN_DB_ERRORS = {
    "duplicate key value violates unique constraint": "already exists. Please use a different value.",
    "violates foreign key constraint": "is linked to another record that doesn't exist.",
    "violates not-null constraint": "is missing a required field.",
    "could not serialize access": "was saved by another user at the same time. Please try again.",
    "connection already closed": "Database connection was lost. Please try again.",
    "SSL connection has been closed": "Database connection was lost. Please try again.",
    "timeout": "took too long to respond. Please try again.",
    "permission denied": "You don't have permission for this action.",
    "relation.*does not exist": "System configuration error. Please contact support.",
}


def friendly_error(exc, fallback="Something went wrong. Please try again."):
    """Convert a database or generic exception into a user-friendly message."""
    msg = str(exc).lower()
    for pattern, hint in _KNOWN_DB_ERRORS.items():
        import re
        if re.search(pattern, msg):
            # Try to extract the field/table name from the error
            field_match = re.search(r'[("]?(\w+[\._]?\w*)[)"]?', msg)
            field = field_match.group(1) if field_match else ""
            if field and field not in ("", "key", "table", "column"):
                return f"{field.title().replace('_', ' ')} {hint}"
            return f"A record {hint}"
    return str(exc) if str(exc) else fallback


# --- JWT & AUTH HELPERS ---

def generate_jwt_token(payload):
    onlyoffice_jwt_secret = current_app.config.get("ONLYOFFICE_JWT_SECRET", "")
    if not onlyoffice_jwt_secret: return None
    try:
        # Create a copy to avoid modifying the original config passed to the template
        # We remove 'exp' because ONLYOFFICE compares the token payload against the cleartext config;
        # if 'exp' is in the token but not the JS config, verification fails (Error -23).
        data = payload.copy()
        token = jwt.encode(data, onlyoffice_jwt_secret, algorithm='HS256')
        # Ensure we return a string (PyJWT 2.0+ returns str, older versions return bytes)
        return token.decode('utf-8') if isinstance(token, bytes) else token
    except Exception as e:
        current_app.logger.error(f"Error generating JWT: {e}")
        return None


ONLYOFFICE_AI_PLUGIN_GUID = "asc.{9DC93CDB-B576-4F0C-B55E-FCC9C48DD007}"
ONLYOFFICE_AI_PLUGIN_CONFIG_URL = "https://onlyoffice.github.io/sdkjs-plugins/content/ai/config.json"
ONLYOFFICE_DEFAULT_AI_MODEL = "models/gemini-2.5-flash"
ONLYOFFICE_SERVER_MANAGED_AI_KEY = "server-managed"


def normalize_onlyoffice_gemini_model_id(model_name):
    model_name = (model_name or ONLYOFFICE_DEFAULT_AI_MODEL).strip()
    if not model_name:
        return ONLYOFFICE_DEFAULT_AI_MODEL
    if model_name.startswith("models/"):
        return model_name
    return f"models/{model_name}"


def _onlyoffice_force_model_overrides_enabled():
    # Preserve plugin-local model/assistant state by default. Set this flag only
    # when server-side model/action defaults must overwrite client state.
    return bool(current_app.config.get("ONLYOFFICE_FORCE_AI_PLUGIN_MODEL_OVERRIDES", False))


def build_onlyoffice_ai_plugin_settings():
    public_app_url = (current_app.config.get("PUBLIC_APP_URL") or "").rstrip("/")
    if not public_app_url:
        return None

    proxy_url = f"{public_app_url}/onlyoffice/ai-proxy"
    settings = {
        "version": 3,
        "timeout": "5m",
        "proxy": proxy_url,
        "allowedCorsOrigins": [
            "https://onlyoffice.github.io",
            "https://onlyoffice-plugins.github.io",
            public_app_url,
            "https://aipclpms-of.otakunity.com",
            "http://aipclpms-of.otakunity.com",
        ]
    }

    if _onlyoffice_force_model_overrides_enabled():
        configured_model = normalize_onlyoffice_gemini_model_id(
            get_system_prompt(current_app.config.get('SUPABASE_SERVICE') or supabase, 'gemini_model', ONLYOFFICE_DEFAULT_AI_MODEL)
        )
        model_ids = [
            configured_model,
            "models/gemini-2.5-flash",
            "models/gemini-2.5-pro",
            "models/gemini-1.5-pro",
            "models/gemini-1.5-flash",
        ]
        deduped_model_ids = []
        for model_id in model_ids:
            if model_id not in deduped_model_ids:
                deduped_model_ids.append(model_id)

        settings["providers"] = {
            "Google-Gemini": {
                "name": "Google-Gemini",
                "url": "https://generativelanguage.googleapis.com",
                "key": ONLYOFFICE_SERVER_MANAGED_AI_KEY,
                "addon": "v1beta",
                "models": [
                    {
                        "id": model_id,
                        "object": "model",
                        "name": model_id.rsplit('/', 1)[-1],
                        "endpoints": [1],
                        "options": { "max_input_tokens": 1048576 }
                    }
                    for model_id in deduped_model_ids
                ]
            }
        }
        
        models_list = []
        for m in settings["providers"]["Google-Gemini"]["models"]:
            models_list.append({
                "capabilities": 129,
                "provider": "Google-Gemini",
                "name": "Google-Gemini [" + m["name"] + "]",
                "id": m["id"]
            })
            
        settings["models"] = models_list
        settings["actions"] = {
            "Chat": {
                "name": "Chatbot",
                "icon": "ask-ai",
                "model": configured_model,
                "capabilities": 1,
            },
            "Summarization": {
                "name": "Summarization",
                "icon": "summarization",
                "model": configured_model,
                "capabilities": 1,
            },
            "Translation": {
                "name": "Translation",
                "icon": "translation",
                "model": configured_model,
                "capabilities": 1,
            },
            "TextAnalyze": {
                "name": "Text analysis",
                "icon": "text-analysis-ai",
                "model": configured_model,
                "capabilities": 1,
            },
            "Vision": {
                "name": "Vision",
                "icon": "vision-ai",
                "model": configured_model,
                "capabilities": 128,
            },
        }

    return settings


def attach_onlyoffice_ai_plugin(config):
    # Let the native OnlyOffice AI plugin keep its own saved state by default.
    # Forced per-document injection is available as an opt-in escape hatch only.
    if not current_app.config.get("ONLYOFFICE_FORCE_AI_PLUGIN_SETTINGS", False):
        return config

    settings = build_onlyoffice_ai_plugin_settings()
    if not settings:
        return config

    editor_config = config.setdefault("editorConfig", {})
    plugins = editor_config.setdefault("plugins", {})

    plugins_data = plugins.setdefault("pluginsData", [])
    if ONLYOFFICE_AI_PLUGIN_CONFIG_URL not in plugins_data:
        plugins_data.append(ONLYOFFICE_AI_PLUGIN_CONFIG_URL)

    autostart = plugins.setdefault("autostart", [])
    if ONLYOFFICE_AI_PLUGIN_GUID not in autostart:
        autostart.append(ONLYOFFICE_AI_PLUGIN_GUID)

    options = plugins.setdefault("options", {})
    plugin_options = options.setdefault(ONLYOFFICE_AI_PLUGIN_GUID, {})
    plugin_options["aiPluginSettings"] = json.dumps(settings, separators=(",", ":"))
    return config

def parse_supabase_timestamp(data_list, field_name='timestamp'):
    for item in data_list:
        if item.get(field_name):
            iso_string = item[field_name].split('+')[0].split('.')[0]
            try: item[field_name] = datetime.fromisoformat(iso_string)
            except ValueError: pass
    return data_list


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def create_notification(user_id, message, reference_type=None, reference_id=None):
    try: 
        client = current_app.config.get('SUPABASE_SERVICE') or supabase
        record = {'user_id': user_id, 'message': message}
        if reference_type is not None:
            record['reference_type'] = reference_type
        if reference_id is not None:
            record['reference_id'] = reference_id
        client.table('notifications').insert(record).execute()
        try:
            cache.delete(f"unread_notifications:{user_id}")
        except Exception:
            pass
    except Exception as e: current_app.logger.error(f"Failed to create notification: {e}")


def delete_clp_dependencies(plan_id, local_supabase=None):
    client = get_server_supabase_client(local_supabase)
    if not client:
        return

    dependent_tables = [
        'ai_usage_log',
        'background_tasks',
        'clp_comments',
        'clp_history',
        'clp_mapping_entries',
        'clp_versions',
        'system_events',
    ]

    for table_name in dependent_tables:
        client.table(table_name).delete().eq('plan_id', plan_id).execute()


def delete_clp_with_dependencies(plan_id, local_supabase=None):
    client = get_server_supabase_client(local_supabase)
    if not client:
        return False

    dependent_tables = [
        'ai_usage_log',
        'background_tasks',
        'clp_comments',
        'clp_history',
        'clp_mapping_entries',
        'clp_versions',
        'system_events',
    ]

    if hasattr(client, 'get_connection'):
        with client.get_connection() as conn:
            with conn.cursor() as cur:
                for table_name in dependent_tables:
                    cur.execute(
                        sql.SQL("DELETE FROM {} WHERE plan_id = %s").format(sql.Identifier(table_name)),
                        (plan_id,),
                    )
                cur.execute("DELETE FROM course_learning_plans WHERE id = %s RETURNING id", (plan_id,))
                deleted = cur.fetchone()
            conn.commit()
        return bool(deleted)

    delete_clp_dependencies(plan_id, local_supabase=client)
    result = client.table('course_learning_plans').delete().eq('id', plan_id).execute()
    return bool(result.data)


def log_system_event(category, level, message, details=None, user_id=None, plan_id=None, local_supabase=None):
    if not has_app_context():
        return

    payload = {
        'category': category,
        'level': level,
        'message': message,
        'details': details or {},
        'user_id': user_id,
        'plan_id': plan_id,
    }
    try:
        client = get_server_supabase_client(local_supabase)
        client.table('system_events').insert(payload).execute()
    except Exception as e:
        current_app.logger.warning(f"System event log failed: {e}")


def count_active_admins(local_supabase=None):
    client = get_server_supabase_client(local_supabase)
    if not client:
        return 0
    try:
        res = (
            client.table('users')
            .select('id', count='exact')
            .eq('role', 'admin')
            .eq('approved', True)
            .eq('active', True)
            .execute()
        )
        return res.count or 0
    except Exception:
        return 0


def get_user_dependency_summary(user_id, local_supabase=None):
    client = get_server_supabase_client(local_supabase)
    if not client or not user_id:
        return {}

    dependency_map = {
        'course_learning_plans': ('user_id', 'plans'),
        'background_tasks': ('user_id', 'tasks'),
        'notifications': ('user_id', 'notifications'),
        'audit_logs': ('user_id', 'audit_logs'),
        'ai_usage_log': ('user_id', 'ai_logs'),
        'clp_history': ('actor_id', 'history_actions'),
        'clp_versions': ('actor_id', 'version_actions'),
        'clp_comments': ('user_id', 'comments'),
    }
    summary = {}
    for table_name, (field_name, label) in dependency_map.items():
        try:
            res = client.table(table_name).select('id', count='exact').eq(field_name, user_id).execute()
            summary[label] = res.count or 0
        except Exception:
            summary[label] = 0
    return summary


def _batched_dependency_counts(client, table_name, field_name, ids):
    normalized_ids = [str(value) for value in ids if value is not None]
    if not normalized_ids:
        return {}

    if hasattr(client, 'get_connection'):
        with client.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL(
                        "SELECT {}::text AS ref_id, COUNT(*)::int AS total "
                        "FROM {} "
                        "WHERE {}::text = ANY(%s) "
                        "GROUP BY {}::text"
                    ).format(
                        sql.Identifier(field_name),
                        sql.Identifier(table_name),
                        sql.Identifier(field_name),
                        sql.Identifier(field_name),
                    ),
                    (normalized_ids,),
                )
                return {row[0]: row[1] for row in cur.fetchall()}

    rows = client.table(table_name).select(field_name).in_(field_name, normalized_ids).execute().data or []
    counts = {}
    for row in rows:
        key = str(row.get(field_name))
        counts[key] = counts.get(key, 0) + 1
    return counts


def get_user_dependency_summaries(user_ids, local_supabase=None):
    client = get_server_supabase_client(local_supabase)
    normalized_ids = [str(user_id) for user_id in user_ids if user_id]
    if not client or not normalized_ids:
        return {}

    dependency_map = {
        'course_learning_plans': ('user_id', 'plans'),
        'background_tasks': ('user_id', 'tasks'),
        'notifications': ('user_id', 'notifications'),
        'audit_logs': ('user_id', 'audit_logs'),
        'ai_usage_log': ('user_id', 'ai_logs'),
        'clp_history': ('actor_id', 'history_actions'),
        'clp_versions': ('actor_id', 'version_actions'),
        'clp_comments': ('user_id', 'comments'),
    }

    summaries = {
        user_id: {label: 0 for _, label in dependency_map.values()}
        for user_id in normalized_ids
    }

    for table_name, (field_name, label) in dependency_map.items():
        counts = _batched_dependency_counts(client, table_name, field_name, normalized_ids)
        for user_id, count in counts.items():
            if user_id in summaries:
                summaries[user_id][label] = count

    return summaries


def get_clp_dependency_summary(plan_id, local_supabase=None):
    client = get_server_supabase_client(local_supabase)
    if not client or not plan_id:
        return {}

    dependency_map = {
        'clp_history': 'history_entries',
        'clp_versions': 'versions',
        'clp_comments': 'comments',
        'clp_mapping_entries': 'mappings',
        'background_tasks': 'tasks',
        'ai_usage_log': 'ai_runs',
    }
    summary = {}
    for table_name, label in dependency_map.items():
        try:
            res = client.table(table_name).select('id', count='exact').eq('plan_id', plan_id).execute()
            summary[label] = res.count or 0
        except Exception:
            summary[label] = 0
    return summary


def get_clp_dependency_summaries(plan_ids, local_supabase=None):
    client = get_server_supabase_client(local_supabase)
    normalized_ids = [str(plan_id) for plan_id in plan_ids if plan_id is not None]
    if not client or not normalized_ids:
        return {}

    dependency_map = {
        'clp_history': 'history_entries',
        'clp_versions': 'versions',
        'clp_comments': 'comments',
        'clp_mapping_entries': 'mappings',
        'background_tasks': 'tasks',
        'ai_usage_log': 'ai_runs',
    }

    summaries = {
        plan_id: {label: 0 for label in dependency_map.values()}
        for plan_id in normalized_ids
    }

    for table_name, label in dependency_map.items():
        counts = _batched_dependency_counts(client, table_name, 'plan_id', normalized_ids)
        for plan_id, count in counts.items():
            if plan_id in summaries:
                summaries[plan_id][label] = count

    return summaries


def get_department_dependency_summary(dept_id, dept_name=None, local_supabase=None):
    client = get_server_supabase_client(local_supabase)
    if not client:
        return {}

    summary = {}
    checks = [
        ('templates', 'department_id', dept_id, 'templates'),
        ('program_outcomes', 'department_id', dept_id, 'program_outcomes'),
        ('course_outcomes', 'department_id', dept_id, 'course_outcomes'),
        ('knowledge_base', 'department_id', dept_id, 'knowledge_base_docs'),
    ]

    for table_name, field_name, value, label in checks:
        try:
            res = client.table(table_name).select('id', count='exact').eq(field_name, value).execute()
            summary[label] = res.count or 0
        except Exception:
            summary[label] = 0

    if dept_name:
        for table_name, field_name, label in (
            ('users', 'assigned_department', 'assigned_users'),
            ('course_learning_plans', 'department', 'plans'),
        ):
            try:
                res = client.table(table_name).select('id', count='exact').eq(field_name, dept_name).execute()
                summary[label] = res.count or 0
            except Exception:
                summary[label] = 0
    else:
        summary['assigned_users'] = 0
        summary['plans'] = 0

    return summary


def get_outcome_dependency_summary(outcome_type, code, local_supabase=None):
    client = get_server_supabase_client(local_supabase)
    if not client or not code:
        return {}

    source_types = {
        'program': 'PO_IO',
        'course': 'CO_PO',
        'institutional': 'PO_IO',
    }
    summary = {'mappings_as_source': 0, 'mappings_as_target': 0}

    try:
        source_type = source_types.get(outcome_type)
        if source_type:
            source_res = (
                client.table('clp_mapping_entries')
                .select('id', count='exact')
                .eq('source_type', source_type)
                .eq('source_code', code)
                .execute()
            )
            summary['mappings_as_source'] = source_res.count or 0
    except Exception:
        pass

    try:
        target_res = client.table('clp_mapping_entries').select('id', count='exact').eq('target_code', code).execute()
        summary['mappings_as_target'] = target_res.count or 0
    except Exception:
        pass

    return summary


def classify_admin_exception(exc):
    message = str(exc).strip() or "An unexpected error occurred."
    lowered = message.lower()
    if isinstance(exc, ValueError):
        return 'validation', message
    if isinstance(exc, FileNotFoundError):
        return 'storage', "The referenced file is missing from local storage."
    if isinstance(exc, (psycopg_errors.ForeignKeyViolation, psycopg_errors.RestrictViolation)):
        return 'dependency', "This item is still referenced elsewhere and cannot be removed yet."
    if isinstance(exc, (psycopg_errors.UniqueViolation, psycopg_errors.NotNullViolation, psycopg_errors.CheckViolation)):
        return 'validation', message
    if 'referenced' in lowered or 'foreign key' in lowered or 'violates foreign key constraint' in lowered:
        return 'dependency', "This item is still referenced elsewhere and cannot be removed yet."
    if 'duplicate' in lowered or 'already exists' in lowered or 'unique' in lowered:
        return 'validation', message
    if 'storage' in lowered or 'file' in lowered:
        return 'storage', message
    if 'connection' in lowered or 'database' in lowered or 'timeout' in lowered:
        return 'service', "A required service is temporarily unavailable. Please try again."
    return 'service', message


def make_admin_feedback(action, exc=None, detail=None):
    if exc is None:
        message = detail or f"{action} completed successfully."
        return {'level': 'success', 'kind': 'success', 'message': message}

    kind, message = classify_admin_exception(exc)
    if detail:
        message = f"{message} {detail}".strip()
    level_map = {
        'validation': 'warning',
        'dependency': 'warning',
        'storage': 'danger',
        'service': 'danger',
    }
    return {'level': level_map.get(kind, 'danger'), 'kind': kind, 'message': message}


def get_current_user_department():
    profile = get_current_user_profile()
    if not profile:
        return None
    return profile.get('assigned_department')


def get_onlyoffice_base_url(internal=False):
    if internal:
        internal_url = (
            (current_app.config.get('ONLYOFFICE_INTERNAL_APP_URL') if has_app_context() else None)
            or os.environ.get('ONLYOFFICE_INTERNAL_APP_URL')
        )
        if internal_url:
            return internal_url.rstrip('/')

    public_url = (
        (current_app.config.get('ONLYOFFICE_CALLBACK_URL') if has_app_context() else None)
        or os.environ.get('ONLYOFFICE_CALLBACK_URL')
        or (current_app.config.get('PUBLIC_APP_URL') if has_app_context() else None)
        or os.environ.get('PUBLIC_APP_URL')
    )
    if public_url:
        return public_url.rstrip('/')
    if has_request_context():
        return request.host_url.rstrip('/')
    return "http://127.0.0.1:3000"


def user_can_access_clp(plan, role=None, user_id=None, department=None):
    if not plan:
        return False

    role = role or session.get('role')
    user_id = str(user_id or session.get('user_id') or '')
    department = department if department is not None else get_current_user_department()

    plan_user_id = str(plan.get('user_id') or '')
    plan_department = plan.get('department')
    plan_status = plan.get('status')

    if role == 'admin':
        return True
    if role == 'teacher':
        if plan_user_id and plan_user_id == user_id:
            return True
        return bool(department) and plan_status == 'approved' and plan_department == department
    if role == 'dean':
        return bool(department) and plan_department == department
    return False


def user_can_delete_clp(plan, role=None, user_id=None, department=None):
    if not plan:
        return False

    role = role or session.get('role')
    user_id = str(user_id or session.get('user_id') or '')
    department = department if department is not None else get_current_user_department()

    plan_user_id = str(plan.get('user_id') or '')
    plan_department = plan.get('department')

    if role == 'admin':
        return True
    if role == 'teacher':
        return bool(plan_user_id) and plan_user_id == user_id
    if role == 'dean':
        return bool(department) and plan_department == department
    return False


def classify_workflow_exception(exc):
    message = str(exc).strip() or "An unexpected error occurred."
    lowered = message.lower()

    if isinstance(exc, PermissionError):
        return 'forbidden', "You do not have permission to perform this action."
    if isinstance(exc, ValueError):
        return 'validation', message
    if isinstance(exc, FileNotFoundError):
        return 'storage', "The referenced file is missing from local storage."
    if isinstance(exc, (psycopg_errors.ForeignKeyViolation, psycopg_errors.RestrictViolation)):
        return 'dependency', "This item is still referenced elsewhere and cannot be changed yet."
    if isinstance(exc, (psycopg_errors.UniqueViolation, psycopg_errors.NotNullViolation, psycopg_errors.CheckViolation)):
        return 'validation', message
    if 'forbidden' in lowered or 'unauthorized' in lowered or 'permission' in lowered:
        return 'forbidden', "You do not have permission to perform this action."
    if 'referenced' in lowered or 'foreign key' in lowered or 'violates foreign key constraint' in lowered:
        return 'dependency', "This item is still referenced elsewhere and cannot be changed yet."
    if 'missing' in lowered or 'storage' in lowered or 'file' in lowered:
        return 'storage', message
    if 'connection' in lowered or 'database' in lowered or 'timeout' in lowered or 'temporarily unavailable' in lowered:
        return 'service', "A required service is temporarily unavailable. Please try again."
    return 'service', "Something went wrong while processing your request. Please try again."


def make_workflow_feedback(action, exc=None, detail=None, partial=False):
    if exc is None:
        message = detail or f"{action} completed successfully."
        return {
            'level': 'warning' if partial else 'success',
            'kind': 'partial_success' if partial else 'success',
            'message': message,
        }

    kind, message = classify_workflow_exception(exc)
    if detail:
        message = f"{message} {detail}".strip()
    level_map = {
        'validation': 'warning',
        'forbidden': 'danger',
        'dependency': 'warning',
        'storage': 'danger',
        'service': 'danger',
    }
    return {'level': level_map.get(kind, 'danger'), 'kind': kind, 'message': message}


def cleanup_storage_after_commit(filename, *, context, user_id=None, plan_id=None, local_supabase=None):
    if not filename:
        return True

    try:
        delete_storage_paths(STORAGE_BUCKET_NAME, [filename])
        return True
    except Exception as exc:
        if has_app_context():
            current_app.logger.warning(f"{context} cleanup failed for {filename}: {exc}")
        log_system_event(
            'storage',
            'warning',
            f"{context} cleanup failed",
            details={'filename': filename, 'error': str(exc)},
            user_id=user_id,
            plan_id=plan_id,
            local_supabase=local_supabase,
        )
        return False


def storage_file_exists(bucket, path):
    if not path:
        return False
    full_path = get_storage_full_path(bucket, path)
    return os.path.exists(full_path)


def stream_storage_file(bucket, path, download_name, *, mimetype, inline=True, cache_seconds=300):
    full_path = get_storage_full_path(bucket, path)
    if not os.path.exists(full_path):
        raise FileNotFoundError(path)

    response = send_file(
        full_path,
        mimetype=mimetype,
        as_attachment=not inline,
        download_name=download_name,
        conditional=True,
        etag=True,
        max_age=cache_seconds,
    )
    if inline:
        response.headers["Content-Disposition"] = f'inline; filename="{download_name}"'
    response.headers["Cache-Control"] = f'private, max-age={cache_seconds}, stale-while-revalidate=60'
    return response


def log_document_timing(event, started_at, **details):
    if not has_app_context():
        return

    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
    payload = {'duration_ms': duration_ms}
    for key, value in details.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            payload[key] = value
        else:
            payload[key] = str(value)
    current_app.logger.info("DOCUMENT_TIMING %s %s", event, json.dumps(payload, sort_keys=True))


def _coerce_content_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def ensure_plan_document_file(plan, local_supabase=None):
    client = get_server_supabase_client(local_supabase)
    if not client:
        raise FileNotFoundError("No database client is available.")
    if not plan:
        raise FileNotFoundError("Plan not found.")

    filename = plan.get('filename')
    if filename:
        try:
            read_storage = current_app.config.get('READ_STORAGE_BYTES_HELPER')
        except Exception:
            read_storage = None
        try:
            from app.compat_supabase import read_storage_bytes
            read_storage_bytes(STORAGE_BUCKET_NAME, filename)
            return filename
        except FileNotFoundError:
            pass

    content_data = _coerce_content_dict(plan.get('content'))
    if not content_data:
        raise FileNotFoundError("The plan document is missing and cannot be rebuilt because no CLP content is available.")

    # For beta-copilot documents, the recovery path below that uses
    # flatten_json + replace_placeholders on a stock template produces
    # garbled output (nested JSON objects flatten into string dumps).
    # Instead, raise a clear error so the user can re-insert from the
    # beta review page, where the full profile/placeholder renderer
    # can produce a correct document.
    if content_data.get('beta_document_filename'):
        beta_filename = content_data['beta_document_filename']
        try:
            from app.compat_supabase import read_storage_bytes
            read_storage_bytes(STORAGE_BUCKET_NAME, beta_filename)
            # File exists under the beta path but not under plan.filename.
            # Point the plan at the beta filename and carry on.
            client.table('course_learning_plans').update({
                'filename': beta_filename,
            }).eq('id', plan.get('id')).execute()
            return beta_filename
        except FileNotFoundError:
            pass
        raise FileNotFoundError(
            "The beta copilot document is missing from storage and cannot be "
            "rebuilt automatically. Please re-insert the draft from the "
            "beta review page to regenerate the document."
        )

    department_name = plan.get('department')
    dept_id = get_department_id_by_name(department_name, local_supabase=client)
    template_key = None

    if dept_id:
        tmpl_res = client.table('templates').select('filename').eq('department_id', dept_id).limit(1).execute()
        if tmpl_res.data:
            template_key = tmpl_res.data[0].get('filename')
    if not template_key:
        def_res = client.table('templates').select('filename').eq('is_default', True).limit(1).execute()
        if def_res.data:
            template_key = def_res.data[0].get('filename')

    if template_key:
        template_bytes = client.storage.from_(STORAGE_BUCKET_NAME).download(template_key)
    else:
        with open(get_template_filepath(), 'rb') as handle:
            template_bytes = handle.read()

    doc = Document(BytesIO(template_bytes))
    flat = flatten_json(content_data)
    flat.setdefault('date_today', datetime.now().strftime("%B %Y"))
    doc = replace_placeholders(doc, flat)

    output = BytesIO()
    doc.save(output)
    output.seek(0)

    subject_name = (plan.get('subject') or f"plan_{plan.get('id')}").replace(' ', '_')
    user_id = plan.get('user_id') or session.get('user_id') or 'recovered'
    new_filename = f"{user_id}/{subject_name}_{int(time.time())}.docx"
    write_storage_bytes(STORAGE_BUCKET_NAME, new_filename, output.read())

    client.table('course_learning_plans').update({
        'filename': new_filename,
        'upload_type': 'file_upload',
    }).eq('id', plan['id']).execute()

    log_system_event(
        'storage',
        'warning',
        'Plan document rebuilt from stored CLP content',
        details={'plan_id': plan['id'], 'old_filename': filename, 'new_filename': new_filename},
        user_id=user_id,
        plan_id=plan['id'],
        local_supabase=client,
    )
    return new_filename


def get_server_supabase_client(local_supabase=None):
    if has_app_context():
        client = current_app.config.get('SUPABASE_SERVICE') or current_app.config.get('SUPABASE_CLIENT')
        if client:
            return client
    return local_supabase or supabase


def get_system_settings_map(local_supabase=None, force_refresh=False):
    cache_key = "system_settings_map"
    fallback_cache_key = "system_settings_map_last_known"
    retry_backoff_key = "system_settings_retry_backoff"

    try:
        if not force_refresh:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached
    except Exception:
        pass

    try:
        if not force_refresh and cache.get(retry_backoff_key):
            fallback = cache.get(fallback_cache_key)
            return fallback if fallback is not None else {}
    except Exception:
        pass

    try:
        client = get_server_supabase_client(local_supabase)
        if not client:
            return {}

        res = client.table('system_settings').select('key, value').execute()
        settings = {
            row['key']: row.get('value')
            for row in (res.data or [])
            if row.get('key')
        }

        try:
            cache.set(cache_key, settings, timeout=300)
            cache.set(fallback_cache_key, settings, timeout=3600)
            for key, value in settings.items():
                cache.set(f"sys_setting:{key}", value, timeout=300)
                cache.set(f"sys_setting_last_known:{key}", value, timeout=3600)
        except Exception:
            pass

        return settings
    except Exception as e:
        is_network_error = isinstance(e, httpx.HTTPError) or 'Temporary failure in name resolution' in str(e)
        try:
            cache.set(retry_backoff_key, True, timeout=30 if is_network_error else 10)
        except Exception:
            pass

        try:
            fallback = cache.get(fallback_cache_key)
            if fallback is not None:
                return fallback
        except Exception:
            pass

        if has_app_context():
            current_app.logger.warning(f"Error fetching system settings bundle: {e}")

        return {}


def invalidate_system_settings_cache(keys=None):
    cache_keys = [
        'global_settings',
        'system_settings_map',
        'system_settings_map_last_known',
        'system_settings_retry_backoff',
    ]
    try:
        for cache_key in cache_keys:
            cache.delete(cache_key)
        for key in (keys or []):
            cache.delete(f"sys_setting:{key}")
            cache.delete(f"sys_setting_last_known:{key}")
            cache.delete(f"sys_setting_warned:{key}")
            cache.delete(f"sys_setting_retry_backoff:{key}")
    except Exception:
        pass


def get_department_records(local_supabase=None, force_refresh=False):
    cache_key = 'departments:records'
    try:
        if not force_refresh:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached
    except Exception:
        pass

    try:
        client = get_server_supabase_client(local_supabase)
        res = client.table('departments').select('id, name').order('name').execute()
        records = [
            {'id': row['id'], 'name': row['name']}
            for row in (res.data or [])
            if row.get('name') is not None
        ]
        try:
            cache.set(cache_key, records, timeout=600)
        except Exception:
            pass
        return records
    except Exception:
        return []


def get_department_id_by_name(department_name, local_supabase=None):
    if not department_name:
        return None

    for row in get_department_records(local_supabase=local_supabase):
        if row.get('name') == department_name:
            return row.get('id')
    return None


def get_department_choices(include_blank_label=None, use_ids=False, local_supabase=None):
    records = get_department_records(local_supabase=local_supabase)
    choices = [
        ((str(row['id']) if use_ids else row['name']), row['name'])
        for row in records
    ]
    if include_blank_label is not None:
        choices = [('', include_blank_label)] + choices
    return choices


def invalidate_department_caches(department_names=None):
    names = set(filter(None, department_names or []))
    if not names:
        try:
            names = {row.get('name') for row in get_department_records()}
        except Exception:
            names = set()

    try:
        cache.delete('departments:records')
        cache.delete('department_choices')
        cache.delete('department_id_choices')
        for name in names:
            cache.delete(f"dept_outcomes_bundle:{name}")
            cache.delete(f"department:signatories:{name}")
    except Exception:
        pass


def get_department_signatory_settings(department_name=None, department_id=None, local_supabase=None):
    if not department_name and not department_id:
        return {}

    cache_key = f"department:signatories:{department_id or department_name}"
    try:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    except Exception:
        pass

    try:
        client = get_server_supabase_client(local_supabase)
        query = client.table('departments').select(
            'id, name, dean_name, dean_title, program_coordinator_name, program_coordinator_title, vice_president_name, vice_president_title'
        )
        if department_id:
            res = query.eq('id', department_id).single().execute()
        else:
            res = query.eq('name', department_name).single().execute()
        data = res.data or {}
    except Exception:
        data = {}

    settings = {
        'dean_name': (data.get('dean_name') or '').strip() if data else '',
        'dean_title': (data.get('dean_title') or '').strip() if data else '',
        'program_coordinator_name': (data.get('program_coordinator_name') or '').strip() if data else '',
        'program_coordinator_title': (data.get('program_coordinator_title') or '').strip() if data else '',
        'vice_president_name': (data.get('vice_president_name') or '').strip() if data else '',
        'vice_president_title': (data.get('vice_president_title') or '').strip() if data else '',
    }
    try:
        cache.set(cache_key, settings, timeout=600)
    except Exception:
        pass
    return settings

def get_current_user_profile():
    user_id = session.get('user_id')
    if not user_id: return None
    cache_key = f"user_profile:{user_id}"
    try:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    except Exception:
        pass
    try:
        client = current_app.config.get('SUPABASE_SERVICE') or supabase
        res = client.table('users').select('*').eq('id', user_id).single().execute()
        profile = res.data
        try:
            cache.set(cache_key, profile, timeout=60)
        except Exception:
            pass
        return profile
    except: return None


def invalidate_user_profile_cache(user_id):
    if not user_id:
        return
    try:
        cache.delete(f"user_profile:{user_id}")
    except Exception:
        pass


def get_unread_notifications_count(user_id):
    if not user_id:
        return 0
    cache_key = f"unread_notifications:{user_id}"
    try:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    except Exception:
        pass

    try:
        client = current_app.config.get('SUPABASE_SERVICE') or supabase
        count_res = client.table('notifications').select('id', count='exact').eq('user_id', user_id).eq('is_read', False).execute()
        count = count_res.count or 0
        try:
            cache.set(cache_key, count, timeout=15)
        except Exception:
            pass
        return count
    except Exception:
        return 0


def invalidate_notifications_cache(user_id):
    if not user_id:
        return
    try:
        cache.delete(f"unread_notifications:{user_id}")
    except Exception:
        pass


def get_department_outcomes_bundle(department_name):
    if not department_name:
        return {
            'department_id': None,
            'program_outcomes': [],
            'course_outcomes': [],
            'institutional_outcomes': [],
            'institutional_headers': [],
            'program_headers': []
        }

    cache_key = f"dept_outcomes_bundle:{department_name}"
    try:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    except Exception:
        pass

    result = {
        'department_id': None,
        'program_outcomes': [],
        'course_outcomes': [],
        'institutional_outcomes': [],
        'institutional_headers': [],
        'program_headers': []
    }

    try:
        client = get_server_supabase_client()
        io_res = client.table('institutional_outcomes').select('code, description').order('id').execute()
        result['institutional_outcomes'] = io_res.data or []
        result['institutional_headers'] = [row['code'] for row in result['institutional_outcomes']]

        dept_id = get_department_id_by_name(department_name, local_supabase=client)
        if dept_id:
            result['department_id'] = dept_id
            po_res = client.table('program_outcomes').select('*').eq('department_id', dept_id).order('code').execute()
            co_res = client.table('course_outcomes').select('*').eq('department_id', dept_id).order('code').execute()
            result['program_outcomes'] = po_res.data or []
            result['course_outcomes'] = co_res.data or []
            result['program_headers'] = [row['code'] for row in result['program_outcomes']]

        try:
            cache.set(cache_key, result, timeout=600)
        except Exception:
            pass
    except Exception as e:
        if current_app:
            current_app.logger.warning(f"Failed to fetch department outcomes for '{department_name}': {e}")

    return result

def _build_audit_log_data(user_id, action, details=None):
    ip = None
    method = None
    endpoint = None
    if has_request_context():
        ip = request.remote_addr
        method = request.method
        endpoint = request.endpoint

    details_payload = details or {}
    if not isinstance(details_payload, dict):
        details_payload = {'message': str(details_payload)}

    return {
        'user_id': user_id,
        'action': action,
        'details': details_payload,
        'ip_address': ip,
        'method': method,
        'endpoint': endpoint,
        'event_type': details_payload.get('event_type'),
        'result': details_payload.get('result'),
        'status_code': details_payload.get('status_code'),
        'resource_id': details_payload.get('resource_id'),
        'latency_ms': details_payload.get('latency_ms')
    }


def _insert_audit_log(app_obj, log_data):
    try:
        with app_obj.app_context():
            client = current_app.config.get('SUPABASE_SERVICE') or supabase
            try:
                client.table('audit_logs').insert(log_data).execute()
            except Exception as e:
                if 'PGRST204' in str(e) or 'endpoint' in str(e):
                    details_payload = log_data.get('details') or {}
                    if isinstance(details_payload, dict):
                        details_payload['_method'] = log_data.get('method')
                        details_payload['_endpoint'] = log_data.get('endpoint')

                    fallback_log_data = dict(log_data)
                    fallback_log_data['details'] = details_payload
                    fallback_log_data.pop('method', None)
                    fallback_log_data.pop('endpoint', None)
                    client.table('audit_logs').insert(fallback_log_data).execute()
                else:
                    raise
    except Exception as e:
        try:
            app_obj.logger.error(f"Audit Log Failed: {e}")
        except Exception:
            pass


def log_audit(user_id, action, details=None):
    """Logs a user action to the audit_logs table."""
    if not has_app_context():
        return
    _insert_audit_log(current_app._get_current_object(), _build_audit_log_data(user_id, action, details))


def log_audit_async(user_id, action, details=None):
    """Schedules an audit log insert so user-facing requests are not blocked."""
    if not has_app_context():
        return

    app_obj = current_app._get_current_object()
    log_data = _build_audit_log_data(user_id, action, details)

    if app_obj.config.get('TESTING'):
        _insert_audit_log(app_obj, log_data)
        return

    executor.submit(_insert_audit_log, app_obj, log_data)

def check_ai_quota(user_id):
    """Checks if the user has exceeded their daily AI generation quota."""
    try:
        from app import supabase
        limit_str = get_system_prompt(supabase, 'daily_ai_limit', '5')
        limit = int(limit_str)
        
        # Check last 24 hours
        yesterday = (datetime.now() - timedelta(days=1)).isoformat()
        
        res = supabase.table('audit_logs').select('id', count='exact')\
            .eq('user_id', user_id)\
            .eq('action', 'CLP Creation Started')\
            .gte('timestamp', yesterday).execute()
        
        count = res.count or 0
        return count < limit, count, limit
    except Exception as e:
        if current_app:
            current_app.logger.error(f"Quota check error: {e}")
        return True, 0, 999

def log_clp_history(plan_id, actor_id, action, comment=None):
    try:
        supabase.table('clp_history').insert({'plan_id': plan_id, 'actor_id': actor_id, 'action': action, 'comment': comment}).execute()
    except Exception as e: current_app.logger.error(f"Failed to log history: {e}")

def get_system_prompt(local_supabase, key, default_text=""):
    """Fetches a system setting from the database with caching support."""
    cache_key = f"sys_setting:{key}"
    fallback_cache_key = f"sys_setting_last_known:{key}"
    try:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    except Exception: pass

    settings = get_system_settings_map(local_supabase=local_supabase)
    if key in settings:
        val = settings.get(key)
        if val is not None and str(val).strip() != "":
            return val

    try:
        fallback = cache.get(fallback_cache_key)
        if fallback is not None:
            return fallback
    except Exception:
        pass

    return default_text

# --- TASK ORCHESTRATION (Reliable Task Queue Section) ---
from app.services.task_queue import TaskQueue

def start_clp_data_generation(plan_id, user_id, course_data):
    """Enqueues a task to generate CLP data."""
    try:
        TaskQueue.enqueue('generate_clp', course_data, user_id, plan_id)
    except Exception as e:
        current_app.logger.error(f"Failed to enqueue generation: {e}")

def start_clp_refinement(plan_id, user_id, original_data, updated_data):
    """Enqueues a task to refine CLP data based on user edits."""
    try:
        payload = {'original': original_data, 'updated': updated_data}
        TaskQueue.enqueue('refine_clp', payload, user_id, plan_id)
    except Exception as e:
        current_app.logger.error(f"Failed to enqueue refinement: {e}")

def start_clp_finalization(plan_id, user_id):
    """Enqueues a task to finalize the CLP document."""
    try:
        TaskQueue.enqueue('finalize_clp', {}, user_id, plan_id)
    except Exception as e:
        current_app.logger.error(f"Failed to enqueue finalization: {e}")

def start_apply_validation_fixes(plan_id, user_id, current_content, validation_data):
    """Enqueues a task to apply AI validation fixes."""
    try:
        payload = {'content': current_content, 'validation': validation_data}
        TaskQueue.enqueue('apply_fixes', payload, user_id, plan_id)
    except Exception as e:
        current_app.logger.error(f"Failed to enqueue validation fixes: {e}")

def handle_system_error(e, context="System Error", user_id=None, level="error"):
    """
    Centralized error handler for professional logging and audit trails.
    """
    error_msg = str(e)
    tb = traceback.format_exc()
    
    if current_app:
        if level == "error":
            current_app.logger.error(f"[{context}] {error_msg}\n{tb}")
        else:
            current_app.logger.warning(f"[{context}] {error_msg}")

    if user_id:
        log_audit(user_id, f"Error: {context}", {"message": error_msg, "level": level})
        if level == "error":
            create_notification(user_id, f"An issue occurred during {context}. Our team has been notified.")
    
    return error_msg

def update_progress(local_supabase, plan_id, step, label, percent, current_data):
    """Enhanced progress updater with detailed status tracking."""
    current_data['progress'] = {
        'step': step, 
        'label': label, 
        'percent': percent,
        'last_updated': datetime.now().isoformat()
    }
    try:
        local_supabase.table('course_learning_plans').update({
            'content': json.dumps(current_data)
        }).eq('id', plan_id).execute()
    except Exception as e:
        if current_app:
            current_app.logger.warning(f"Failed to update progress for {plan_id}: {e}")


def parse_iso(value):
    if not value: return None
    try:
        # Handle cases where value is already a datetime
        if isinstance(value, datetime): return value
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except: return None

def format_datetime(value, format="%b %d, %Y %I:%M %p"):
    if not value: return ""
    return value.strftime(format)

# --- RELATIONAL MAPPING UTILITY ---

def shred_clp_mappings(local_supabase, plan_id, clp_data):
    """
    Parses the flat JSON data and extracts CO-PO and PO-IO mappings into a relational table.
    This enables fast SQL-based analytics and coverage tracking.
    """
    try:
        # First, clear any old mappings for this plan
        local_supabase.table('clp_mapping_entries').delete().eq('plan_id', plan_id).execute()
        
        mappings = []
        # Weight map for coverage intensity
        weight_map = {
            'I': 1,   # Introductory
            'R': 3,   # Reinforced
            'M': 5,   # Mastered
            # Legacy values kept for backward compatibility
            'E': 3,
            'D': 5,
            '✔': 2,
            '✓': 2
        }

        def parse_code_list(raw_value):
            """Extracts ordered, unique CO codes from string/list payloads."""
            if isinstance(raw_value, list):
                items = [str(x).strip() for x in raw_value]
            elif isinstance(raw_value, str):
                normalized = raw_value.replace('\n', ',').replace(';', ',')
                items = [x.strip() for x in normalized.split(',')]
            else:
                items = []

            seen = set()
            codes = []
            for item in items:
                if not item:
                    continue
                code = item.upper().replace(' ', '')
                if code in seen:
                    continue
                seen.add(code)
                codes.append(code)
            return codes

        # Iterate through the JSON keys looking for patterns like CO1_PO1 or PO1_IO1
        for key, val in clp_data.items():
            if val is None:
                continue
            clean_val = val.strip() if isinstance(val, str) else val
            if isinstance(clean_val, str) and clean_val == "":
                continue

            weight = weight_map.get(clean_val, 1) if isinstance(clean_val, str) else 1

            # Case 1: CO-PO (pattern CO*_PO*)
            if "CO" in key and "_PO" in key:
                parts = key.split('_')
                # Usually CO1_PO1 or similar
                if len(parts) >= 2:
                    mappings.append({
                        'plan_id': plan_id,
                        'source_type': 'CO_PO',
                        'source_code': parts[0],
                        'target_code': parts[1],
                        'mapping_value': clean_val,
                        'weight': weight
                    })

            # Case 2: PO-IO (pattern PO*_IO*)
            elif "PO" in key and "_IO" in key:
                parts = key.split('_')
                if len(parts) >= 2:
                    mappings.append({
                        'plan_id': plan_id,
                        'source_type': 'PO_IO',
                        'source_code': parts[0],
                        'target_code': parts[1],
                        'mapping_value': clean_val,
                        'weight': weight
                    })

            # Case 3: Weekly LO to Course Outcome mappings (e.g., W1_Mapped_COs: "CO1, CO3")
            elif key.endswith('_Mapped_COs'):
                week_code = key.replace('_Mapped_COs', '')
                for co_code in parse_code_list(val):
                    mappings.append({
                        'plan_id': plan_id,
                        'source_type': 'WLO_CO',
                        'source_code': week_code,
                        'target_code': co_code,
                        'mapping_value': 'mapped',
                        'weight': 1
                    })

            # Case 4: Assessment to Course Outcome mappings (e.g., W1_Assessment_COs: "CO1")
            elif key.endswith('_Assessment_COs'):
                assessment_code = key.replace('_Assessment_COs', '')
                for co_code in parse_code_list(val):
                    mappings.append({
                        'plan_id': plan_id,
                        'source_type': 'ASSESSMENT_CO',
                        'source_code': assessment_code,
                        'target_code': co_code,
                        'mapping_value': 'assessed',
                        'weight': 1
                    })
        
        if mappings:
            # Batch insert for efficiency
            local_supabase.table('clp_mapping_entries').insert(mappings).execute()
            
    except Exception as e:
        # Don't fail the whole process if shredding fails, but log it
        print(f"Error shredding mappings for Plan {plan_id}: {e}")

def update_env(key_values: dict):
    """
    Updates the .env file with the provided key-value pairs.
    If a value is None, the key is removed.
    Also updates os.environ for the current process.
    """
    import os
    from flask import current_app
    
    try:
        # Get absolute path to this file's directory (app/)
        current_dir = os.path.dirname(os.path.abspath(__file__))
        # For Application Factory, .env is usually one level up from app/
        env_path = os.path.join(os.path.dirname(current_dir), '.env')
        
        # If it doesn't exist, try in the current dir just in case
        if not os.path.exists(env_path):
            env_path = os.path.join(current_dir, '.env')

        if not os.path.exists(env_path):
            with open(env_path, 'w') as f:
                f.write("")

        with open(env_path, 'r') as f:
            lines = f.readlines()

        new_lines = []
        keys_remaining = key_values.copy()

        for line in lines:
            line_strip = line.strip()
            if '=' in line and not line_strip.startswith('#'):
                # Handle cases like KEY=VALUE or KEY=
                k = line.split('=')[0].strip()
                if k in keys_remaining:
                    val = keys_remaining.pop(k)
                    if val is not None:
                        new_lines.append(f"{k}={val}\n")
                        os.environ[k] = str(val)
                    else:
                        if k in os.environ:
                            del os.environ[k]
                    continue
            new_lines.append(line)

        # Add any new keys
        for k, val in keys_remaining.items():
            if val is not None:
                new_lines.append(f"{k}={val}\n")
                os.environ[k] = str(val)

        with open(env_path, 'w') as f:
            f.writelines(new_lines)
            
    except Exception as e:
        if current_app:
            current_app.logger.error(f"Failed to update .env: {e}")
        else:
            print(f"Failed to update .env: {e}")
