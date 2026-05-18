from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app, jsonify, Response, session
from app.compat_supabase import (
    PostgrestAPIError,
    build_public_storage_url,
    read_storage_bytes,
    write_storage_bytes,
    delete_storage_paths,
    verify_password_hash,
)
from app import supabase, STORAGE_BUCKET_NAME, csrf
from app.decorators import login_required, roles_required, admin_required
from werkzeug.utils import secure_filename
import os, time, hashlib, requests, traceback, io
import re
from datetime import datetime, timedelta, timezone
import jwt
from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from app.forms import (ApproveUserForm, TemplateEditForm, EditUserForm, 
                       DepartmentForm, TemplateUploadForm, SystemSettingsForm, OutcomeForm, DeleteForm, CopilotReferenceForm,
                       TemplateFromCLPBetaForm, TemplateBetaPromoteForm, TeacherSubjectForm, ChangePasswordForm, UserProfileForm)
from app.utils import (
    count_active_admins,
    attach_onlyoffice_ai_plugin,
    create_notification,
    delete_clp_with_dependencies,
    make_admin_feedback,
    log_audit,
    log_system_event,
    parse_supabase_timestamp,
    parse_iso,
    generate_jwt_token,
    get_current_user_profile,
    invalidate_user_profile_cache,
    get_system_prompt,
    get_system_settings_map,
    invalidate_system_settings_cache,
    invalidate_department_caches,
    get_department_records,
    get_clp_dependency_summary,
    get_clp_dependency_summaries,
    get_department_dependency_summary,
    get_outcome_dependency_summary,
    get_user_dependency_summary,
    get_user_dependency_summaries,
    delete_clp_dependencies,
    get_onlyoffice_base_url,
    stream_storage_file,
    log_document_timing,
    ensure_plan_document_file,
)
from app.services.template_ai_service import generate_template_from_docx, summarize_placeholder_summary
from app.services.task_queue import TaskQueue
from app.services.copilot_beta_service import (
    COPILOT_DEFAULT_TEMPLATE_FILENAME_KEY,
    COPILOT_DEFAULT_TEMPLATE_NAME_KEY,
    AQRF_LEVEL_6_OPTIONS,
    CORE_VALUE_OPTIONS,
    PQF_LEVEL_6_OPTIONS,
    SDG_CONTEXT,
    SGA_OPTIONS,
    get_beta_prompt_defaults,
)
from license_manager import license_manager

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')


def _prompt_setting_defaults():
    defaults = {
        'prompt_po_io': 'Analyze alignment between Program Outcomes (PO) and Institutional Outcomes (IO). Map ✔ for aligned.',
        'prompt_co_po': 'Map Course Outcomes (CO) to Program Outcomes (PO).',
        'prompt_weekly': 'Generate comprehensive weekly outline with Learning Outcomes (LO), Topics (TO), Methods, and Assessments.',
    }
    defaults.update(get_beta_prompt_defaults())
    return defaults


def _copilot_reference_seed_rows_for_department(department_id):
    rows = []
    sort_order = 0
    for title in SGA_OPTIONS:
        rows.append({'department_id': department_id, 'category': 'graduate_attributes', 'code': title, 'title': title, 'description': '', 'sort_order': sort_order})
        sort_order += 1
    for title in CORE_VALUE_OPTIONS:
        rows.append({'department_id': department_id, 'category': 'core_values', 'code': title, 'title': title, 'description': '', 'sort_order': sort_order})
        sort_order += 1
    for code in PQF_LEVEL_6_OPTIONS:
        rows.append({'department_id': department_id, 'category': 'pqf_level_6', 'code': code, 'title': code, 'description': '', 'sort_order': sort_order})
        sort_order += 1
    for code in AQRF_LEVEL_6_OPTIONS:
        rows.append({'department_id': department_id, 'category': 'aqrf_level_6', 'code': code, 'title': code, 'description': '', 'sort_order': sort_order})
        sort_order += 1
    for order, code in enumerate(SDG_CONTEXT.keys(), start=sort_order):
        details = SDG_CONTEXT.get(code, {})
        rows.append({
            'department_id': department_id,
            'category': 'sdg',
            'code': code,
            'title': details.get('title', code),
            'description': details.get('guidance', ''),
            'sort_order': order,
        })
    return rows


def _ensure_copilot_reference_seeded(client):
    departments = get_department_records()
    if not departments:
        return {}
    existing_rows = client.table('copilot_reference_entries').select('*').execute().data or []
    rows_by_department = {}
    updates = []
    for department in departments:
        dept_id = department.get('id')
        dept_rows = [row for row in existing_rows if row.get('department_id') == dept_id]
        rows_by_department[str(dept_id)] = dept_rows
        if dept_rows:
            continue
        seeded = _copilot_reference_seed_rows_for_department(dept_id)
        if seeded:
            updates.extend(seeded)
            rows_by_department[str(dept_id)] = seeded
    if updates:
        client.table('copilot_reference_entries').insert(updates).execute()
        existing_rows = client.table('copilot_reference_entries').select('*').execute().data or []
        rows_by_department = {}
        for department in departments:
            rows_by_department[str(department.get('id'))] = [row for row in existing_rows if row.get('department_id') == department.get('id')]
    return rows_by_department


def _ensure_prompt_settings_seeded(client):
    prompt_defaults = _prompt_setting_defaults()
    settings = get_system_settings_map(client, force_refresh=True)
    updates = []
    for key, default_value in prompt_defaults.items():
        current_value = settings.get(key)
        if current_value is None or not str(current_value).strip():
            updates.append({'key': key, 'value': default_value})
    if updates:
        client.table('system_settings').upsert(updates).execute()
        invalidate_system_settings_cache([item['key'] for item in updates])
        settings = get_system_settings_map(client, force_refresh=True)
    return settings, prompt_defaults, {}


def _bundled_production_template_path():
    from pathlib import Path
    return Path(current_app.root_path).parent / "new_template.docx"


def _ensure_production_copilot_default_template(client):
    settings = get_system_settings_map(client, force_refresh=True)
    current_filename = str(settings.get(COPILOT_DEFAULT_TEMPLATE_FILENAME_KEY) or "").strip()
    current_name = str(settings.get(COPILOT_DEFAULT_TEMPLATE_NAME_KEY) or "").strip()
    existing_templates = client.table('templates').select('*').order('created_at', desc=True).execute().data or []

    matching = None
    if current_filename:
        matching = next((row for row in existing_templates if row.get('filename') == current_filename), None)
    if not matching and current_name:
        matching = next((row for row in existing_templates if row.get('name') == current_name), None)

    needs_bootstrap = not current_filename or not current_name or not matching
    if not needs_bootstrap:
        return current_name, current_filename

    if not matching:
        template_path = _bundled_production_template_path()
        if not template_path.exists():
            raise FileNotFoundError(f"Bundled production template is missing: {template_path}")
        target_name = "Production Copilot Default"
        target_filename = f"templates/{int(time.time())}_{secure_filename(template_path.name)}"
        write_storage_bytes(STORAGE_BUCKET_NAME, target_filename, template_path.read_bytes())
        insert_result = client.table('templates').insert({
            'name': target_name,
            'filename': target_filename,
            'department_id': None,
            'is_default': True,
        }).execute()
        matching = (insert_result.data or [{}])[0]
        client.table('templates').update({'is_default': False}).neq('id', matching.get('id')).execute()
    else:
        client.table('templates').update({'is_default': False}).neq('id', matching.get('id')).execute()
        client.table('templates').update({'is_default': True}).eq('id', matching.get('id')).execute()

    template_name = matching.get('name') or current_name or "Production Copilot Default"
    template_filename = matching.get('filename') or current_filename
    updates = [
        {'key': COPILOT_DEFAULT_TEMPLATE_NAME_KEY, 'value': template_name},
        {'key': COPILOT_DEFAULT_TEMPLATE_FILENAME_KEY, 'value': template_filename},
    ]
    client.table('system_settings').upsert(updates).execute()
    invalidate_system_settings_cache([item['key'] for item in updates])
    return template_name, template_filename


def _copilot_category_label(category):
    labels = {
        'graduate_attributes': 'Graduate Attributes',
        'core_values': 'Core Values',
        'pqf_level_6': 'PQF Level 6',
        'aqrf_level_6': 'AQRF Level 6',
        'sdg': 'SDG',
    }
    return labels.get(category, category.replace('_', ' ').title())


def _get_active_term_settings():
    settings = get_system_settings_map(local_supabase=current_app.config.get('SUPABASE_SERVICE'))
    active_semester = str(settings.get('active_semester') or '').strip()
    active_academic_year = str(settings.get('active_academic_year') or '').strip()
    if not active_semester or not active_academic_year:
        current_semester = str(settings.get('current_semester') or '').strip()
        match = re.search(r'\b\d{4}-\d{4}\b', current_semester)
        parsed_year = match.group(0) if match else ''
        parsed_semester = current_semester.replace(parsed_year, '').strip(' -') if current_semester else ''
        active_semester = active_semester or parsed_semester or '1st Semester'
        active_academic_year = active_academic_year or parsed_year or str(datetime.now().year)
    return active_semester, active_academic_year

def _validate_onlyoffice_callback(data):
    secret = current_app.config.get('ONLYOFFICE_JWT_SECRET', '')
    if not secret:
        current_app.logger.error("ONLYOFFICE_JWT_SECRET not configured; rejecting callback.")
        return False

    token = None
    auth_header = request.headers.get('Authorization', '')
    if auth_header.lower().startswith('bearer '):
        token = auth_header.split(' ', 1)[1].strip()
    if not token and isinstance(data, dict):
        token = data.get('token')

    if not token:
        current_app.logger.warning("OnlyOffice callback missing token.")
        return False

    try:
        jwt.decode(token, secret, algorithms=['HS256'])
        return True
    except Exception as e:
        current_app.logger.warning(f"OnlyOffice callback token invalid: {e}")
        return False


def _get_onlyoffice_base_url():
    return get_onlyoffice_base_url(internal=True)


def _flash_feedback(feedback):
    flash(feedback['message'], feedback['level'])


def _handle_admin_exception(action, exc, *, category='admin', user_id=None, plan_id=None, detail=None):
    feedback = make_admin_feedback(action, exc, detail=detail)
    log_system_event(
        category,
        'error' if feedback['level'] == 'danger' else 'warning',
        f"{action} failed",
        details={'error': str(exc), 'kind': feedback['kind'], 'detail': detail},
        user_id=user_id,
        plan_id=plan_id,
    )
    current_app.logger.warning(f"{action} failed: {exc}")
    _flash_feedback(feedback)
    return feedback


def _has_dependencies(summary):
    return any((summary or {}).values())


def _dependency_warning_text(summary, labels=None):
    labels = labels or {}
    parts = []
    for key, value in (summary or {}).items():
        if not value:
            continue
        label = labels.get(key, key.replace('_', ' '))
        parts.append(f"{value} {label}")
    return ", ".join(parts)


def _ensure_not_last_active_admin(user):
    if not user or user.get('role') != 'admin' or user.get('active') is False:
        return
    if count_active_admins() <= 1:
        raise ValueError("You cannot deactivate or delete the last active administrator.")


def _ensure_admin_role_transition_safe(user, new_role=None, new_active=None, new_approved=None):
    if not user:
        return

    current_is_active_admin = (
        user.get('role') == 'admin'
        and user.get('active', True)
        and user.get('approved', False)
    )
    if not current_is_active_admin:
        return

    resulting_role = new_role if new_role is not None else user.get('role')
    resulting_active = new_active if new_active is not None else user.get('active', True)
    resulting_approved = new_approved if new_approved is not None else user.get('approved', False)
    resulting_is_active_admin = (
        resulting_role == 'admin'
        and bool(resulting_active)
        and bool(resulting_approved)
    )

    if not resulting_is_active_admin and count_active_admins() <= 1:
        raise ValueError("You cannot remove admin access from the last active administrator.")


def _get_onlyoffice_health_url():
    api_js_url = current_app.config.get('ONLYOFFICE_API_JS_URL') or os.environ.get('ONLYOFFICE_API_JS_URL', '')
    if not api_js_url:
        return None
    base = api_js_url.split('/web-apps/', 1)[0].rstrip('/')
    if not base:
        return None
    return f"{base}/healthcheck"


