# app/blueprints/main.py

# from flask import Blueprint, render_template, redirect, url_for, session, jsonify, request, flash, current_app
# from supabase import PostgrestAPIError
# from app import supabase, limiter
# from app.decorators import login_required
# from app.utils import parse_supabase_timestamp
from io import BytesIO
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
import jwt
from flask import Blueprint, render_template, redirect, url_for, session, jsonify, request, flash, current_app, Response, send_file, stream_with_context
from app.compat_supabase import PostgrestAPIError
from app import supabase, limiter, cache, csrf
from app.decorators import login_required, roles_required
from app.utils import (
    parse_supabase_timestamp,
    get_unread_notifications_count,
    invalidate_notifications_cache,
    stream_storage_file,
    log_document_timing,
    log_system_event,
    get_current_user_profile,
)
from app.services.ai_buddy_service import AIBuddyService
from app.services.ai_client import is_valid_gemini_api_key
import requests
import time

# Create a Blueprint instance
main_bp = Blueprint('main', __name__)


def _onlyoffice_ai_proxy_origin(response):
    origin = request.headers.get("Origin", "")
    allowed = {
        "https://aipclpms.otakunity.com",
        "https://aipclpms-of.otakunity.com",
        "http://aipclpms-of.otakunity.com",
    }
    onlyoffice_api_url = current_app.config.get("ONLYOFFICE_API_JS_URL", "")
    if onlyoffice_api_url:
        parsed = urlparse(onlyoffice_api_url)
        if parsed.scheme and parsed.netloc:
            allowed.add(f"{parsed.scheme}://{parsed.netloc}")
            if parsed.scheme == "http":
                allowed.add(f"https://{parsed.netloc}")
    if origin in allowed:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return response


def _validate_onlyoffice_ai_proxy_token():
    secret = current_app.config.get("ONLYOFFICE_JWT_SECRET", "")
    if not secret:
        return False
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return False
    token = auth_header.split(" ", 1)[1].strip()
    try:
        jwt.decode(token, secret, algorithms=["HS256"])
    except Exception:
        return False
    return True


def _build_onlyoffice_ai_proxy_target(raw_target):
    parsed = urlparse(raw_target or "")
    if parsed.scheme != "https" or parsed.netloc != "generativelanguage.googleapis.com":
        return None
    gemini_key = (current_app.config.get("GEMINI_API_KEY") or "").strip()
    if not is_valid_gemini_api_key(gemini_key):
        return None
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["key"] = gemini_key
    return urlunparse(parsed._replace(query=urlencode(query)))

@main_bp.route('/license', methods=['GET', 'POST'])
def license_setup():
    from license_manager import license_manager
    # If already licensed, redirect to index
    if license_manager.check_local_license():
        return redirect(url_for('main.index'))
        
    if request.method == 'POST':
        license_key = request.form.get('license_key')
        domain = request.host
        
        if not license_key:
            flash("Please provide a license key.", "danger")
            return redirect(url_for('main.license_setup'))
            
        result = license_manager.activate_license(license_key, domain)
        if result.get('success') is True or result.get('status') in ['success', 'valid'] or 'license_id' in result:
            flash("License activated successfully!", "success")
            return redirect(url_for('main.index'))
        else:
            flash(f"Invalid license key or activation failed: {result.get('message', 'Unknown error')}", "danger")
            return redirect(url_for('main.license_setup'))
            
    return render_template('license_setup.html')

@main_bp.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('main.dashboard'))
    return render_template('landing.html')

@main_bp.route('/docs')
def docs_public():
    return render_template('docs_public.html')

@main_bp.route('/docs/teacher')
@login_required
@roles_required('teacher')
def docs_teacher():
    return render_template('docs_teacher.html')

@main_bp.route('/docs/dean')
@login_required
@roles_required('dean')
def docs_dean():
    return render_template('docs_dean.html')