def _build_service_health():
    health = {
        'database': {'status': 'healthy', 'detail': 'Local PostgreSQL reachable.'},
        'storage': {'status': 'healthy', 'detail': 'Local storage root is available.'},
        'onlyoffice': {'status': 'unknown', 'detail': 'OnlyOffice healthcheck not configured.'},
        'ai-monitor': {'status': 'unknown', 'detail': 'ZeroClaw autonomous agent status unknown.'},
    }

    try:
        supabase.table('system_settings').select('key').limit(1).execute()
    except Exception as exc:
        health['database'] = {'status': 'degraded', 'detail': str(exc)}

    storage_root = current_app.config.get('LPMS_STORAGE_ROOT') or os.environ.get('LPMS_STORAGE_ROOT')
    if not storage_root or not os.path.isdir(storage_root):
        health['storage'] = {'status': 'degraded', 'detail': 'Local storage root is missing or unreadable.'}

    health_url = _get_onlyoffice_health_url()
    if health_url:
        try:
            resp = requests.get(health_url, timeout=3)
            if resp.ok:
                health['onlyoffice'] = {'status': 'healthy', 'detail': 'Document server responded successfully.'}
            else:
                health['onlyoffice'] = {'status': 'degraded', 'detail': f'Healthcheck returned {resp.status_code}.'}
        except Exception as exc:
            health['onlyoffice'] = {'status': 'degraded', 'detail': str(exc)}

    # ZeroClaw autonomous AI monitor health
    try:
        import subprocess
        result = subprocess.run(['systemctl', 'is-active', 'zeroclaw'], capture_output=True, text=True, timeout=5)
        status = result.stdout.strip()
        if status == 'active':
            # Get memory usage
            mem = subprocess.run(
                ['systemctl', 'show', 'zeroclaw', '--property=MemoryCurrent'],
                capture_output=True, text=True, timeout=5
            )
            mem_bytes = int(mem.stdout.strip().split('=')[1]) if '=' in mem.stdout.strip() else 0
            mem_mb = mem_bytes // 1024 // 1024 if mem_bytes else 0
            health['ai-monitor'] = {
                'status': 'healthy',
                'detail': f'ZeroClaw agent running ({mem_mb} MB). Monitors OOMs, errors, and service health every 30 min.'
            }
        else:
            health['ai-monitor'] = {'status': 'degraded', 'detail': f'ZeroClaw agent is {status}.'}
    except Exception as exc:
        health['ai-monitor'] = {'status': 'degraded', 'detail': f'Cannot reach ZeroClaw: {exc}'}

    return health


def _beta_template_doc_key(draft):
    return hashlib.md5(f"beta_tmpl_{draft['id']}_{draft['generated_filename']}".encode()).hexdigest()


def _load_beta_template_draft(draft_id):
    result = supabase.table('generated_template_drafts').select('*').eq('id', draft_id).single().execute()
    draft = result.data
    if not draft:
        abort(404)
    return draft


def _beta_template_department_name(department_id):
    if not department_id:
        return None
    try:
        result = supabase.table('departments').select('name').eq('id', department_id).single().execute()
        return result.data.get('name') if result.data else None
    except Exception:
        return None


def _safe_template_beta_storage_name(name):
    safe_name = secure_filename(name or "generated_template")
    return safe_name or "generated_template"

@admin_bp.route('/audit_logs')
@login_required
@roles_required('admin')
def view_audit_logs():
    # ... existing code ...
    selected_user = request.args.get('user_id') or request.args.get('user') or 'all'
    try:
        client = current_app.config.get('SUPABASE_SERVICE') or supabase
        # Fetch all users for the filter dropdown
        users_res = client.table('users').select('id, username, first_name, last_name').order('username').execute()
        users = users_res.data

        # Fetch logs
        query = client.table('audit_logs').select('*, actor:users(username, first_name, last_name)').order('timestamp', desc=True).limit(200)
        
        if selected_user != 'all':
            query = query.eq('user_id', selected_user)
            
        logs_res = query.execute()
        logs = parse_supabase_timestamp(logs_res.data, 'timestamp')
        
    except Exception as e:
        current_app.logger.error(f"Audit Logs Fetch Error: {e}")
        users = []
        logs = []
        flash("Error fetching audit logs.", "danger")

    return render_template('admin_audit_logs.html', users=users, logs=logs, selected_user=selected_user)

@admin_bp.route('/audit_logs/export/pdf')
@login_required
@roles_required('admin')
def export_audit_logs_pdf():
    selected_user = request.args.get('user_id') or request.args.get('user') or 'all'
    try:
        client = current_app.config.get('SUPABASE_SERVICE') or supabase
        # Fetch logs
        query = client.table('audit_logs').select('*, actor:users(username, first_name, last_name)').order('timestamp', desc=True).limit(500)
        
        if selected_user != 'all':
            query = query.eq('user_id', selected_user)
            
        logs_res = query.execute()
        logs = parse_supabase_timestamp(logs_res.data, 'timestamp')
        
        # Create PDF
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=landscape(letter), rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
        elements = []
        
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], fontSize=16, alignment=1, spaceAfter=20)
        
        inst_name = f"{get_system_prompt(client, 'institution_name', 'Security Audit Logs')} - Security Audit Report"
        
        elements.append(Paragraph(inst_name, title_style))
        elements.append(Paragraph(f"Generated on: {time.strftime('%Y-%m-%d %I:%M %p')}", styles['Normal']))
        if selected_user != 'all' and logs:
            user_name = "Unknown User"
            if logs[0].get('actor'):
                user_name = f"{logs[0]['actor']['first_name']} {logs[0]['actor']['last_name']} (@{logs[0]['actor']['username']})"
            elements.append(Paragraph(f"Filter: User - {user_name}", styles['Normal']))
        elements.append(Spacer(1, 12))
        
        # Table data
        data = [['Timestamp', 'Actor', 'IP Address', 'Action', 'Event', 'Result', 'Status', 'Method', 'Endpoint', 'Resource', 'Latency (ms)']]
        for log in logs:
            actor = "N/A"
            if log.get('actor'):
                actor = f"{log['actor']['username']}"
            
            data.append([
                log['timestamp'].strftime('%Y-%m-%d %H:%M'),
                actor,
                log.get('ip_address', 'N/A'),
                log.get('action', 'N/A'),
                log.get('event_type', 'N/A'),
                log.get('result', 'N/A'),
                log.get('status_code', 'N/A'),
                log.get('method', 'N/A'),
                log.get('endpoint', 'N/A'),
                log.get('resource_id', 'N/A'),
                log.get('latency_ms', 'N/A')
            ])
        
        t = Table(data, colWidths=[90, 70, 80, 100, 60, 60, 50, 50, 200, 70, 70])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('FONTSIZE', (0, 1), (-1, -1), 8),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        elements.append(t)
        
        doc.build(elements)
        buffer.seek(0)
        
        filename = f"audit_logs_{int(time.time())}.pdf"
        return Response(buffer, mimetype='application/pdf', headers={'Content-Disposition': f'attachment;filename={filename}'})
        
    except Exception as e:
        current_app.logger.error(f"PDF Export Error: {traceback.format_exc()}")
        flash(f"Error exporting PDF: {str(e)}", "danger")
        return redirect(url_for('admin.view_audit_logs'))

@admin_bp.route('/user/create', methods=['GET', 'POST'])
@login_required
@roles_required('admin')
def admin_create_user():
    from app.forms import AdminCreateUserForm
    form = AdminCreateUserForm()
    
    if form.validate_on_submit():
        email = form.email.data
        password = form.password.data
        username = form.username.data
        
        try:
            # 1. Use the service_role client to create the user (bypasses email verification)
            supabase_service = current_app.config.get('SUPABASE_SERVICE')
            if not supabase_service:
                flash("Supabase Service Key not configured.", "danger")
                return redirect(url_for('admin.admin_dashboard'))

            # Create the auth user and confirm email immediately
            auth_res = supabase_service.auth.admin.create_user({
                "email": email,
                "password": password,
                "email_confirm": True
            })
            
            if auth_res.user:
                # 2. Create the profile in the 'users' table
                profile_data = {
                    'id': auth_res.user.id,
                    'username': username,
                    'email': email,
                    'first_name': form.first_name.data,
                    'last_name': form.last_name.data,
                    'role': form.role.data,
                    'approved': True, # Admins manually creating users auto-approve them
                    'active': True,
                    'assigned_department': form.department.data if form.department.data else None
                }
                supabase.table('users').insert(profile_data).execute()
                
                log_audit(session.get('user_id'), 'Admin Created User', {'new_user_email': email, 'role': form.role.data})
                
                _flash_feedback(make_admin_feedback('Create user', detail=f"User {email} created and verified successfully."))
                return redirect(url_for('admin.admin_dashboard'))
        except Exception as e:
            _handle_admin_exception('Create user', e, category='admin_user')

    return render_template('admin_create_user.html', form=form)