@main_bp.route('/docs/admin')
@login_required
@roles_required('admin')
def docs_admin():
    return render_template('docs_admin.html')


@main_bp.route('/storage/v1/object/public/<bucket>/<path:key>')
def public_storage_object(bucket, key):
    started_at = time.perf_counter()
    try:
        download_name = key.rsplit('/', 1)[-1] or 'file'
        response = stream_storage_file(
            bucket,
            key,
            download_name,
            mimetype='application/octet-stream',
            inline=True,
            cache_seconds=3600,
        )
        response.headers["Cache-Control"] = 'public, max-age=3600, stale-while-revalidate=300'
        log_document_timing('public_storage_served', started_at, bucket=bucket, key=key)
        return response
    except FileNotFoundError:
        return render_template('404.html'), 404
    except Exception as e:
        current_app.logger.error(f"Error serving local storage object {bucket}/{key}: {e}")
        return render_template('404.html'), 404

# --------------------------
# Reverse Proxy Route
# --------------------------
DOCKER_TARGET = "http://100.0.0.11:8080"  # your Docker container

@main_bp.route('/web-apps/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE'])
@login_required
def proxy_webapps(path):
    # Build full URL to Docker container
    target_url = f"{DOCKER_TARGET}/web-apps/{path}"

    # Forward headers (remove Host to avoid conflicts)
    headers = dict(request.headers)
    headers.pop('Host', None)

    # Forward the request to the container
    resp = requests.request(
        method=request.method,
        url=target_url,
        headers=headers,
        data=request.get_data(),
        cookies=request.cookies,
        allow_redirects=False
    )

    # Return response back to client
    excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
    response_headers = [(name, value) for (name, value) in resp.raw.headers.items()
                        if name.lower() not in excluded_headers]

    return Response(resp.content, resp.status_code, response_headers)


@main_bp.route('/onlyoffice/ai-proxy', methods=['POST', 'OPTIONS'])
@csrf.exempt
@limiter.exempt
def onlyoffice_ai_proxy():
    if request.method == 'OPTIONS':
        return _onlyoffice_ai_proxy_origin(Response(status=204))

    if not _validate_onlyoffice_ai_proxy_token():
        return _onlyoffice_ai_proxy_origin(jsonify({'error': {'message': 'Unauthorized'}})), 401

    payload = request.get_json(silent=True) or {}
    target = _build_onlyoffice_ai_proxy_target(payload.get('target'))
    if not target:
        return _onlyoffice_ai_proxy_origin(jsonify({'error': {'message': 'Unsupported AI target'}})), 400

    method = (payload.get('method') or 'POST').upper()
    if method not in {'GET', 'POST'}:
        return _onlyoffice_ai_proxy_origin(jsonify({'error': {'message': 'Unsupported AI method'}})), 400

    upstream_headers = {
        'Content-Type': 'application/json',
    }
    incoming_headers = payload.get('headers') or {}
    content_type = incoming_headers.get('Content-Type') or incoming_headers.get('content-type')
    if content_type:
        upstream_headers['Content-Type'] = content_type

    upstream_data = payload.get('data')
    try:
        upstream = requests.request(
            method=method,
            url=target,
            headers=upstream_headers,
            data=upstream_data,
            stream=True,
            timeout=120,
        )
    except requests.RequestException as exc:
        current_app.logger.warning(f"OnlyOffice AI proxy request failed: {exc}")
        return _onlyoffice_ai_proxy_origin(jsonify({'error': {'message': 'AI proxy upstream request failed'}})), 502

    excluded_headers = {'content-length', 'transfer-encoding', 'connection', 'content-encoding'}
    response_headers = {
        key: value for key, value in upstream.headers.items()
        if key.lower() not in excluded_headers
    }

    response = Response(
        stream_with_context(upstream.iter_content(chunk_size=8192)),
        status=upstream.status_code,
        headers=response_headers,
        direct_passthrough=True,
    )
    return _onlyoffice_ai_proxy_origin(response)

@main_bp.route('/dashboard')
@login_required
def dashboard():
    role = session.get('role')
    user_id = session.get('user_id')
    unread_notifications_count = 0
    
    # Redirect or Render based on Role
    if role == 'admin':
        return redirect(url_for('admin.admin_dashboard'))
    
    elif role == 'dean':
        # Redirect Dean to their specific dashboard which handles department filtering
        return redirect(url_for('dean.dashboard'))
    
    elif role == 'teacher':
        unread_notifications_count = get_unread_notifications_count(user_id)
        cache_key = f'teacher_dashboard_stats:{user_id}'
        stats = cache.get(cache_key)
        if stats is None:
            stats = {'approved': 0, 'pending': 0, 'returned': 0, 'draft': 0}
            try:
                res = supabase.table('course_learning_plans').select('status').eq('user_id', user_id).execute()
                for row in res.data or []:
                    status = row.get('status')
                    if status == 'approved':
                        stats['approved'] += 1
                    elif status == 'pending':
                        stats['pending'] += 1
                    elif status == 'returned_for_revision':
                        stats['returned'] += 1
                    else:
                        stats['draft'] += 1
                cache.set(cache_key, stats, timeout=30)
            except Exception:
                pass
        
        # Template profile summary for dashboard.
        profile_stats = {'confirmed': 0, 'draft': 0, 'has_department_default': False}
        try:
            tp_res = supabase.table('teacher_template_profiles').select('status').eq('user_id', user_id).execute()
            for tp_row in tp_res.data or []:
                if tp_row.get('status') == 'confirmed':
                    profile_stats['confirmed'] += 1
                elif tp_row.get('status') == 'draft':
                    profile_stats['draft'] += 1
        except Exception:
            pass

        # Subject count for dashboard workflow.
        subject_count = 0
        try:
            sub_res = supabase.table('teacher_subjects').select('id').eq('user_id', user_id).execute()
            subject_count = len(sub_res.data or [])
        except Exception:
            pass

        # Departments for creation form.
        departments = []
        try:
            dept_res = supabase.table('departments').select('id,name').order('name').execute()
            departments = dept_res.data or []
        except Exception:
            pass

        # Check onboarding state.
        onboarding = not profile_stats['confirmed'] and subject_count == 0

        return render_template('teacher_dashboard.html', 
                               unread_notifications=unread_notifications_count,
                               stats=stats,
                               profile_stats=profile_stats,
                               subject_count=subject_count,
                               onboarding=onboarding,
                               departments=departments)
        
    else:
        # A new user who is not yet approved might not have a role.
        flash("Your role is not defined. Please contact an administrator.", "warning")
        return redirect(url_for('auth.login'))

# --- NOTIFICATION ROUTES ---

@main_bp.route('/notifications')
@login_required
def list_notifications():
    try:
        notif_res = supabase.table('notifications').select('id, message, is_read, timestamp, reference_type, reference_id').eq('user_id', session['user_id']).order('timestamp', desc=True).execute()
        notifications_with_dates = parse_supabase_timestamp(notif_res.data, 'timestamp')
        return render_template('notifications.html', notifications=notifications_with_dates)
    except Exception as e:
        flash("Could not load notifications due to a network error.", "danger")
        return render_template('notifications.html', notifications=[])

@main_bp.route('/check_notifications')
@login_required
@limiter.exempt
def check_notifications():
    try:
        return jsonify({'unread_count': get_unread_notifications_count(session['user_id'])})
    except Exception:
        # Silently fail for polling endpoints to prevent log spam and 500 errors
        return jsonify({'unread_count': 0})

@main_bp.route('/notifications/mark_read/<int:notification_id>', methods=['POST'])
@login_required
def mark_notification_read(notification_id):
    supabase.table('notifications').update({'is_read': True}).eq('id', notification_id).eq('user_id', session['user_id']).execute()
    invalidate_notifications_cache(session['user_id'])
    flash('Notification marked as read.', 'info')
    return redirect(url_for('main.list_notifications'))