@admin_bp.route('/analytics')
@login_required
@roles_required('admin')
def admin_analytics():
    try:
        from app import cache
        snapshot = cache.get('admin_analytics_snapshot')
        if snapshot is None:
            plans = supabase.table('course_learning_plans').select('id, status, department, date_posted, last_updated').execute().data or []
            users = supabase.table('users').select('id, role, approved, active, assigned_department').execute().data or []
            tasks = supabase.table('background_tasks').select('id, status, created_at').execute().data or []
            ai_usage = supabase.table('ai_usage_log').select('id, status, created_at').execute().data or []
            audit_logs = supabase.table('audit_logs').select('id, timestamp').execute().data or []

            now = datetime.now(timezone.utc)
            snapshot = {
                'total': len(plans),
                'approved': 0,
                'pending': 0,
                'returned': 0,
                'dept_counts': {},
                'approval_by_department': {},
                'pending_users': 0,
                'inactive_users': 0,
                'failed_tasks_24h': 0,
                'ai_failures_24h': 0,
                'activity_24h': 0,
                'oldest_pending_hours': 0,
                'last_updated': now.isoformat(),
            }
            oldest_pending = None
            for row in plans:
                status = row.get('status')
                if status == 'approved':
                    snapshot['approved'] += 1
                elif status == 'pending':
                    snapshot['pending'] += 1
                    pending_date = parse_iso(row.get('last_updated') or row.get('date_posted'))
                    if pending_date and (oldest_pending is None or pending_date < oldest_pending):
                        oldest_pending = pending_date
                elif status == 'returned_for_revision':
                    snapshot['returned'] += 1

                dept = row.get('department') or 'Unassigned'
                snapshot['dept_counts'][dept] = snapshot['dept_counts'].get(dept, 0) + 1
                dept_bucket = snapshot['approval_by_department'].setdefault(dept, {'approved': 0, 'pending': 0, 'returned': 0, 'other': 0})
                if status == 'approved':
                    dept_bucket['approved'] += 1
                elif status == 'pending':
                    dept_bucket['pending'] += 1
                elif status == 'returned_for_revision':
                    dept_bucket['returned'] += 1
                else:
                    dept_bucket['other'] += 1

            for user in users:
                if not user.get('approved'):
                    snapshot['pending_users'] += 1
                if user.get('active') is False:
                    snapshot['inactive_users'] += 1

            for task in tasks:
                created_at = parse_iso(task.get('created_at'))
                if created_at and (now - created_at).total_seconds() <= 86400 and task.get('status') in {'failed', 'error'}:
                    snapshot['failed_tasks_24h'] += 1

            for entry in ai_usage:
                created_at = parse_iso(entry.get('created_at'))
                if created_at and (now - created_at).total_seconds() <= 86400 and entry.get('status') not in {None, 'success', 'completed'}:
                    snapshot['ai_failures_24h'] += 1

            for entry in audit_logs:
                created_at = parse_iso(entry.get('timestamp'))
                if created_at and (now - created_at).total_seconds() <= 86400:
                    snapshot['activity_24h'] += 1

            if oldest_pending:
                snapshot['oldest_pending_hours'] = int((now - oldest_pending).total_seconds() // 3600)

            cache.set('admin_analytics_snapshot', snapshot, timeout=30)

        total = snapshot['total']
        approved = snapshot['approved']
        pending = snapshot['pending']
        returned = snapshot['returned']
        dept_counts = snapshot['dept_counts']
        approval_by_department = snapshot.get('approval_by_department', {})
        pending_users = snapshot.get('pending_users', 0)
        inactive_users = snapshot.get('inactive_users', 0)
        failed_tasks_24h = snapshot.get('failed_tasks_24h', 0)
        ai_failures_24h = snapshot.get('ai_failures_24h', 0)
        activity_24h = snapshot.get('activity_24h', 0)
        oldest_pending_hours = snapshot.get('oldest_pending_hours', 0)
        last_updated = parse_iso(snapshot.get('last_updated'))
    except Exception as e:
        total = approved = pending = returned = 0
        dept_counts = {}
        approval_by_department = {}
        pending_users = inactive_users = failed_tasks_24h = ai_failures_24h = activity_24h = oldest_pending_hours = 0
        last_updated = None
        _handle_admin_exception("Load analytics", e, category='analytics')

    return render_template('admin_analytics.html', 
                           total=total, 
                           approved=approved, 
                           pending=pending, 
                           returned=returned,
                           dept_counts=dept_counts,
                           approval_by_department=approval_by_department,
                           pending_users=pending_users,
                           inactive_users=inactive_users,
                           failed_tasks_24h=failed_tasks_24h,
                           ai_failures_24h=ai_failures_24h,
                           activity_24h=activity_24h,
                           oldest_pending_hours=oldest_pending_hours,
                           last_updated=last_updated)

@admin_bp.route('/dashboard')
@login_required
@roles_required('admin')
def admin_dashboard():
    try:
        needs_license = not license_manager.check_local_license()
        search_query = (request.args.get('q') or '').strip().lower()
        status_filter = (request.args.get('status') or 'active').strip().lower()
        pending_users_res = supabase.table('users').select('*').eq('approved', False).order('created_at', desc=True).execute()
        approved_users_res = supabase.table('users').select('*').eq('approved', True).order('last_name').execute()

        pending_user_forms = []
        for user in pending_users_res.data:
            form = ApproveUserForm(user_id=user['id'])
            form.assigned_department.data = user.get('assigned_department')
            form.role.data = user.get('role', 'teacher')
            pending_user_forms.append({'user': user, 'form': form})

        approved_users = approved_users_res.data or []
        if status_filter == 'active':
            approved_users = [user for user in approved_users if user.get('active', True)]
        elif status_filter == 'inactive':
            approved_users = [user for user in approved_users if not user.get('active', True)]

        if search_query:
            def _matches(user):
                haystack = " ".join([
                    str(user.get('first_name') or ''),
                    str(user.get('last_name') or ''),
                    str(user.get('email') or ''),
                    str(user.get('username') or ''),
                    str(user.get('assigned_department') or ''),
                    str(user.get('role') or ''),
                ]).lower()
                return search_query in haystack
            approved_users = [user for user in approved_users if _matches(user)]

        user_dependency_summary = get_user_dependency_summaries([user['id'] for user in approved_users])
        active_admin_count = count_active_admins()

        return render_template('admin_dashboard.html',
                               pending_user_forms=pending_user_forms,
                               approved_users=approved_users,
                               user_dependency_summary=user_dependency_summary,
                               active_admin_count=active_admin_count,
                               delete_form=DeleteForm(),
                               needs_license=needs_license,
                               search_query=search_query,
                               status_filter=status_filter)
    except Exception as e:
        current_app.logger.error(f"Dashboard Error: {e}")
        return render_template(
            'admin_dashboard.html',
            pending_user_forms=[],
            approved_users=[],
            user_dependency_summary={},
            active_admin_count=0,
            needs_license=True,
            search_query='',
            status_filter='active',
        )


@admin_bp.route('/profile', methods=['GET', 'POST'])
@login_required
@roles_required('admin')
def admin_profile():
    user = get_current_user_profile()
    if not user:
        flash('We could not load your profile. Please sign in again.', 'warning')
        session.clear()
        return redirect(url_for('auth.login'))

    info_form = UserProfileForm(obj=user)
    pwd_form = ChangePasswordForm()

    if request.method == 'GET':
        info_form.first_name.data = user.get('first_name')
        info_form.last_name.data = user.get('last_name')
        info_form.title.data = user.get('title')

    if info_form.validate_on_submit() and info_form.submit_info.data:
        update_data = {
            'first_name': info_form.first_name.data,
            'last_name': info_form.last_name.data,
            'title': info_form.title.data,
        }
        if info_form.signature.data:
            file = info_form.signature.data
            filename = secure_filename(file.filename)
            file_path = f"signatures/{session['user_id']}_{int(time.time())}_{filename}"
            try:
                supabase.storage.from_(STORAGE_BUCKET_NAME).upload(
                    path=file_path,
                    file=file.read(),
                    file_options={"content-type": file.mimetype}
                )
                update_data['signature_url'] = build_public_storage_url(STORAGE_BUCKET_NAME, file_path)
            except Exception as e:
                flash(f"Error uploading signature: {e}", "danger")

        try:
            supabase.table('users').update(update_data).eq('id', session['user_id']).execute()
            invalidate_user_profile_cache(session['user_id'])
            flash('Profile updated.', 'success')
            return redirect(url_for('admin.admin_profile'))
        except Exception as e:
            flash(f"Error updating profile: {e}", 'danger')

    if pwd_form.validate_on_submit() and pwd_form.submit.data:
        try:
            password_row = (
                supabase.table('users')
                .select('password_hash')
                .eq('id', session['user_id'])
                .single()
                .execute()
            )
            current_password_hash = (password_row.data or {}).get('password_hash')
            if not verify_password_hash(current_password_hash, pwd_form.current_password.data):
                flash('Current password is incorrect.', 'danger')
                return render_template('admin_profile.html', info_form=info_form, pwd_form=pwd_form, user=user)
            supabase.auth.update_user({"password": pwd_form.new_password.data})
            flash('Password changed successfully.', 'success')
            return redirect(url_for('admin.admin_profile'))
        except Exception as e:
            flash(f"Error changing password: {e}", 'danger')

    return render_template('admin_profile.html', info_form=info_form, pwd_form=pwd_form, user=user)


@admin_bp.route('/operations')
@login_required
@roles_required('admin')
def admin_operations():
    failed_tasks = []
    stuck_tasks = []
    recent_events = []
    recent_failures = []
    copilot_ai_summary = []
    health = _build_service_health()
    now = datetime.now(timezone.utc)

    try:
        tasks = (
            supabase.table('background_tasks')
            .select('*')
            .order('created_at', desc=True)
            .limit(50)
            .execute()
            .data or []
        )
        for task in tasks:
            created_at = parse_iso(task.get('created_at'))
            started_at = parse_iso(task.get('started_at'))
            anchor = started_at or created_at
            if task.get('status') in {'failed', 'error'}:
                failed_tasks.append(task)
            elif task.get('status') not in {'completed', 'success'} and anchor and (now - anchor) > timedelta(minutes=15):
                stuck_tasks.append(task)
    except Exception as e:
        _handle_admin_exception('Load background task operations', e, category='operations')

    try:
        usage_rows = (
            supabase.table('ai_usage_log')
            .select('task_type,duration_ms,status,created_at,plan_id,model_used')
            .order('created_at', desc=True)
            .limit(200)
            .execute()
            .data or []
        )
        cutoff = now - timedelta(hours=24)
        buckets = {}

        def _duration_percentile(values, percentile):
            if not values:
                return 0
            ordered = sorted(values)
            index = int(round((len(ordered) - 1) * percentile))
            return ordered[index]

        for row in usage_rows:
            task_type = str(row.get('task_type') or '')
            if not task_type.startswith('copilot_beta'):
                continue
            created_at = parse_iso(row.get('created_at'))
            if created_at and created_at < cutoff:
                continue
            try:
                duration_ms = int(row.get('duration_ms') or 0)
            except (TypeError, ValueError):
                duration_ms = 0
            bucket = buckets.setdefault(task_type, {
                'task_type': task_type,
                'durations': [],
                'success': 0,
                'errors': 0,
                'last_plan_id': row.get('plan_id'),
                'last_model': row.get('model_used') or '',
            })
            if duration_ms > 0:
                bucket['durations'].append(duration_ms)
            if row.get('status') == 'success':
                bucket['success'] += 1
            else:
                bucket['errors'] += 1
            if not bucket.get('last_plan_id') and row.get('plan_id'):
                bucket['last_plan_id'] = row.get('plan_id')
            if not bucket.get('last_model') and row.get('model_used'):
                bucket['last_model'] = row.get('model_used')

        for task_type, bucket in sorted(buckets.items()):
            durations = bucket.pop('durations')
            count = len(durations)
            bucket.update({
                'count': count,
                'avg_ms': int(sum(durations) / count) if count else 0,
                'p50_ms': _duration_percentile(durations, 0.50),
                'p90_ms': _duration_percentile(durations, 0.90),
                'max_ms': max(durations) if durations else 0,
            })
            copilot_ai_summary.append(bucket)
    except Exception as e:
        _handle_admin_exception('Load Copilot AI timing summary', e, category='operations')

    try:
        recent_events = (
            supabase.table('system_events')
            .select('*')
            .order('created_at', desc=True)
            .limit(25)
            .execute()
            .data or []
        )
    except Exception as e:
        _handle_admin_exception('Load system events', e, category='operations')

    try:
        recent_failures = (
            supabase.table('audit_logs')
            .select('*, actor:users(username, first_name, last_name)')
            .order('timestamp', desc=True)
            .limit(25)
            .execute()
            .data or []
        )
        recent_failures = [
            row for row in recent_failures
            if row.get('result') not in (None, '', 'success') or (row.get('status_code') or 0) >= 400
        ]
        recent_failures = parse_supabase_timestamp(recent_failures, 'timestamp')
    except Exception as e:
        _handle_admin_exception('Load recent admin failures', e, category='operations')

    # Read ZeroClaw health log
    health_log = ''
    health_log_path = '/home/llppmmss/lpms/local_storage/zeroclaw-health.log'
    try:
        if os.path.exists(health_log_path):
            with open(health_log_path, 'r') as f:
                health_log = f.read()[-8000:]
    except Exception:
        pass

    return render_template(
        'admin_operations.html',
        failed_tasks=failed_tasks,
        stuck_tasks=stuck_tasks,
        recent_events=parse_supabase_timestamp(recent_events, 'created_at'),
        recent_failures=recent_failures,
        copilot_ai_summary=copilot_ai_summary,
        health=health,
        health_log=health_log,
        generated_at=now,
    )

@admin_bp.route('/approve_user', methods=['POST'])
@login_required
@roles_required('admin')
def approve_user_action():
    form = ApproveUserForm()
    if form.validate_on_submit():
        user_id = form.user_id.data
        update_data = {
            'approved': True,
            'active': True,
            'deactivated_at': None,
            'deactivation_reason': None,
            'role': form.role.data,
            'assigned_department': form.assigned_department.data if form.assigned_department.data else None
        }
        try:
            supabase.table('users').update(update_data).eq('id', user_id).execute()
            log_audit(session.get('user_id'), 'Admin Approved User', {'resource_id': user_id, 'role': form.role.data, 'result': 'success'})
            _flash_feedback(make_admin_feedback('Approve user', detail='User approved successfully.'))
        except PostgrestAPIError as e:
            _handle_admin_exception('Approve user', e, category='admin_user', user_id=user_id)
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/users/bulk_action', methods=['POST'])
@login_required
@roles_required('admin')
def bulk_user_action():
    selected_ids = list(dict.fromkeys(request.form.getlist('selected_user_ids')))
    bulk_action = request.form.get('bulk_action')
    bulk_role = request.form.get('bulk_role') or 'teacher'
    bulk_department = request.form.get('bulk_department') or None

    if not selected_ids:
        flash('Select at least one pending user first.', 'warning')
        return redirect(url_for('admin.admin_dashboard'))

    try:
        if bulk_action not in {'approve', 'reject'}:
            flash('Unsupported bulk action.', 'warning')
            return redirect(url_for('admin.admin_dashboard'))

        fetched_users = supabase.table('users').select('*').execute().data or []
        fetched_by_id = {
            str(user['id']): user
            for user in fetched_users
            if str(user.get('id')) in selected_ids
        }
        valid_pending = [user for user in fetched_by_id.values() if not user.get('approved')]
        valid_pending_ids = {str(user['id']) for user in valid_pending}
        not_found = [user_id for user_id in selected_ids if user_id not in fetched_by_id]
        skipped = [user_id for user_id in selected_ids if user_id in fetched_by_id and user_id not in valid_pending_ids]

        changed_count = 0
        if bulk_action == 'approve':
            for user in valid_pending:
                supabase.table('users').update({
                    'approved': True,
                    'active': True,
                    'deactivated_at': None,
                    'deactivation_reason': None,
                    'role': bulk_role,
                    'assigned_department': bulk_department,
                }).eq('id', user['id']).eq('approved', False).execute()
                changed_count += 1
            log_audit(session.get('user_id'), 'Admin Bulk Approved Users', {
                'approved': changed_count,
                'skipped': len(skipped),
                'not_found': len(not_found),
                'role': bulk_role,
                'department': bulk_department,
            })
        else:
            for user in valid_pending:
                supabase.table('users').delete().eq('id', user['id']).eq('approved', False).execute()
                changed_count += 1
            log_audit(session.get('user_id'), 'Admin Bulk Rejected Users', {
                'deleted': changed_count,
                'skipped': len(skipped),
                'not_found': len(not_found),
            })

        if skipped or not_found:
            log_system_event(
                'admin_user',
                'warning',
                f"Bulk user {bulk_action} completed with skipped records",
                details={
                    'selected_ids': selected_ids,
                    'skipped_ids': skipped,
                    'not_found_ids': not_found,
                },
                user_id=session.get('user_id'),
            )

        if changed_count and not skipped and not not_found:
            verb = 'Approved' if bulk_action == 'approve' else 'Rejected'
            flash(f'{verb} {changed_count} pending account(s).', 'success')
        elif changed_count:
            verb = 'approved' if bulk_action == 'approve' else 'rejected'
            flash(
                f'Partially completed: {changed_count} account(s) {verb}, '
                f'{len(skipped)} skipped, {len(not_found)} not found.',
                'warning'
            )
        else:
            flash(
                f'No pending accounts were changed. {len(skipped)} skipped, {len(not_found)} not found.',
                'warning'
            )
    except Exception as e:
        _handle_admin_exception('Bulk user action', e, category='admin_user')
    return redirect(url_for('admin.admin_dashboard'))

@admin_bp.route('/user/<user_id>/edit', methods=['GET', 'POST'])
@login_required
@roles_required('admin')
def edit_user(user_id):
    try:
        user = supabase.table('users').select('*').eq('id', user_id).single().execute().data
        if not user: abort(404)
    except PostgrestAPIError as e:
        flash(f"Database error: {e.message}", "danger")
        return redirect(url_for('admin.admin_dashboard'))

    form = EditUserForm()
    if form.validate_on_submit():
        update_data = {
            'first_name': form.first_name.data, 'last_name': form.last_name.data,
            'title': form.title.data, 'role': form.role.data,
            'assigned_department': form.department.data if form.department.data else None
        }
        try:
            _ensure_admin_role_transition_safe(user, new_role=form.role.data)
            supabase.table('users').update(update_data).eq('id', user_id).execute()
            log_audit(session.get('user_id'), 'Admin Updated User', {'resource_id': user_id, 'role': form.role.data})
            _flash_feedback(make_admin_feedback('Update user', detail='User profile updated.'))
            return redirect(url_for('admin.admin_dashboard'))
        except Exception as e:
            if 'last active administrator' in str(e).lower():
                log_audit(session.get('user_id'), 'Admin Edit Blocked', {
                    'resource_id': user_id,
                    'attempted_role': form.role.data,
                    'result': 'blocked_last_admin',
                })
            _handle_admin_exception('Update user', e, category='admin_user', user_id=user_id)

    if request.method == 'GET':
        form.first_name.data = user.get('first_name')
        form.last_name.data = user.get('last_name')
        form.title.data = user.get('title')
        form.role.data = user.get('role')
        form.department.data = user.get('assigned_department')

    is_last_active_admin = (
        user.get('role') == 'admin'
        and user.get('active', True)
        and user.get('approved', False)
        and count_active_admins() <= 1
    )

    return render_template(
        'admin_edit_user.html',
        form=form,
        user=user,
        is_admin_user=(user.get('role') == 'admin'),
        is_last_active_admin=is_last_active_admin,
    )

@admin_bp.route('/user/<user_id>/deactivate', methods=['POST'])
@login_required
@roles_required('admin')
def deactivate_user(user_id):
    form = DeleteForm()
    if form.validate_on_submit():
        try:
            user = supabase.table('users').select('*').eq('id', user_id).single().execute().data
            if not user:
                abort(404)
            _ensure_admin_role_transition_safe(user, new_active=False)
            supabase.table('users').update({
                'active': False,
                'deactivated_at': datetime.now(timezone.utc).isoformat(),
                'deactivation_reason': 'Deactivated by administrator',
            }).eq('id', user_id).execute()
            create_notification(user_id, 'Your account has been deactivated by an administrator.')
            log_audit(session.get('user_id'), 'Admin Deactivated User', {'resource_id': user_id, 'result': 'success'})
            _flash_feedback(make_admin_feedback('Deactivate user', detail='User access has been disabled.'))
        except Exception as e:
            _handle_admin_exception('Deactivate user', e, category='admin_user', user_id=user_id)
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/user/<user_id>/reactivate', methods=['POST'])
@login_required
@roles_required('admin')
def reactivate_user(user_id):
    form = DeleteForm()
    if form.validate_on_submit():
        try:
            supabase.table('users').update({
                'active': True,
                'deactivated_at': None,
                'deactivation_reason': None,
            }).eq('id', user_id).execute()
            create_notification(user_id, 'Your account has been reactivated by an administrator.')
            log_audit(session.get('user_id'), 'Admin Reactivated User', {'resource_id': user_id, 'result': 'success'})
            _flash_feedback(make_admin_feedback('Reactivate user', detail='User access has been restored.'))
        except Exception as e:
            _handle_admin_exception('Reactivate user', e, category='admin_user', user_id=user_id)
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/user/delete/<user_id>', methods=['POST'])
@login_required
@roles_required('admin')
def delete_user(user_id):
    form = DeleteForm()
    if form.validate_on_submit():
        try:
            user = supabase.table('users').select('*').eq('id', user_id).single().execute().data
            if not user:
                abort(404)
            _ensure_admin_role_transition_safe(user, new_active=False, new_approved=False, new_role=None)
            dependency_summary = get_user_dependency_summary(user_id)
            if _has_dependencies(dependency_summary):
                raise ValueError(
                    "Use deactivate instead of permanent delete. Existing records found: "
                    + _dependency_warning_text(dependency_summary)
                )
            supabase.table('users').delete().eq('id', user_id).execute()
            log_audit(session.get('user_id'), 'Admin Deleted User', {'resource_id': user_id, 'result': 'success'})
            _flash_feedback(make_admin_feedback('Delete user', detail='User account permanently removed.'))
        except Exception as e:
            _handle_admin_exception('Delete user', e, category='admin_user', user_id=user_id)
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/disapprove/<user_id>', methods=['POST'])
@login_required
@roles_required('admin')
def disapprove_user(user_id):
    try:
        user = supabase.table('users').select('id, approved').eq('id', user_id).single().execute().data
        if not user:
            abort(404)
        if user.get('approved'):
            raise ValueError("Approved users should be deactivated instead of rejected.")
        supabase.table('users').delete().eq('id', user_id).eq('approved', False).execute()
        log_audit(session.get('user_id'), 'Admin Rejected User', {'resource_id': user_id, 'result': 'success'})
        _flash_feedback(make_admin_feedback('Reject user', detail='Pending registration removed.'))
    except Exception as e:
        _handle_admin_exception('Reject user', e, category='admin_user', user_id=user_id)
    return redirect(url_for('admin.admin_dashboard'))

from app.services.rag_service import RAGService
import tempfile
import os

@admin_bp.route('/knowledge_base', methods=['GET', 'POST'])
@login_required
@roles_required('admin')
def manage_knowledge_base():
    # Fetch existing documents
    try:
        docs = supabase.table('knowledge_base').select('id, filename, department_id, created_at').order('created_at', desc=True).execute().data
    except: docs = []
    
    # Fetch departments for the upload form
    try:
        depts = supabase.table('departments').select('*').execute().data
    except: depts = []

    if request.method == 'POST':
        file = request.files.get('file')
        dept_id = request.form.get('department_id')
        
        if file:
            filename = secure_filename(file.filename)
            # Use /tmp indirectly for safety
            with tempfile.NamedTemporaryFile(delete=False, suffix='.docx') as tmp:
                file.save(tmp.name)
                # Ingest
                count = RAGService.ingest_docx(tmp.name, dept_id, filename)
                os.unlink(tmp.name)
            
            flash(f"Ingested {count} chunks from {filename}.", "success")
            return redirect(url_for('admin.manage_knowledge_base'))

    return render_template('admin_knowledge_base.html', docs=docs, departments=depts)

@admin_bp.route('/departments', methods=['GET', 'POST'])
@login_required
@roles_required('admin')
def manage_departments():
    form = DepartmentForm()
    delete_form = DeleteForm()
    if form.validate_on_submit():
        try:
            supabase.table('departments').insert({
                'name': form.name.data.strip(),
                'dean_name': (form.dean_name.data or '').strip(),
                'dean_title': (form.dean_title.data or '').strip(),
                'program_coordinator_name': (form.program_coordinator_name.data or '').strip(),
                'program_coordinator_title': (form.program_coordinator_title.data or '').strip(),
                'vice_president_name': (form.vice_president_name.data or '').strip(),
                'vice_president_title': (form.vice_president_title.data or '').strip(),
            }).execute()
            invalidate_department_caches([form.name.data.strip()])
            log_audit(session.get('user_id'), 'Admin Created Department', {'resource_id': form.name.data.strip()})
            _flash_feedback(make_admin_feedback('Create department', detail='Department added.'))
            return redirect(url_for('admin.manage_departments'))
        except Exception as e:
            _handle_admin_exception('Create department', e, category='admin_department')

    try:
        departments = supabase.table('departments').select('*').order('created_at').execute().data
    except: departments = []

    return render_template('admin_departments.html', form=form, delete_form=delete_form, departments=departments)

@admin_bp.route('/departments/edit/<int:dept_id>', methods=['POST'])
@login_required
@roles_required('admin')
def edit_department(dept_id):
    new_name = request.form.get('new_name')
    if new_name:
        try:
            existing_departments = {row['id']: row['name'] for row in get_department_records()}
            supabase.table('departments').update({
                'name': new_name.strip(),
                'dean_name': (request.form.get('dean_name') or '').strip(),
                'dean_title': (request.form.get('dean_title') or '').strip(),
                'program_coordinator_name': (request.form.get('program_coordinator_name') or '').strip(),
                'program_coordinator_title': (request.form.get('program_coordinator_title') or '').strip(),
                'vice_president_name': (request.form.get('vice_president_name') or '').strip(),
                'vice_president_title': (request.form.get('vice_president_title') or '').strip(),
            }).eq('id', dept_id).execute()
            invalidate_department_caches([existing_departments.get(dept_id), new_name.strip()])
            log_audit(session.get('user_id'), 'Admin Updated Department', {'resource_id': dept_id, 'new_name': new_name.strip()})
            _flash_feedback(make_admin_feedback('Update department', detail='Department updated.'))
        except Exception as e:
            _handle_admin_exception('Update department', e, category='admin_department')
    else:
        flash("Department name cannot be empty.", "warning")
    return redirect(url_for('admin.manage_departments'))

@admin_bp.route('/departments/delete/<int:dept_id>', methods=['POST'])
@login_required
@roles_required('admin')
def delete_department(dept_id):
    form = DeleteForm()
    if form.validate_on_submit():
        try:
            existing_departments = {row['id']: row['name'] for row in get_department_records()}
            existing_name = existing_departments.get(dept_id)
            dependency_summary = get_department_dependency_summary(dept_id, existing_name)
            if _has_dependencies(dependency_summary):
                raise ValueError(
                    "Department still has dependent records: "
                    + _dependency_warning_text(dependency_summary)
                )
            supabase.table('departments').delete().eq('id', dept_id).execute()
            invalidate_department_caches([existing_departments.get(dept_id)])
            log_audit(session.get('user_id'), 'Admin Deleted Department', {'resource_id': dept_id, 'name': existing_name})
            _flash_feedback(make_admin_feedback('Delete department', detail='Department deleted.'))
        except Exception as e:
            _handle_admin_exception('Delete department', e, category='admin_department')
    return redirect(url_for('admin.manage_departments'))


@admin_bp.route('/subjects', methods=['GET'])
@login_required
@roles_required('admin')
def manage_subjects():
    active_semester, active_academic_year = _get_active_term_settings()
    subject_rows = (
        supabase.table('teacher_subjects')
        .select('*')
        .eq('semester', active_semester)
        .eq('academic_year', active_academic_year)
        .order('department')
        .order('course_code')
        .execute()
        .data
        or []
    )
    subjects = [row for row in subject_rows if row.get('user_id') is None]
    return render_template(
        'admin_subjects.html',
        subjects=subjects,
        active_semester=active_semester,
        active_academic_year=active_academic_year,
        delete_form=DeleteForm(),
    )


@admin_bp.route('/subjects/add', methods=['GET', 'POST'])
@login_required
@roles_required('admin')
def add_subject():
    active_semester, active_academic_year = _get_active_term_settings()
    form = TeacherSubjectForm(include_blank_department=True)
    if form.validate_on_submit():
        try:
            supabase.table('teacher_subjects').insert({
                'user_id': None,
                'department': form.department.data,
                'semester': active_semester,
                'academic_year': active_academic_year,
                'course_code': form.course_code.data,
                'course_title': form.course_title.data,
                'course_description': form.course_description.data,
                'type_of_course': form.type_of_course.data,
                'units': form.units.data,
                'contact_hours': form.contact_hours.data,
                'pre_requisites': form.pre_requisites.data,
                'co_requisites': form.co_requisites.data,
            }).execute()
            _flash_feedback(make_admin_feedback('Create subject', detail='Department subject added.'))
            return redirect(url_for('admin.manage_subjects'))
        except Exception as e:
            _handle_admin_exception('Create subject', e, category='admin_subject')
    return render_template(
        'admin_subject_form.html',
        form=form,
        mode='add',
        active_semester=active_semester,
        active_academic_year=active_academic_year,
    )


@admin_bp.route('/subjects/<int:subject_id>/edit', methods=['GET', 'POST'])
@login_required
@roles_required('admin')
def edit_subject(subject_id):
    subject = supabase.table('teacher_subjects').select('*').eq('id', subject_id).single().execute().data
    if not subject or subject.get('user_id') is not None:
        abort(404)

    active_semester, active_academic_year = _get_active_term_settings()
    form = TeacherSubjectForm(include_blank_department=True, data={
        'department': subject.get('department'),
        'course_code': subject.get('course_code'),
        'course_title': subject.get('course_title'),
        'course_description': subject.get('course_description'),
        'type_of_course': subject.get('type_of_course'),
        'units': subject.get('units'),
        'contact_hours': subject.get('contact_hours'),
        'pre_requisites': subject.get('pre_requisites'),
        'co_requisites': subject.get('co_requisites'),
    })
    if form.validate_on_submit():
        try:
            supabase.table('teacher_subjects').update({
                'department': form.department.data,
                'course_code': form.course_code.data,
                'course_title': form.course_title.data,
                'course_description': form.course_description.data,
                'type_of_course': form.type_of_course.data,
                'units': form.units.data,
                'contact_hours': form.contact_hours.data,
                'pre_requisites': form.pre_requisites.data,
                'co_requisites': form.co_requisites.data,
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }).eq('id', subject_id).execute()
            _flash_feedback(make_admin_feedback('Update subject', detail='Department subject updated.'))
            return redirect(url_for('admin.manage_subjects'))
        except Exception as e:
            _handle_admin_exception('Update subject', e, category='admin_subject')

    return render_template(
        'admin_subject_form.html',
        form=form,
        mode='edit',
        subject=subject,
        active_semester=subject.get('semester') or active_semester,
        active_academic_year=subject.get('academic_year') or active_academic_year,
    )


@admin_bp.route('/subjects/<int:subject_id>/delete', methods=['POST'])
@login_required
@roles_required('admin')
def delete_subject(subject_id):
    form = DeleteForm()
    if form.validate_on_submit():
        try:
            subject = supabase.table('teacher_subjects').select('id,user_id').eq('id', subject_id).single().execute().data
            if not subject or subject.get('user_id') is not None:
                abort(404)
            supabase.table('teacher_subjects').delete().eq('id', subject_id).execute()
            _flash_feedback(make_admin_feedback('Delete subject', detail='Department subject deleted.'))
        except Exception as e:
            _handle_admin_exception('Delete subject', e, category='admin_subject')
    return redirect(url_for('admin.manage_subjects'))

@admin_bp.route('/templates', methods=['GET', 'POST'])
@login_required
@roles_required('admin')
def manage_templates():
    form = TemplateUploadForm()
    if form.validate_on_submit():
        file = form.file.data
        filename = secure_filename(file.filename)
        storage_path = f"templates/{int(time.time())}_{filename}"
        
        try:
            supabase.storage.from_(STORAGE_BUCKET_NAME).upload(
                path=storage_path, file=file.read(),
                file_options={"content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
            )
            dept_id = form.department.data if form.department.data else None
            is_def = (form.is_default.data == 'yes')
            
            if is_def:
                supabase.table('templates').update({'is_default': False}).neq('id', 0).execute()

            supabase.table('templates').insert({
                'name': form.name.data, 'filename': storage_path,
                'department_id': dept_id, 'is_default': is_def
            }).execute()

            log_audit(session.get('user_id'), 'Admin Uploaded Template', {'resource_id': storage_path, 'department_id': dept_id})
            _flash_feedback(make_admin_feedback('Upload template', detail='Template uploaded successfully.'))
            return redirect(url_for('admin.manage_templates'))
        except Exception as e:
            log_system_event('storage', 'error', 'Template upload failed', details={'error': str(e), 'filename': filename})
            _handle_admin_exception('Upload template', e, category='storage')

    try:
        res = supabase.table('templates').select('*, departments(name)').order('created_at', desc=True).execute()
        templates = res.data
    except Exception as e:
        templates = []
        _handle_admin_exception('Load templates', e, category='admin_template')

    return render_template('admin_templates.html', form=form, templates=templates, delete_form=DeleteForm())


@admin_bp.route('/templates/beta/generate', methods=['GET', 'POST'])
@login_required
@roles_required('admin')
def generate_template_beta():
    form = TemplateFromCLPBetaForm()
    recent_drafts = []
    ai_assist_available = current_app.config.get('TEMPLATE_BETA_AI_ENABLED', False)

    if form.validate_on_submit():
        source_path = None
        generated_path = None
        try:
            upload = form.source_file.data
            filename = secure_filename(upload.filename or 'source.docx')
            source_bytes = upload.read()
            timestamp = int(time.time())
            draft_name = _safe_template_beta_storage_name(form.name.data)
            department_id = int(form.department.data) if form.department.data else None
            department_name = _beta_template_department_name(department_id)
            department_slug = secure_filename((department_name or 'general').lower()) or 'general'

            source_path = f"templates/beta/source/{timestamp}_{filename}"
            generated_path = f"templates/beta/generated/{department_slug}/{timestamp}_{draft_name}.docx"

            write_storage_bytes(STORAGE_BUCKET_NAME, source_path, source_bytes)
            use_ai = bool(form.ai_assist.data) and ai_assist_available
            generated_bytes, summary = generate_template_from_docx(
                source_bytes,
                filename,
                use_ai=False,
            )
            if use_ai:
                summary['ai_requested'] = True
                summary['used_ai'] = False
                summary['ai_suggestions_applied'] = 0
                summary.setdefault('warnings', []).append(
                    'AI assist was requested and has been queued in the background. Refresh the review page to see AI-applied suggestions when it completes.'
                )
            write_storage_bytes(STORAGE_BUCKET_NAME, generated_path, generated_bytes)

            draft_payload = {
                'name': form.name.data,
                'source_filename': source_path,
                'generated_filename': generated_path,
                'source_type': 'docx',
                'status': 'generated',
                'notes': form.notes.data or '',
                'department_id': department_id,
                'placeholder_summary': summarize_placeholder_summary(summary),
                'warnings': summary.get('warnings', []),
                'generation_meta': {
                    'algorithm_version': summary.get('algorithm_version'),
                    'segments_scanned': summary.get('segments_scanned'),
                    'segments_replaced': summary.get('segments_replaced'),
                    'segments_skipped': summary.get('segments_skipped'),
                    'used_ai': summary.get('used_ai', False),
                    'ai_requested': summary.get('ai_requested', False),
                    'ai_suggestions_applied': summary.get('ai_suggestions_applied', 0),
                    'ai_status': 'queued' if use_ai else 'not_requested',
                    'source_filename': filename,
                },
                'created_by': session.get('user_id'),
            }
            draft_result = supabase.table('generated_template_drafts').insert(draft_payload).execute()
            draft = (draft_result.data or [{}])[0]
            if use_ai and draft.get('id'):
                task_id = TaskQueue.enqueue(
                    'template_beta_ai_assist',
                    {'draft_id': draft.get('id')},
                    user_id=session.get('user_id'),
                    plan_id=None,
                )
                generation_meta = dict(draft.get('generation_meta') or {})
                generation_meta.update(
                    {
                        'ai_status': 'queued' if task_id else 'failed_to_queue',
                        'ai_task_id': task_id,
                    }
                )
                supabase.table('generated_template_drafts').update(
                    {
                        'generation_meta': generation_meta,
                        'updated_at': datetime.now(timezone.utc).isoformat(),
                    }
                ).eq('id', draft.get('id')).execute()
                draft['generation_meta'] = generation_meta

            log_audit(session.get('user_id'), 'Admin Generated Template Beta Draft', {
                'resource_id': draft.get('id'),
                'department_id': department_id,
                'placeholder_count': summary.get('placeholder_count', 0),
                'segments_skipped': summary.get('segments_skipped', 0),
            })
            log_system_event(
                'template_beta',
                'info',
                'Template beta draft generated',
                details={
                    'draft_id': draft.get('id'),
                    'department_id': department_id,
                    'placeholder_count': summary.get('placeholder_count', 0),
                    'segments_skipped': summary.get('segments_skipped', 0),
                },
                user_id=session.get('user_id'),
            )
            flash('Beta template draft generated. Review it before promoting it into the template library.', 'success')
            return redirect(url_for('admin.review_template_beta_draft', draft_id=draft.get('id')))
        except Exception as exc:
            if generated_path:
                try:
                    delete_storage_paths(STORAGE_BUCKET_NAME, [generated_path])
                except Exception:
                    pass
            if source_path:
                try:
                    delete_storage_paths(STORAGE_BUCKET_NAME, [source_path])
                except Exception:
                    pass
            _handle_admin_exception('Generate beta template draft', exc, category='template_beta')

    try:
        recent_result = (
            supabase.table('generated_template_drafts')
            .select('*')
            .order('created_at', desc=True)
            .limit(12)
            .execute()
        )
        recent_drafts = recent_result.data or []
    except Exception as exc:
        _handle_admin_exception('Load beta template drafts', exc, category='template_beta')

    return render_template(
        'admin_template_beta_generate.html',
        form=form,
        drafts=recent_drafts,
        ai_assist_available=ai_assist_available,
    )


@admin_bp.route('/templates/beta/review/<int:draft_id>')
@login_required
@roles_required('admin')
def review_template_beta_draft(draft_id):
    try:
        draft = _load_beta_template_draft(draft_id)
    except Exception as exc:
        _handle_admin_exception('Load beta template draft', exc, category='template_beta')
        return redirect(url_for('admin.generate_template_beta'))

    department_name = _beta_template_department_name(draft.get('department_id'))
    summary = draft.get('placeholder_summary') or {}
    warnings = draft.get('warnings') or []
    generation_meta = draft.get('generation_meta') or {}
    ai_task_status = None
    ai_task_id = generation_meta.get('ai_task_id')
    if ai_task_id:
        try:
            task_row = (
                supabase.table('background_tasks')
                .select('status, error_message, progress_percent, progress_label')
                .eq('id', ai_task_id)
                .single()
                .execute()
            )
            ai_task_status = task_row.data or None
        except Exception:
            ai_task_status = None
    promote_form = TemplateBetaPromoteForm()
    discard_form = DeleteForm()

    return render_template(
        'admin_template_beta_review.html',
        draft=draft,
        department_name=department_name,
        summary=summary,
        warnings=warnings,
        generation_meta=generation_meta,
        ai_task_status=ai_task_status,
        promote_form=promote_form,
        discard_form=discard_form,
        edit_url=url_for('admin.edit_template_beta_draft', draft_id=draft_id),
    )


@admin_bp.route('/templates/beta/edit/<int:draft_id>')
@login_required
@roles_required('admin')
def edit_template_beta_draft(draft_id):
    try:
        draft = _load_beta_template_draft(draft_id)
        if draft.get('status') == 'discarded':
            flash('This beta draft has already been discarded.', 'warning')
            return redirect(url_for('admin.generate_template_beta'))

        doc_title = f"BETA TEMPLATE: {draft['name']}"
        doc_key = _beta_template_doc_key(draft)
        doc_url = f"{_get_onlyoffice_base_url()}/admin/templates/beta/serve/{draft_id}/{doc_key}"
        callback_url = f"{_get_onlyoffice_base_url()}/admin/templates/beta/onlyoffice_callback/{draft_id}"
        config = {
            "document": {
                "title": doc_title,
                "url": doc_url,
                "fileType": "docx",
                "key": doc_key,
                "permissions": {"edit": True, "download": True, "review": True},
            },
            "documentType": "word",
            "editorConfig": {
                "mode": "edit",
                "callbackUrl": callback_url,
                "user": {"id": "admin-beta", "name": "Admin Beta Review"},
                "customization": {
                    "autosave": False,
                    "forcesave": True,
                    "hideRightMenu": False,
                },
            },
            "width": "100%",
            "height": "100%",
        }
        attach_onlyoffice_ai_plugin(config)
        token = generate_jwt_token(config) or ""
        return render_template(
            "teacher_edit_document.html",
            config=config,
            doc_title=doc_title,
            doc_url=doc_url,
            callback_url=callback_url,
            doc_key=doc_key,
            token=token,
            plan_id=None,
            onlyoffice_client_log_url='',
            inserted_success=False,
            inserted_source='',
            inserted_flagged=False,
            alpha_ai_config=None,
            alpha_ai_config_json='{}',
        )
    except Exception as exc:
        _handle_admin_exception('Load beta template editor', exc, category='template_beta')
        return redirect(url_for('admin.generate_template_beta'))


@admin_bp.route('/templates/beta/serve/<int:draft_id>/<doc_key>')
def serve_template_beta_document(draft_id, doc_key):
    started_at = time.perf_counter()
    try:
        draft = _load_beta_template_draft(draft_id)
        expected_key = _beta_template_doc_key(draft)
        if doc_key != expected_key:
            abort(403)
        download_filename = os.path.basename(draft['generated_filename']).split('_', 1)[-1] or f"{draft['name']}.docx"
        response = stream_storage_file(
            STORAGE_BUCKET_NAME,
            draft['generated_filename'],
            download_filename,
            mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            inline=True,
            cache_seconds=300,
        )
        log_document_timing('admin_serve_template_beta', started_at, draft_id=draft_id, filename=draft['generated_filename'])
        return response
    except FileNotFoundError:
        abort(404)
    except Exception:
        abort(500)


@admin_bp.route('/templates/beta/onlyoffice_callback/<int:draft_id>', methods=['POST'])
@csrf.exempt
def onlyoffice_template_beta_callback(draft_id):
    try:
        data = request.get_json(silent=True) or {}
        if not _validate_onlyoffice_callback(data):
            log_system_event('onlyoffice', 'warning', 'Admin beta template callback rejected', details={'draft_id': draft_id, 'status': data.get('status')})
            return jsonify({"error": 1})
        if data.get("status") in [2, 6]:
            draft = _load_beta_template_draft(draft_id)
            download_url = data.get("url")
            file_resp = requests.get(download_url, timeout=30)
            file_resp.raise_for_status()
            write_storage_bytes(STORAGE_BUCKET_NAME, draft['generated_filename'], file_resp.content)
            supabase.table('generated_template_drafts').update({
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }).eq('id', draft_id).execute()
        return jsonify({"error": 0})
    except Exception as exc:
        log_system_event('onlyoffice', 'error', 'Admin beta template callback failed', details={'draft_id': draft_id, 'error': str(exc)})
        return jsonify({"error": 1})


@admin_bp.route('/templates/beta/promote/<int:draft_id>', methods=['POST'])
@login_required
@roles_required('admin')
def promote_template_beta_draft(draft_id):
    form = TemplateBetaPromoteForm()
    if not form.validate_on_submit():
        flash('Invalid beta template promotion request.', 'danger')
        return redirect(url_for('admin.review_template_beta_draft', draft_id=draft_id))

    draft = _load_beta_template_draft(draft_id)
    if draft.get('status') == 'discarded':
        flash('Discarded beta drafts cannot be promoted.', 'warning')
        return redirect(url_for('admin.generate_template_beta'))
    if draft.get('promoted_template_id'):
        flash('This beta draft has already been promoted.', 'warning')
        return redirect(url_for('admin.edit_template', template_id=draft['promoted_template_id']))

    target_path = None
    try:
        generated_bytes = read_storage_bytes(STORAGE_BUCKET_NAME, draft['generated_filename'])
        target_path = f"templates/{int(time.time())}_{_safe_template_beta_storage_name(draft['name'])}.docx"
        write_storage_bytes(STORAGE_BUCKET_NAME, target_path, generated_bytes)

        is_default = form.is_default.data == 'yes'
        if is_default:
            supabase.table('templates').update({'is_default': False}).neq('id', 0).execute()

        created_result = supabase.table('templates').insert({
            'name': draft['name'],
            'filename': target_path,
            'department_id': draft.get('department_id'),
            'is_default': is_default,
        }).execute()
        created_template = (created_result.data or [{}])[0]

        supabase.table('generated_template_drafts').update({
            'status': 'promoted',
            'promoted_template_id': created_template.get('id'),
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }).eq('id', draft_id).execute()

        log_audit(session.get('user_id'), 'Admin Promoted Template Beta Draft', {
            'resource_id': draft_id,
            'promoted_template_id': created_template.get('id'),
        })
        flash('Beta draft promoted into the template library.', 'success')
        return redirect(url_for('admin.edit_template', template_id=created_template.get('id')))
    except Exception as exc:
        if target_path:
            try:
                delete_storage_paths(STORAGE_BUCKET_NAME, [target_path])
            except Exception:
                pass
        _handle_admin_exception('Promote beta template draft', exc, category='template_beta')
        return redirect(url_for('admin.review_template_beta_draft', draft_id=draft_id))


@admin_bp.route('/templates/beta/discard/<int:draft_id>', methods=['POST'])
@login_required
@roles_required('admin')
def discard_template_beta_draft(draft_id):
    form = DeleteForm()
    if not form.validate_on_submit():
        flash('Invalid beta template discard request.', 'danger')
        return redirect(url_for('admin.review_template_beta_draft', draft_id=draft_id))

    draft = _load_beta_template_draft(draft_id)
    if draft.get('promoted_template_id'):
        flash('Promoted beta drafts cannot be discarded.', 'warning')
        return redirect(url_for('admin.edit_template', template_id=draft['promoted_template_id']))

    try:
        supabase.table('generated_template_drafts').update({
            'status': 'discarded',
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }).eq('id', draft_id).execute()
        cleanup_errors = []
        for path in [draft.get('source_filename'), draft.get('generated_filename')]:
            if not path:
                continue
            try:
                delete_storage_paths(STORAGE_BUCKET_NAME, [path])
            except Exception as exc:
                cleanup_errors.append({'path': path, 'error': str(exc)})
        if cleanup_errors:
            log_system_event('storage', 'warning', 'Beta template draft cleanup failed', details={'draft_id': draft_id, 'errors': cleanup_errors})
            flash('Beta draft discarded, but one or more local files could not be removed automatically.', 'warning')
        else:
            flash('Beta draft discarded.', 'success')
        log_audit(session.get('user_id'), 'Admin Discarded Template Beta Draft', {'resource_id': draft_id})
    except Exception as exc:
        _handle_admin_exception('Discard beta template draft', exc, category='template_beta')

    return redirect(url_for('admin.generate_template_beta'))

@admin_bp.route('/templates/delete/<int:template_id>', methods=['POST'])
@login_required
@roles_required('admin')
def delete_template(template_id):
    form = DeleteForm()
    if form.validate_on_submit():
        try:
            res = supabase.table('templates').select('filename').eq('id', template_id).single().execute()
            file_to_remove = res.data.get('filename') if res.data else None
            if not res.data:
                raise ValueError("The selected template could not be found.")
            supabase.table('templates').delete().eq('id', template_id).execute()
            log_audit(session.get('user_id'), 'Admin Deleted Template', {'resource_id': template_id, 'result': 'success'})
            if file_to_remove:
                try:
                    delete_storage_paths(STORAGE_BUCKET_NAME, [file_to_remove])
                    _flash_feedback(make_admin_feedback('Delete template', detail='Template deleted.'))
                except Exception as storage_error:
                    log_audit(session.get('user_id'), 'Admin Deleted Template', {
                        'resource_id': template_id,
                        'result': 'partial_success',
                        'storage_error': str(storage_error),
                    })
                    log_system_event('storage', 'warning', 'Template file cleanup failed after DB delete', details={'template_id': template_id, 'filename': file_to_remove, 'error': str(storage_error)})
                    flash('Template record deleted, but the local file cleanup failed. The file may need manual cleanup.', 'warning')
            else:
                _flash_feedback(make_admin_feedback('Delete template', detail='Template deleted.'))
        except Exception as e:
            log_system_event('storage', 'error', 'Template delete failed', details={'error': str(e), 'template_id': template_id})
            _handle_admin_exception('Delete template', e, category='storage')
    return redirect(url_for('admin.manage_templates'))

@admin_bp.route('/clps')
@login_required
@roles_required('admin')
def manage_clps():
    try:
        plans_res = supabase.table('course_learning_plans').select('*, author:users(username, first_name, last_name)').order('date_posted', desc=True).execute()
        plans = parse_supabase_timestamp(plans_res.data, 'date_posted')
        query = (request.args.get('q') or '').strip().lower()
        status_filter = (request.args.get('status') or '').strip().lower()
        department_filter = (request.args.get('department') or '').strip().lower()

        if query:
            plans = [
                plan for plan in plans
                if query in " ".join([
                    str(plan.get('subject') or ''),
                    str(plan.get('department') or ''),
                    str((plan.get('author') or {}).get('username') or ''),
                    str((plan.get('author') or {}).get('first_name') or ''),
                    str((plan.get('author') or {}).get('last_name') or ''),
                ]).lower()
            ]
        if status_filter:
            plans = [plan for plan in plans if (plan.get('status') or '').lower() == status_filter]
        if department_filter:
            plans = [plan for plan in plans if (plan.get('department') or '').lower() == department_filter]

        dependency_summaries = get_clp_dependency_summaries([plan['id'] for plan in plans])
        department_options = sorted({plan.get('department') for plan in plans if plan.get('department')})
    except Exception as e:
        plans = []
        dependency_summaries = {}
        department_options = []
        query = status_filter = department_filter = ''
        _handle_admin_exception('Load CLPs', e, category='admin_clp')
    return render_template(
        'admin_clps.html',
        plans=plans,
        delete_form=DeleteForm(),
        dependency_summaries=dependency_summaries,
        query=query,
        status_filter=status_filter,
        department_filter=department_filter,
        department_options=department_options,
    )

@admin_bp.route('/clp/<int:plan_id>/delete', methods=['POST'])
@login_required
@roles_required('admin')
def delete_clp(plan_id):
    form = DeleteForm()
    if form.validate_on_submit():
        try:
            res = supabase.table('course_learning_plans').select('filename').eq('id', plan_id).single().execute()
            file_to_remove = res.data.get('filename') if res.data else None
            deleted = delete_clp_with_dependencies(plan_id)
            if not deleted:
                raise ValueError("The selected CLP could not be deleted because it no longer exists.")
            log_audit(session.get('user_id'), 'Admin Deleted CLP', {'resource_id': plan_id})
            if file_to_remove:
                try:
                    delete_storage_paths(STORAGE_BUCKET_NAME, [file_to_remove])
                    _flash_feedback(make_admin_feedback('Delete CLP', detail='CLP deleted with related history cleanup.'))
                except Exception as storage_error:
                    log_audit(session.get('user_id'), 'Admin Deleted CLP', {
                        'resource_id': plan_id,
                        'result': 'partial_success',
                        'storage_error': str(storage_error),
                    })
                    log_system_event('storage', 'warning', 'CLP file cleanup failed after DB delete', details={'plan_id': plan_id, 'filename': file_to_remove, 'error': str(storage_error)}, plan_id=plan_id)
                    flash('CLP record deleted, but the local file cleanup failed. The file may need manual cleanup.', 'warning')
            else:
                _flash_feedback(make_admin_feedback('Delete CLP', detail='CLP deleted with related history cleanup.'))
        except Exception as e:
            log_system_event('storage', 'error', 'CLP delete failed', details={'error': str(e), 'plan_id': plan_id}, plan_id=plan_id)
            _handle_admin_exception('Delete CLP', e, category='admin_clp', plan_id=plan_id)
    return redirect(url_for('admin.manage_clps'))

SECTION_KEYS = {
    'academic': ['institution_name', 'institution_logo_url', 'current_semester',
                 'active_semester', 'active_academic_year', 'submission_deadline',
                 'announcement_text'],
    'ai': ['ai_provider', 'gemini_model', 'gemini_thinking_level', 'ai_temperature', 'daily_ai_limit',
           'prompt_copilot_beta_clo', 'prompt_copilot_beta_alignment',
           'prompt_copilot_beta_weekly'],
    'security': ['allow_signups', 'auto_approve_signups', 'maintenance_mode', 'audit_log_retention_days',
                 'session_lifetime_minutes', 'max_upload_mb', 'admin_alert_email'],
    'workflow': ['require_multi_approval', 'default_rejection_reasons',
                 'use_dynamic_copilot_prompts'],
}


@admin_bp.route('/settings', methods=['GET', 'POST'])
@login_required
@roles_required('admin')
def system_settings():
    form = SystemSettingsForm()
    prompt_defaults = _prompt_setting_defaults()
    client = current_app.config.get('SUPABASE_SERVICE') or supabase
    copilot_defaults = {}
    section = request.args.get('section')
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    
    # All setting keys (full list for GET, filtered for section POST)
    all_setting_keys = [
        'prompt_po_io', 'prompt_co_po', 'prompt_weekly',
        'prompt_copilot_beta_clo', 'prompt_copilot_beta_alignment', 'prompt_copilot_beta_weekly',
        'ai_provider', 'gemini_model', 'gemini_thinking_level', 'ai_temperature', 'daily_ai_limit',
        'current_semester', 'active_semester', 'active_academic_year', 'submission_deadline',
        'institution_name', 'institution_logo_url', 'announcement_text',
        'allow_signups', 'auto_approve_signups', 'maintenance_mode', 'audit_log_retention_days',
        'session_lifetime_minutes', 'max_upload_mb', 'admin_alert_email',
        'require_multi_approval', 'default_rejection_reasons',
        'use_dynamic_copilot_prompts',
    ]
    # Filter keys by section if provided
    setting_keys = SECTION_KEYS.get(section, all_setting_keys) if section else all_setting_keys
    display_only_keys = [COPILOT_DEFAULT_TEMPLATE_NAME_KEY, COPILOT_DEFAULT_TEMPLATE_FILENAME_KEY]

    if form.validate_on_submit():
        try:

            # 1. Handle File Upload (Logo)
            if form.institution_logo_file.data:
                file = form.institution_logo_file.data
                filename = secure_filename(file.filename)
                # Use a specific path for branding in Supabase Storage
                storage_path = f"branding/logo_{int(time.time())}_{filename}"
                
                # Upload to Supabase Storage - Ensuring it's stored in the cloud
                try:
                    # Read the file content once
                    file_content = file.read()
                    supabase.storage.from_(STORAGE_BUCKET_NAME).upload(
                        path=storage_path, 
                        file=file_content,
                        file_options={"content-type": file.mimetype, "upsert": "true"}
                    )
                    
                    # Construct the PERMANENT public URL for Supabase Storage
                    # This ensures the logo is accessible in production
                    new_url = build_public_storage_url(STORAGE_BUCKET_NAME, storage_path)
                    
                    # Update both the form data and a local variable to ensure Step 2 saves it
                    form.institution_logo_url.data = new_url
                    current_app.logger.info(f"New logo uploaded to Supabase: {new_url}")
                except Exception as upload_err:
                    current_app.logger.error(f"Supabase Upload Failed: {upload_err}")
                    log_system_event('storage', 'error', 'Institution logo upload failed', details={'error': str(upload_err), 'path': storage_path})
                    flash(f"Failed to upload logo to local storage: {upload_err}", "warning")

            # 2. Save all textual settings
            updates = []
            for key in setting_keys:
                if hasattr(form, key):
                    val = getattr(form, key).data
                    if key in prompt_defaults and not str(val or '').strip():
                        val = prompt_defaults[key]
                    # SPECIAL CASE: Don't overwrite logo_url with empty if we didn't upload a new one 
                    # and the hidden field didn't send anything (though it should now)
                    if key == 'institution_logo_url' and not val:
                        continue
                    updates.append({'key': key, 'value': str(val) if val is not None else ''})
            
            client.table('system_settings').upsert(updates).execute()
            invalidate_system_settings_cache([update['key'] for update in updates])
            _ensure_production_copilot_default_template(client)
            
            # Update the form data from the saved state to ensure consistency in the re-rendered page
            if request.method == 'POST':
                # Re-fetch settings to ensure we show the saved state
                settings = get_system_settings_map(local_supabase=client, force_refresh=True)
                for key in setting_keys + display_only_keys:
                    if key in settings:
                        field = getattr(form, key, None)
                        if field:
                            field.data = settings[key]

            log_audit(session.get('user_id'), 'Admin Updated System Settings', {'count': len(updates), 'result': 'success', 'section': section})

            if is_ajax:
                return jsonify({'status': 'ok', 'section': section, 'saved_keys': [u['key'] for u in updates]})

            _flash_feedback(make_admin_feedback('Save settings', detail='All system settings updated successfully.'))
            return redirect(url_for('admin.system_settings'))
        except Exception as e:
            current_app.logger.error(f"Settings Save Error: {e}")
            if is_ajax:
                return jsonify({'status': 'error', 'message': str(e), 'section': section}), 400
            _handle_admin_exception('Save settings', e, category='admin_settings')

    if request.method == 'POST' and is_ajax and not form.validate_on_submit():
        errors = {}
        for field_name, field_errors in form.errors.items():
            errors[field_name] = field_errors
        return jsonify({'status': 'error', 'errors': errors, 'section': section}), 400

    if request.method == 'GET':
        settings = {}
        try:
            # Use service client to ensure we get latest settings
            settings, prompt_defaults, copilot_defaults = _ensure_prompt_settings_seeded(client)
            _ensure_production_copilot_default_template(client)
            settings = get_system_settings_map(client, force_refresh=True)
            
            current_app.logger.info(f"Loaded {len(settings)} system settings.")
            
            # Populate form with current values
            for key in setting_keys + display_only_keys:
                if key in settings:
                    field = getattr(form, key, None)
                    if field:
                        field.data = settings[key]
                        if hasattr(field, 'choices') and field.choices is not None:
                            vals = [c[0] for c in field.choices]
                            if settings[key] and settings[key] not in vals:
                                field.choices = [(settings[key], settings[key])] + field.choices
                elif key in prompt_defaults:
                    field = getattr(form, key, None)
                    if field:
                        field.data = prompt_defaults[key]
                elif key in copilot_defaults:
                    field = getattr(form, key, None)
                    if field:
                        field.data = copilot_defaults[key]
            
        except Exception as e:
            current_app.logger.error(f"Settings Load Error: {e}")
            _handle_admin_exception('Load settings', e, category='admin_settings')

    return render_template('admin_settings.html', form=form)


@admin_bp.route('/copilot-data', methods=['GET', 'POST'])
@login_required
@roles_required('admin')
def manage_copilot_data():
    form = CopilotReferenceForm()
    client = current_app.config.get('SUPABASE_SERVICE') or supabase

    if form.validate_on_submit():
        try:
            payload = {
                'department_id': int(form.department.data),
                'category': form.category.data,
                'code': (form.code.data or '').strip() or (form.title.data or '').strip(),
                'title': (form.title.data or '').strip(),
                'description': (form.description.data or '').strip(),
                'sort_order': 0,
            }
            existing = client.table('copilot_reference_entries').select('id').eq('department_id', payload['department_id']).eq('category', payload['category']).execute().data or []
            payload['sort_order'] = len(existing)
            client.table('copilot_reference_entries').insert(payload).execute()
            log_audit(session.get('user_id'), 'Admin Created Copilot Reference Entry', {'category': payload['category'], 'resource_id': payload['code']})
            _flash_feedback(make_admin_feedback('Create copilot reference', detail=f"{_copilot_category_label(payload['category'])} entry added."))
            return redirect(url_for('admin.manage_copilot_data'))
        except Exception as e:
            _handle_admin_exception('Create copilot reference', e, category='admin_copilot_data')

    rows_by_department = {}
    departments = {}
    categories = ['graduate_attributes', 'core_values', 'pqf_level_6', 'aqrf_level_6', 'sdg']
    try:
        dept_rows = client.table('departments').select('*').execute().data or []
        departments = {str(row['id']): row['name'] for row in dept_rows}
        rows_by_department = _ensure_copilot_reference_seeded(client)
    except Exception as e:
        _handle_admin_exception('Load copilot reference data', e, category='admin_copilot_data')

    grouped = {}
    for dept_id, dept_name in departments.items():
        dept_rows = rows_by_department.get(str(dept_id), [])
        grouped[str(dept_id)] = {
            category: sorted(
                [row for row in dept_rows if row.get('category') == category],
                key=lambda row: (row.get('sort_order', 0), row.get('id', 0)),
            )
            for category in categories
        }

    return render_template(
        'admin_copilot_data.html',
        form=form,
        departments=departments,
        grouped_entries=grouped,
        delete_form=DeleteForm(),
        category_labels={category: _copilot_category_label(category) for category in categories},
    )


@admin_bp.route('/copilot-data/delete/<int:entry_id>', methods=['POST'])
@login_required
@roles_required('admin')
def delete_copilot_data_entry(entry_id):
    form = DeleteForm()
    if form.validate_on_submit():
        try:
            client = current_app.config.get('SUPABASE_SERVICE') or supabase
            entry = client.table('copilot_reference_entries').select('*').eq('id', entry_id).single().execute().data
            if not entry:
                raise ValueError("Copilot reference entry no longer exists.")
            client.table('copilot_reference_entries').delete().eq('id', entry_id).execute()
            log_audit(session.get('user_id'), 'Admin Deleted Copilot Reference Entry', {'resource_id': entry_id, 'category': entry.get('category')})
            _flash_feedback(make_admin_feedback('Delete copilot reference', detail=f"{_copilot_category_label(entry.get('category', 'entry'))} entry deleted."))
        except Exception as e:
            _handle_admin_exception('Delete copilot reference', e, category='admin_copilot_data')
    return redirect(url_for('admin.manage_copilot_data'))


@admin_bp.route('/outcomes', methods=['GET', 'POST'])
@login_required
@roles_required('admin')
def manage_outcomes():
    form = OutcomeForm()
    if form.validate_on_submit():
        outcome_type = form.type.data
        data = {'code': form.code.data, 'description': form.description.data}
        if outcome_type != 'institutional' and form.department.data:
            data['department_id'] = int(form.department.data)
        table_name = 'institutional_outcomes'
        if outcome_type == 'program': table_name = 'program_outcomes'
        elif outcome_type == 'course': table_name = 'course_outcomes'
        try:
            supabase.table(table_name).insert(data).execute()
            
            departments_by_id = {str(row['id']): row['name'] for row in get_department_records()}
            if outcome_type == 'institutional':
                invalidate_department_caches()
            elif form.department.data:
                invalidate_department_caches([departments_by_id.get(form.department.data)])
            
            log_audit(session.get('user_id'), 'Admin Created Outcome', {'type': outcome_type, 'resource_id': form.code.data})
            _flash_feedback(make_admin_feedback('Create outcome', detail=f"{outcome_type.title()} outcome added."))
            return redirect(url_for('admin.manage_outcomes'))
        except Exception as e:
            _handle_admin_exception('Create outcome', e, category='admin_outcome')

    try:
        depts_res = supabase.table('departments').select('*').execute()
        departments = {d['id']: d['name'] for d in depts_res.data}
        pos = supabase.table('program_outcomes').select('*').order('code').execute().data
        cos = supabase.table('course_outcomes').select('*').order('code').execute().data
        ios = supabase.table('institutional_outcomes').select('*').order('id').execute().data
    except Exception as e:
        pos, cos, ios, departments = [], [], [], {}
        _handle_admin_exception('Load outcomes', e, category='admin_outcome')

    return render_template('admin_outcomes.html', form=form, 
                           program_outcomes=pos, course_outcomes=cos, 
                           institutional_outcomes=ios, departments=departments,
                           delete_form=DeleteForm())

@admin_bp.route('/outcomes/delete/<type>/<int:id>', methods=['POST'])
@login_required
@roles_required('admin')
def delete_outcome(type, id):
    form = DeleteForm()
    if form.validate_on_submit():
        table_name = 'institutional_outcomes'
        if type == 'program': table_name = 'program_outcomes'
        elif type == 'course': table_name = 'course_outcomes'
        try:
            outcome = supabase.table(table_name).select('*').eq('id', id).single().execute().data
            if not outcome:
                abort(404)
            dependency_summary = get_outcome_dependency_summary(type, outcome.get('code'))
            if _has_dependencies(dependency_summary):
                raise ValueError(
                    "Outcome still participates in CLP mappings: "
                    + _dependency_warning_text(dependency_summary, {
                        'mappings_as_source': 'source mappings',
                        'mappings_as_target': 'target mappings',
                    })
                )
            supabase.table(table_name).delete().eq('id', id).execute()
            invalidate_department_caches()
            log_audit(session.get('user_id'), 'Admin Deleted Outcome', {'type': type, 'resource_id': outcome.get('code')})
            _flash_feedback(make_admin_feedback('Delete outcome', detail='Outcome deleted.'))
        except Exception as e:
            _handle_admin_exception('Delete outcome', e, category='admin_outcome')
    return redirect(url_for('admin.manage_outcomes'))

@admin_bp.route('/templates/edit/<int:template_id>')
@login_required
@roles_required('admin')
def edit_template(template_id):
    try:
        res = supabase.table('templates').select('*').eq('id', template_id).single().execute()
        template = res.data
        if not template: return redirect(url_for('admin.manage_templates'))
        
        filename = template['filename']
        doc_title = template['name']
        key_string = f"tmpl_{template['id']}_{filename}"
        doc_key = hashlib.md5(key_string.encode()).hexdigest()
        doc_url = f"{_get_onlyoffice_base_url()}/admin/serve_template/{template_id}/{doc_key}"
        
        callback_url = f"{_get_onlyoffice_base_url()}/admin/onlyoffice_callback/{template_id}"
        config = {
            "document": {
                "title": doc_title, "url": doc_url, "fileType": "docx", "key": doc_key,
                "permissions": {"edit": True, "download": True, "review": True}
            },
            "documentType": "word",
            "editorConfig": {
                "mode": "edit", 
                "callbackUrl": callback_url,
                "user": {"id": "admin", "name": "Admin User"},
                "customization": {
                    "autosave": False, 
                    "forcesave": True,
                    "hideRightMenu": False
                }
            },
            "width": "100%", "height": "100%"
        }
        attach_onlyoffice_ai_plugin(config)
        
        # Generate JWT Token
        token = generate_jwt_token(config) or ""
        
        return render_template("admin_edit_template.html", 
                               config=config,
                               doc_title=doc_title, doc_url=doc_url, 
                               callback_url=callback_url,
                               doc_key=doc_key, token=token)
    except Exception as e:
        flash(f"Error: {e}", "danger")
        return redirect(url_for('admin.manage_templates'))

@admin_bp.route('/clp/<int:plan_id>/edit_document')
@login_required
@roles_required('admin')
def admin_edit_clp_document(plan_id):
    started_at = time.perf_counter()
    try:
        res = supabase.table('course_learning_plans').select('id, filename, subject').eq('id', plan_id).single().execute()
        plan = res.data
        if not plan: abort(404)
        
        if not plan.get('filename') or not plan['filename'].lower().endswith('.docx'):
            flash('This plan is not a .docx document and cannot be edited in the document editor.', 'warning')
            return redirect(url_for('admin.manage_clps'))

        doc_title = f"ADMIN EDIT: {plan['subject']}"
        filename = plan['filename']
        key_string = f"admin_edit_{plan['id']}_{filename}"
        doc_key = hashlib.md5(key_string.encode()).hexdigest()
        
        doc_url = f"{_get_onlyoffice_base_url()}/admin/serve_clp/{plan_id}/{doc_key}"
        
        callback_url = f"{_get_onlyoffice_base_url()}/admin/onlyoffice_clp_callback/{plan_id}"

        config = {
            "document": {
                "title": doc_title, "url": doc_url, "fileType": "docx", "key": doc_key,
                "permissions": {"edit": True, "download": True, "review": True}
            },
            "documentType": "word",
            "editorConfig": {
                "mode": "edit", 
                "callbackUrl": callback_url,
                "user": {"id": "admin", "name": "System Administrator"},
                "customization": {
                    "autosave": False, 
                    "forcesave": True,
                    "hideRightMenu": False
                }
            },
            "width": "100%", "height": "100%"
        }
        attach_onlyoffice_ai_plugin(config)
        
        # Generate JWT Token for ONLYOFFICE
        token = generate_jwt_token(config) or ""
        log_document_timing('admin_edit_document_config', started_at, plan_id=plan_id, user_id=session.get('user_id'))
        
        # Reuse the teacher template as it has the responsive styling and back button
        return render_template("teacher_edit_document.html", 
                               config=config,
                               doc_title=doc_title, doc_url=doc_url, 
                               callback_url=callback_url, 
                               doc_key=doc_key, token=token,
                               plan_id=plan_id,
                               onlyoffice_client_log_url='',
                               inserted_success=False,
                               inserted_source='',
                               inserted_flagged=False,
                               alpha_ai_config=None,
                               alpha_ai_config_json='{}')
    except Exception as e:
        flash(f"Error: {e}", "danger")
        return redirect(url_for('admin.manage_clps'))


@admin_bp.route('/serve_template/<int:template_id>/<doc_key>')
def serve_template_document(template_id, doc_key):
    started_at = time.perf_counter()
    try:
        res = supabase.table('templates').select('filename, name').eq('id', template_id).single().execute()
        template = res.data
        if not template or not template.get('filename'):
            abort(404)
        expected_key = hashlib.md5(f"tmpl_{template_id}_{template['filename']}".encode()).hexdigest()
        if doc_key != expected_key:
            abort(403)
        download_filename = os.path.basename(template['filename']).split('_', 1)[-1] or f"{template['name']}.docx"
        response = stream_storage_file(
            STORAGE_BUCKET_NAME,
            template['filename'],
            download_filename,
            mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            inline=True,
            cache_seconds=300,
        )
        log_document_timing('admin_serve_template', started_at, template_id=template_id, filename=template['filename'])
        return response
    except FileNotFoundError:
        abort(404)
    except Exception:
        abort(500)


@admin_bp.route('/serve_clp/<int:plan_id>/<doc_key>')
def serve_clp_document(plan_id, doc_key):
    started_at = time.perf_counter()
    try:
        res = supabase.table('course_learning_plans').select('filename, subject, user_id, department, content, upload_type').eq('id', plan_id).single().execute()
        plan = res.data
        if not plan or not plan.get('filename'):
            abort(404)
        expected_key = hashlib.md5(f"admin_edit_{plan_id}_{plan['filename']}".encode()).hexdigest()
        if doc_key != expected_key:
            abort(403)
        download_filename = os.path.basename(plan['filename']).split('_', 1)[-1] or f"{plan['subject']}.docx"
        recovered = False
        try:
            response = stream_storage_file(
                STORAGE_BUCKET_NAME,
                plan['filename'],
                download_filename,
                mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                inline=True,
                cache_seconds=300,
            )
        except FileNotFoundError:
            recovered = True
            plan['id'] = plan_id
            plan['filename'] = ensure_plan_document_file(plan)
            download_filename = os.path.basename(plan['filename']).split('_', 1)[-1] or f"{plan['subject']}.docx"
            response = stream_storage_file(
                STORAGE_BUCKET_NAME,
                plan['filename'],
                download_filename,
                mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                inline=True,
                cache_seconds=300,
            )
        log_document_timing('admin_serve_clp', started_at, plan_id=plan_id, filename=plan['filename'], recovered=recovered)
        return response
    except FileNotFoundError:
        abort(404)
    except Exception:
        abort(500)

@admin_bp.route('/onlyoffice_clp_callback/<int:plan_id>', methods=['POST'])
@csrf.exempt
def admin_onlyoffice_clp_callback(plan_id):
    try:
        data = request.get_json(silent=True) or {}
        if not _validate_onlyoffice_callback(data):
            log_system_event('onlyoffice', 'warning', 'Admin CLP callback rejected', details={'plan_id': plan_id, 'status': data.get('status')}, plan_id=plan_id)
            return jsonify({"error": 1})
        if data.get("status") in [2, 6]:
            download_url = data.get("url")
            file_resp = requests.get(download_url, timeout=30)
            file_resp.raise_for_status()
            res = supabase.table('course_learning_plans').select('filename').eq('id', plan_id).single().execute()
            old_path = res.data['filename']
            write_storage_bytes(STORAGE_BUCKET_NAME, old_path, file_resp.content)
        return jsonify({"error": 0})
    except Exception as exc:
        log_system_event('onlyoffice', 'error', 'Admin CLP callback failed', details={'plan_id': plan_id, 'error': str(exc)}, plan_id=plan_id)
        return jsonify({"error": 1})

@admin_bp.route('/onlyoffice_callback/<int:template_id>', methods=['POST'])
@csrf.exempt
def onlyoffice_callback(template_id):
    try:
        data = request.get_json(silent=True) or {}
        if not _validate_onlyoffice_callback(data):
            log_system_event('onlyoffice', 'warning', 'Admin template callback rejected', details={'template_id': template_id, 'status': data.get('status')})
            return jsonify({"error": 1})
        if data.get("status") in [2, 6]:
            download_url = data.get("url")
            file_resp = requests.get(download_url, timeout=30)
            file_resp.raise_for_status()
            res = supabase.table('templates').select('filename').eq('id', template_id).single().execute()
            old_path = res.data['filename']
            write_storage_bytes(STORAGE_BUCKET_NAME, old_path, file_resp.content)
        return jsonify({"error": 0})
    except Exception as exc:
        log_system_event('onlyoffice', 'error', 'Admin template callback failed', details={'template_id': template_id, 'error': str(exc)})
        return jsonify({"error": 1})


# ---------------------------------------------------------------------------
# Admin: Teacher Template Profiles oversight
# ---------------------------------------------------------------------------

@admin_bp.route('/template-profiles')
@login_required
@roles_required('admin')
def admin_template_profiles():
    """List all teacher template profiles across departments."""
    try:
        res = supabase.table('teacher_template_profiles').select(
            'id,user_id,department,name,source_filename,status,confirmed_at,created_at,updated_at,users(first_name,last_name,email)'
        ).order('created_at', desc=True).execute()
        profiles = res.data or []
    except Exception as e:
        profiles = []
        _handle_admin_exception('Load template profiles', e, category='admin_template_profiles')

    return render_template('admin_template_profiles.html', profiles=profiles, delete_form=DeleteForm())


@admin_bp.route('/template-profiles/<int:profile_id>/delete', methods=['POST'])
@login_required
@roles_required('admin')
def admin_delete_template_profile(profile_id):
    """Delete a teacher template profile (admin override)."""
    try:
        in_use = supabase.table('course_learning_plans').select('id').eq('template_profile_id', profile_id).limit(1).execute()
        if in_use.data:
            flash('This template profile is attached to one or more CLPs. Remove or reassign it before deleting.', 'warning')
            return redirect(url_for('admin.admin_template_profiles'))
        supabase.table('teacher_template_profiles').delete().eq('id', profile_id).execute()
        log_audit(session.get('user_id'), 'Admin Deleted Template Profile', {'profile_id': profile_id})
        flash('Template profile deleted.', 'success')
    except Exception as e:
        _handle_admin_exception('Delete template profile', e, category='admin_template_profiles')
    return redirect(url_for('admin.admin_template_profiles'))


@admin_bp.route('/template-profiles/<int:profile_id>/archive', methods=['POST'])
@login_required
@roles_required('admin')
def admin_archive_template_profile(profile_id):
    """Archive a teacher template profile."""
    try:
        supabase.table('teacher_template_profiles').update({'status': 'archived'}).eq('id', profile_id).execute()
        log_audit(session.get('user_id'), 'Admin Archived Template Profile', {'profile_id': profile_id})
        flash('Template profile archived.', 'success')
    except Exception as e:
        _handle_admin_exception('Archive template profile', e, category='admin_template_profiles')
    return redirect(url_for('admin.admin_template_profiles'))


@admin_bp.route('/template-profiles/<int:profile_id>/re-render', methods=['POST'])
@login_required
@roles_required('admin')
def admin_rerender_profile_clps(profile_id):
    """Queue re-finalization for all CLPs using a specific template profile."""
    try:
        res = supabase.table('course_learning_plans').select('id,user_id,status,upload_type,template_profile_id').eq('template_profile_id', profile_id).execute()
        plans = res.data or []
        if not plans:
            flash('No CLPs are using this profile.', 'info')
            return redirect(url_for('admin.admin_template_profiles'))

        queued = 0
        failed = 0
        for plan in plans:
            if plan.get('upload_type') != 'ai_copilot_beta':
                continue
            if plan.get('status') not in ('beta_review', 'beta_ready', 'beta_finalized', 'finalized'):
                continue
            try:
                task_id = TaskQueue.enqueue(
                    "beta_action",
                    {"action": "finalize_document", "form_data": {}},
                    user_id=plan.get('user_id'),
                    plan_id=plan.get('id'),
                )
                if task_id:
                    queued += 1
                else:
                    failed += 1
            except Exception as exc:
                failed += 1
                current_app.logger.warning("Admin bulk re-render queue failed for plan %s: %s", plan.get('id'), exc)

        log_audit(session.get('user_id'), 'Admin Bulk Re-render', {'profile_id': profile_id, 'plans_queued': queued, 'plans_failed': failed})
        if failed:
            flash(f'Re-render queued for {queued} CLP(s); {failed} could not be queued.', 'warning')
        else:
            flash(f'Re-render queued for {queued} CLP(s) using this profile.', 'success')
    except Exception as e:
        _handle_admin_exception('Bulk re-render', e, category='admin_template_profiles')
    return redirect(url_for('admin.admin_template_profiles'))


@admin_bp.route('/template-profiles/analytics')
@login_required
@roles_required('admin')
def admin_template_profile_analytics():
    """Show usage statistics for template profiles."""
    analytics = []
    try:
        profiles = supabase.table('teacher_template_profiles').select(
            'id,name,department,status,is_department_default,user_id,users(first_name,last_name)'
        ).eq('status', 'confirmed').order('department').execute()

        for p in (profiles.data or []):
            pid = p['id']
            # Count CLPs using this profile.
            clp_res = supabase.table('course_learning_plans').select('id,status').eq('template_profile_id', pid).execute()
            clps = clp_res.data or []
            total_clps = len(clps)
            finalized = sum(1 for c in clps if c.get('status') in ('beta_finalized', 'finalized', 'approved', 'pending'))

            # Count system events for fallbacks.
            fallback_count = 0
            try:
                ev_res = supabase.table('system_events').select('id').eq('category', 'template_profile').execute()
                # Filter for this profile's plans.
                plan_ids = {c['id'] for c in clps}
                fallback_count = sum(1 for _ in (ev_res.data or []) if True)  # Approximate — events don't store profile_id directly.
            except Exception:
                pass

            analytics.append({
                'profile': p,
                'total_clps': total_clps,
                'finalized_clps': finalized,
                'fallback_events': fallback_count if total_clps > 0 else 0,
            })
    except Exception as e:
        _handle_admin_exception('Load profile analytics', e, category='admin_template_profiles')

    return render_template('admin_template_profile_analytics.html', analytics=analytics)


@admin_bp.route('/template-profiles/<int:profile_id>/set-default', methods=['POST'])
@login_required
@roles_required('admin')
def admin_set_default_template_profile(profile_id):
    """Promote/demote a profile as department default."""
    try:
        row = supabase.table('teacher_template_profiles').select('id,department,is_department_default,status').eq('id', profile_id).single().execute()
        if not row.data:
            abort(404)
        if row.data.get('status') != 'confirmed':
            flash('Only confirmed profiles can be set as department default.', 'warning')
            return redirect(url_for('admin.admin_template_profiles'))

        is_default = row.data.get('is_department_default', False)
        if is_default:
            # Demote.
            supabase.table('teacher_template_profiles').update({'is_department_default': False}).eq('id', profile_id).execute()
            log_audit(session.get('user_id'), 'Admin Removed Dept Default Profile', {'profile_id': profile_id})
            flash('Profile is no longer the department default.', 'info')
        else:
            # Clear other defaults for this department, then promote.
            dept = row.data.get('department', '')
            supabase.table('teacher_template_profiles').update({'is_department_default': False}).eq('department', dept).eq('is_department_default', True).execute()
            supabase.table('teacher_template_profiles').update({'is_department_default': True}).eq('id', profile_id).execute()
            log_audit(session.get('user_id'), 'Admin Set Dept Default Profile', {'profile_id': profile_id, 'department': dept})
            flash(f'Profile set as default for {dept}.', 'success')
    except Exception as e:
        _handle_admin_exception('Set default template profile', e, category='admin_template_profiles')
    return redirect(url_for('admin.admin_template_profiles'))