@main_bp.route('/notifications/mark_all_read', methods=['POST'])
@login_required
def mark_all_read():
    try:
        supabase.table('notifications').update({'is_read': True}).eq('user_id', session['user_id']).execute()
        invalidate_notifications_cache(session['user_id'])
        flash('All notifications have been marked as read.', 'info')
    except Exception as e:
        flash(f'Error marking all notifications as read: {e}', 'danger')
    return redirect(url_for('main.list_notifications'))

@main_bp.route('/notifications/delete_read', methods=['POST'])
@login_required
def delete_read_notifications():
    try:
        # Delete only notifications that are read
        supabase.table('notifications').delete().eq('user_id', session['user_id']).eq('is_read', True).execute()
        invalidate_notifications_cache(session['user_id'])
        flash('All read notifications have been deleted.', 'success')
    except Exception as e:
        flash(f'Error deleting read notifications: {e}', 'danger')
    return redirect(url_for('main.list_notifications'))


@main_bp.route('/buddy')
@login_required
def buddy_beta():
    route_hint = request.args.get('route_hint') or request.referrer or request.path
    context = AIBuddyService.build_context(session.get('user_id'), session.get('role'), route_hint=route_hint)
    return render_template(
        'buddy_beta.html',
        role=session.get('role'),
        context_summary=context.get('context_summary', {}),
        docs=context.get('docs', []),
        starter_actions=context.get('allowed_actions', [])[:4],
    )


@main_bp.route('/buddy/history')
@login_required
def buddy_history():
    return jsonify({
        'history': AIBuddyService.get_session_history(),
        'role': session.get('role'),
    })


@main_bp.route('/buddy/message', methods=['POST'])
@login_required
@limiter.exempt
def buddy_message():
    payload = request.get_json(silent=True) or {}
    message = (payload.get('message') or '').strip()
    route_hint = payload.get('route_hint') or request.referrer or request.path

    if not message:
        return jsonify({'error': 'A message is required.'}), 400

    try:
        history = AIBuddyService.get_session_history()
        context = AIBuddyService.build_context(session.get('user_id'), session.get('role'), route_hint=route_hint)
        response = AIBuddyService.respond(message, context, history)
        AIBuddyService.append_session_history('user', message)
        AIBuddyService.append_session_history('assistant', response.reply)
        return jsonify(response.to_dict())
    except Exception as exc:
        current_app.logger.warning(f"Buddy message failed: {exc}")
        log_system_event(
            'buddy',
            'warning',
            'Buddy message failed',
            details={'error': str(exc), 'role': session.get('role')},
            user_id=session.get('user_id'),
        )
        return jsonify({
            'reply': 'The AI Buddy could not prepare a response right now. Please try again in a moment.',
            'citations': [],
            'context_summary': {},
            'suggested_actions': [],
            'requires_confirmation': False,
            'session_state': {'history_count': len(AIBuddyService.get_session_history()), 'role': session.get('role')},
            'intent': 'error',
        }), 200


@main_bp.route('/buddy/action', methods=['POST'])
@login_required
def buddy_action():
    payload = request.get_json(silent=True) or {}
    action_id = payload.get('action_id')
    action_payload = payload.get('payload') or {}
    confirmed = bool(payload.get('confirmed'))

    if not action_id:
        return jsonify({'status': 'error', 'message': 'No buddy action was provided.'}), 400

    try:
        context = AIBuddyService.build_context(session.get('user_id'), session.get('role'), route_hint=payload.get('route_hint'))
        allowed_actions = context.get('allowed_actions', [])
        matched = next((action for action in allowed_actions if action.get('action_id') == action_id), None)

        requires_confirmation = (matched and matched.get('requires_confirmation')) or bool(payload.get('requires_confirmation'))
        if requires_confirmation and not confirmed:
            return jsonify({
                'status': 'confirmation_required',
                'message': 'This action needs confirmation before it can be executed.',
                'redirect_url': None,
                'action_id': action_id,
            }), 200

        result = AIBuddyService.execute_action(action_id, action_payload, session.get('user_id'), session.get('role'))
        return jsonify(result)
    except Exception as exc:
        current_app.logger.warning(f"Buddy action failed: {exc}")
        log_system_event(
            'buddy',
            'warning',
            'Buddy action failed',
            details={'error': str(exc), 'action_id': action_id},
            user_id=session.get('user_id'),
        )
        return jsonify({'status': 'error', 'message': str(exc), 'redirect_url': None}), 400


@main_bp.route('/buddy/reset', methods=['POST'])
@login_required
def buddy_reset():
    AIBuddyService.reset_session_history()
    return jsonify({'status': 'ok', 'message': 'Buddy session reset.'})


@main_bp.route('/analytics')
@login_required
@roles_required('teacher')
def teacher_analytics():
    user_id = session.get('user_id')
    user_profile = get_current_user_profile() or {}
    
    try:
        # Load user's CLPs for stats
        plans_res = supabase.table('course_learning_plans').select('id,status,date_posted').eq('user_id', user_id).execute()
        plans = plans_res.data or []

        # Load subjects for current active term
        from datetime import datetime
        now = datetime.now()
        year = now.year
        month = now.month
        if month >= 8:
            active_semester = 'First Semester'
            active_academic_year = f'{year}-{year+1}'
        elif month >= 2:
            active_semester = 'Second Semester'
            active_academic_year = f'{year-1}-{year}'
        else:
            active_semester = 'Summer'
            active_academic_year = f'{year-1}-{year}'

        subjects_res = supabase.table('teacher_subjects').select('id').eq('user_id', user_id).eq('semester', active_semester).eq('academic_year', active_academic_year).execute()
        subjects = subjects_res.data or []

        # Template profiles
        profiles_res = supabase.table('teacher_template_profiles').select('id,status').eq('user_id', user_id).execute()
        profiles = profiles_res.data or []

        # Notifications (recent 7)
        notif_res = supabase.table('notifications').select('id,message,is_read,timestamp').eq('user_id', user_id).order('timestamp', desc=True).limit(7).execute()
        recent_notifications = parse_supabase_timestamp(notif_res.data, 'timestamp') if notif_res.data else []

        # Compute stats
        status_counts = {'approved': 0, 'pending': 0, 'returned_for_revision': 0, 'draft': 0, 'beta_review': 0, 'beta_ready': 0, 'generating': 0}
        for p in plans:
            s = p.get('status')
            if s in status_counts:
                status_counts[s] += 1
            else:
                status_counts[s] = 1

        profile_counts = {'confirmed': 0, 'draft': 0}
        for p in profiles:
            s = p.get('status')
            if s in profile_counts:
                profile_counts[s] += 1

        total_plans = len(plans)
        total_subjects = len(subjects)
        total_profiles = len(profiles)

        # Recent plans for table
        recent_plans = sorted(plans, key=lambda x: x.get('date_posted', ''), reverse=True)[:8]

    except Exception as e:
        current_app.logger.error(f"Teacher analytics error: {e}")
        status_counts = {}
        profile_counts = {'confirmed': 0, 'draft': 0}
        total_plans = total_subjects = total_profiles = 0
        recent_notifications = []
        recent_plans = []

    return render_template(
        'teacher_analytics.html',
        status_counts=status_counts,
        profile_counts=profile_counts,
        total_plans=total_plans,
        total_subjects=total_subjects,
        total_profiles=total_profiles,
        recent_notifications=recent_notifications,
        recent_plans=recent_plans,
        active_semester=active_semester,
        active_academic_year=active_academic_year,
    )
