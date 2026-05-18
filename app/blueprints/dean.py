# app/blueprints/dean.py

import json
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, session, jsonify, Response, current_app
from app.compat_supabase import (
    PostgrestAPIError,
    build_public_storage_url,
    read_storage_bytes,
    write_storage_bytes,
    delete_storage_paths,
    verify_password_hash,
)
from app import supabase, PROGRAM_OUTCOMES, COURSE_OUTCOMES, INSTITUTIONAL_OUTCOMES_HEADERS, PROGRAM_OUTCOMES_HEADERS, STORAGE_BUCKET_NAME, csrf
from app import cache, limiter
from app.forms import DeanReviewForm, ChangePasswordForm, UserProfileForm
from app.decorators import login_required, roles_required
from app.utils import (
    get_current_user_profile,
    create_notification,
    parse_supabase_timestamp,
    generate_jwt_token,
    attach_onlyoffice_ai_plugin,
    log_clp_history,
    get_department_outcomes_bundle,
    invalidate_user_profile_cache,
    log_system_event,
    user_can_access_clp,
    make_workflow_feedback,
    cleanup_storage_after_commit,
    ensure_plan_document_file,
    get_onlyoffice_base_url,
    stream_storage_file,
    log_document_timing,
)
import os
import hashlib
import requests
import time
import jwt
from werkzeug.exceptions import HTTPException

dean_bp = Blueprint('dean', __name__)


def _flash_workflow_feedback(feedback):
    flash(feedback['message'], feedback['level'])

def _validate_onlyoffice_callback(data, expected_key=None):
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
        payload = jwt.decode(token, secret, algorithms=['HS256'])
        if expected_key:
            token_key = payload.get('document', {}).get('key') if isinstance(payload, dict) else None
            if token_key and token_key != expected_key:
                current_app.logger.warning("OnlyOffice callback key mismatch.")
                return False
        return True
    except Exception as e:
        current_app.logger.warning(f"OnlyOffice callback token invalid: {e}")
        return False

def get_dean_department():
    """Helper to get the current logged-in Dean's assigned department."""
    profile = get_current_user_profile()
    if profile:
        return profile.get('assigned_department')
    return None

@dean_bp.route('/analytics')
@login_required
@roles_required('dean')
def dean_analytics():
    dean_dept = get_dean_department()
    if not dean_dept:
        flash("You must be assigned to a department to view analytics.", "warning")
        return redirect(url_for('dean.dashboard'))

    try:
        cache_key = f'dean_analytics:{dean_dept}'
        cached = cache.get(cache_key)
        if cached:
            status_counts = cached['status_counts']
            teacher_stats = cached['teacher_stats']
        else:
            res = supabase.table('course_learning_plans').select('status, user_id').eq('department', dean_dept).execute()
            plans = res.data or []

            teachers_res = supabase.table('users').select('id, first_name, last_name').eq('assigned_department', dean_dept).execute()
            teacher_names = {t['id']: f"{t['first_name']} {t['last_name']}" for t in (teachers_res.data or [])}

            status_counts = {'approved': 0, 'pending': 0, 'returned_for_revision': 0, 'draft': 0}
            teacher_stats = {}

            for p in plans:
                status = p.get('status')
                if status in status_counts:
                    status_counts[status] += 1

                uid = p.get('user_id')
                if uid not in teacher_stats:
                    name = teacher_names.get(uid, f"User {str(uid)[:8]}...")
                    teacher_stats[uid] = {'name': name, 'approved': 0, 'pending': 0, 'total': 0}

                teacher_stats[uid]['total'] += 1
                if status == 'approved':
                    teacher_stats[uid]['approved'] += 1
                if status == 'pending':
                    teacher_stats[uid]['pending'] += 1

            cache.set(cache_key, {'status_counts': status_counts, 'teacher_stats': teacher_stats}, timeout=30)

    except Exception as e:
        current_app.logger.error(f"Dean Analytics Error: {e}")
        status_counts = {}
        teacher_stats = {}

    return render_template('dean_analytics.html', 
                           status_counts=status_counts, 
                           teacher_stats=teacher_stats,
                           department=dean_dept)

@dean_bp.route('/batch_approve', methods=['POST'])
@login_required
@roles_required('dean')
def batch_approve():
    dean_dept = get_dean_department()
    plan_ids = request.form.getlist('plan_ids')
    
    if not plan_ids:
        flash("No plans selected.", "warning")
        return redirect(url_for('dean.dean_courses'))
    if not dean_dept:
        flash("You must be assigned to a department to approve plans.", "warning")
        return redirect(url_for('dean.dean_courses'))

    try:
        normalized_ids = []
        invalid_ids = 0
        for raw_id in plan_ids:
            try:
                normalized_ids.append(int(raw_id))
            except (TypeError, ValueError):
                invalid_ids += 1

        plans_res = supabase.table('course_learning_plans').select('id, status, department, user_id').in_('id', normalized_ids).execute()
        plans = plans_res.data or []
        plan_map = {int(plan['id']): plan for plan in plans}

        approvable_ids = []
        skipped = invalid_ids
        for plan_id in normalized_ids:
            plan = plan_map.get(plan_id)
            if not plan:
                skipped += 1
                continue
            if plan.get('department') != dean_dept or plan.get('status') != 'pending':
                skipped += 1
                log_system_event(
                    'workflow',
                    'warning',
                    'Dean batch approval skipped a plan',
                    details={'plan_id': plan_id, 'status': plan.get('status'), 'department': plan.get('department')},
                    user_id=session.get('user_id'),
                    plan_id=plan_id,
                )
                continue
            approvable_ids.append(plan_id)

        if approvable_ids:
            supabase.table('course_learning_plans').update({
                'status': 'approved',
                'dean_comments': 'Batch approved by Dean.'
            }).in_('id', approvable_ids).execute()

            for pid in approvable_ids:
                log_clp_history(int(pid), session['user_id'], 'Approved (Batch)', 'Batch approved by Dean.')

        cache.delete(f'dean_dashboard_stats:{dean_dept or "all"}')
        cache.delete(f'dean_analytics:{dean_dept}')
        cache.delete('admin_analytics_snapshot')

        if approvable_ids and skipped:
            _flash_workflow_feedback(
                make_workflow_feedback(
                    'Batch approval',
                    detail=f"Approved {len(approvable_ids)} plan(s). Skipped {skipped} selection(s) that were invalid, outside your department, or no longer pending.",
                    partial=True,
                )
            )
        elif approvable_ids:
            _flash_workflow_feedback(make_workflow_feedback('Batch approval', detail=f"Successfully approved {len(approvable_ids)} plan(s)."))
        else:
            _flash_workflow_feedback(
                make_workflow_feedback(
                    'Batch approval',
                    ValueError("No selected plans were eligible for approval."),
                )
            )
    except Exception as e:
        current_app.logger.warning(f"Dean batch approval failed: {e}")
        _flash_workflow_feedback(make_workflow_feedback('Batch approval', e))

    return redirect(url_for('dean.dean_courses'))

@dean_bp.route('/dashboard')
@login_required
@roles_required('dean')
def dashboard():
    dean_dept = get_dean_department()

    try:
        cache_key = f'dean_dashboard_stats:{dean_dept or "all"}'
        cached = cache.get(cache_key)
        if cached:
            total = cached['total']
            pending = cached['pending']
            approved = cached['approved']
            returned = cached['returned']
        else:
            query = supabase.table('course_learning_plans').select('status')
            if dean_dept:
                query = query.eq('department', dean_dept)

            plans = query.execute().data or []
            total = len(plans)
            pending = sum(1 for row in plans if row.get('status') == 'pending')
            approved = sum(1 for row in plans if row.get('status') == 'approved')
            returned = sum(1 for row in plans if row.get('status') == 'returned_for_revision')
            cache.set(cache_key, {
                'total': total,
                'pending': pending,
                'approved': approved,
                'returned': returned
            }, timeout=30)
    except Exception as e:
        current_app.logger.error(f"Error fetching dean stats: {e}")
        total = pending = approved = returned = 0

    # ── Pending plans queue (with teacher names) ──
    pending_plans = []
    try:
        q = supabase.table('course_learning_plans') \
            .select('id,subject,status,user_id,department,date_posted') \
            .eq('status', 'pending')
        if dean_dept:
            q = q.eq('department', dean_dept)
        q = q.order('date_posted', desc=True).limit(10)
        pending_plans = q.execute().data or []

        # Fetch teacher names
        user_ids = list({p['user_id'] for p in pending_plans if p.get('user_id')})
        if user_ids:
            users_res = supabase.table('users') \
                .select('id,username,fname,lname,mi') \
                .in_('id', user_ids).execute()
            user_map = {
                u['id']: (u.get('fname') or '') + ' ' + (u.get('mi') or '') + ' ' + (u.get('lname') or '') or u.get('username', 'Unknown')
                for u in (users_res.data or [])
            }
            for p in pending_plans:
                p['teacher_name'] = user_map.get(p.get('user_id'), 'Unknown')
    except Exception as e:
        current_app.logger.error(f"Error fetching pending plans: {e}")

    # ── Faculty at-a-glance stats ──
    faculty_stats = []
    try:
        teachers_res = supabase.table('users') \
            .select('id,username,fname,lname') \
            .eq('role', 'teacher') \
            .eq('approved', True)
        if dean_dept:
            teachers_res = teachers_res.eq('assigned_department', dean_dept)
        teachers = teachers_res.execute().data or []

        all_plans_res = supabase.table('course_learning_plans').select('user_id,status')
        if dean_dept:
            all_plans_res = all_plans_res.eq('department', dean_dept)
        all_plans = all_plans_res.execute().data or []

        plan_counts = {}
        for row in all_plans:
            uid = row.get('user_id')
            if uid not in plan_counts:
                plan_counts[uid] = {'total': 0, 'pending': 0, 'approved': 0, 'returned': 0}
            plan_counts[uid]['total'] += 1
            s = row.get('status')
            if s in plan_counts[uid]:
                plan_counts[uid][s] += 1

        for t in teachers:
            uid = t['id']
            name = f"{t.get('fname', '')} {t.get('lname', '')}".strip() or t.get('username', 'Unknown')
            stats = plan_counts.get(uid, {'total': 0, 'pending': 0, 'approved': 0, 'returned': 0})
            faculty_stats.append({'id': uid, 'name': name, **stats})
        faculty_stats.sort(key=lambda x: (-x['pending'], -x['total']))
    except Exception as e:
        current_app.logger.error(f"Error fetching faculty stats: {e}")

    # ── Recent activity feed ──
    recent_activity = []
    try:
        activity_query = supabase.table('system_events') \
            .select('id,category,level,message,created_at,user_id,plan_id') \
            .order('created_at', desc=True).limit(20)
        events = activity_query.execute().data or []
        # Filter by department if dean_dept (plan_id resolution is approximate)
        if dean_dept:
            filtered = []
            for ev in events:
                msg = (ev.get('message') or '').lower()
                if dean_dept.lower() in msg:
                    filtered.append(ev)
            events = filtered[:20] if len(filtered) < 5 else filtered[:10]
        recent_activity = events
    except Exception as e:
        current_app.logger.error(f"Error fetching activity: {e}")

    return render_template('dean_dashboard.html',
                           total=total,
                           pending=pending,
                           approved=approved,
                           returned=returned,
                           department=dean_dept,
                           pending_plans=pending_plans,
                           faculty_stats=faculty_stats,
                           recent_activity=recent_activity)

@dean_bp.route('/faculty')
@login_required
@roles_required('dean')
def dean_faculty():
    dean_dept = get_dean_department()
    
    query = supabase.table('users').select('*').eq('role', 'teacher')
    
    # FILTER: Show only teachers in the Dean's department
    if dean_dept:
        query = query.eq('assigned_department', dean_dept)
        
    teachers_res = query.order('username').limit(200).execute()
    
    return render_template('dean_faculty.html', 
                           teachers=teachers_res.data, 
                           department=dean_dept)

@dean_bp.route('/faculty/<uuid:user_id>')
@login_required
@roles_required('dean')
def view_faculty_profile(user_id):
    dean_dept = get_dean_department()
    
    try:
        user_res = supabase.table('users').select('*').eq('id', str(user_id)).single().execute()
        user = user_res.data
    except PostgrestAPIError:
        flash('User not found.', 'danger')
        return redirect(url_for('dean.dean_faculty'))

    if not user:
        abort(404)

    if user.get('role') != 'teacher':
        flash('You can only view faculty profiles.', 'warning')
        return redirect(url_for('dean.dean_faculty'))

    if dean_dept and user.get('assigned_department') != dean_dept:
        flash('You are not authorized to view faculty from other departments.', 'danger')
        return redirect(url_for('dean.dean_faculty'))

    return render_template('dean_view_faculty.html', user=user)

@dean_bp.route('/courses')
@login_required
@roles_required('dean')
def dean_courses():
    dean_dept = get_dean_department()
    
    # Base queries
    pending_query = supabase.table('course_learning_plans').select('id,subject,status,department,date_posted,user_id,upload_type, author:users(username)').eq('status', 'pending')
    approved_query = supabase.table('course_learning_plans').select('id,subject,status,department,date_posted,user_id,upload_type, author:users(username)').eq('status', 'approved')
    
    # FILTER: Show only plans for the Dean's department
    if dean_dept:
        pending_query = pending_query.eq('department', dean_dept)
        approved_query = approved_query.eq('department', dean_dept)
        
    pending_plans_res = pending_query.order('date_posted', desc=True).execute()
    approved_plans_res = approved_query.order('date_posted', desc=True).execute()
    
    # Parse dates
    pending_plans_data = parse_supabase_timestamp(pending_plans_res.data, 'date_posted')
    approved_plans_data = parse_supabase_timestamp(approved_plans_res.data, 'date_posted')

    return render_template('dean_courses.html',
                           pending_plans=pending_plans_data,
                           approved_plans=approved_plans_data,
                           department=dean_dept)

@dean_bp.route('/review_clp/<int:plan_id>', methods=['GET', 'POST'])
@login_required
@roles_required('dean')
def dean_review_clp(plan_id):
    dean_dept = get_dean_department()

    # 1. Fetch Plan
    try:
        plan_res = supabase.table('course_learning_plans').select('*, author:users(*)').eq('id', plan_id).single().execute()
        plan = plan_res.data
    except PostgrestAPIError:
        abort(404)

    if not plan: abort(404)

    # SECURITY CHECK: Ensure Dean only reviews plans from their department
    if not user_can_access_clp(plan, role='dean', user_id=session.get('user_id'), department=dean_dept):
        flash("You are not authorized to review plans from other departments.", "danger")
        return redirect(url_for('dean.dean_courses'))

    # 2. Fetch Audit History
    history_res = supabase.table('clp_history').select('*, actor:users(first_name, last_name)').eq('plan_id', plan_id).order('timestamp', desc=True).execute()
    history = parse_supabase_timestamp(history_res.data, 'timestamp')

    # 3. Parse Plan Dates
    parsed_plan_list = parse_supabase_timestamp([plan], 'date_posted')
    plan = parsed_plan_list[0]

    # 4. Handle Endorsement/Return Form
    form = DeanReviewForm()
    
    if request.method == 'POST':
        if form.validate_on_submit():
            comments = form.comments.data
            new_status = ''
            action_text = ''
            
            if form.submit_approve.data:
                new_status = 'approved'
                action_text = 'Approved'
                
            elif form.submit_return.data:
                new_status = 'returned_for_revision'
                action_text = 'Returned for Revision'
            else:
                _flash_workflow_feedback(make_workflow_feedback('Review plan', ValueError('No review action was selected.')))
                return redirect(url_for('dean.dean_courses'))

            if plan.get('status') != 'pending':
                _flash_workflow_feedback(
                    make_workflow_feedback(
                        'Review plan',
                        ValueError('This plan is no longer pending review.'),
                    )
                )
                return redirect(url_for('dean.dean_courses'))
            
            try:
                supabase.table('course_learning_plans').update({
                    'status': new_status,
                    'dean_comments': comments
                }).eq('id', plan_id).execute()
                cache.delete(f'dean_dashboard_stats:{dean_dept or "all"}')
                cache.delete(f'dean_analytics:{dean_dept}')
                cache.delete(f'teacher_dashboard_stats:{plan["user_id"]}')
                cache.delete('admin_analytics_snapshot')
                
                log_clp_history(plan_id, session['user_id'], action_text, comments)
                notification_message = (
                    f'Your CLP for "{plan["subject"]}" has been APPROVED by the Dean.'
                    if new_status == 'approved'
                    else f'Your CLP for "{plan["subject"]}" has been RETURNED. Comments: {comments}'
                )
                notification_ok = True
                try:
                    create_notification(plan['user_id'], notification_message, reference_type='clp', reference_id=plan_id)
                except Exception as notify_exc:
                    notification_ok = False
                    log_system_event(
                        'workflow',
                        'warning',
                        'Dean review notification failed',
                        details={'plan_id': plan_id, 'status': new_status, 'error': str(notify_exc)},
                        user_id=session.get('user_id'),
                        plan_id=plan_id,
                    )

                message = (
                    f'CLP for {plan["subject"]} has been approved.'
                    if new_status == 'approved'
                    else f'CLP for {plan["subject"]} has been returned for revision.'
                )
                _flash_workflow_feedback(
                    make_workflow_feedback(
                        'Review plan',
                        detail=message if notification_ok else f"{message} The teacher notification could not be delivered.",
                        partial=not notification_ok,
                    )
                )
            except Exception as e:
                current_app.logger.error(f"Error updating plan: {e}")
                _flash_workflow_feedback(make_workflow_feedback('Review plan', e))
            
            return redirect(url_for('dean.dean_courses'))

    # 5. Prepare Content Data
    content_data = {}
    if plan.get('content'):
        try:
            content_data = json.loads(plan['content'])
        except (json.JSONDecodeError, TypeError):
            content_data = {'descriptive_title': plan.get('subject', 'Error')}

    # 6. FETCH DYNAMIC OUTCOMES
    outcomes_bundle = get_department_outcomes_bundle(plan.get('department'))
    institutional_headers = outcomes_bundle['institutional_headers']
    program_outcomes = outcomes_bundle['program_outcomes']
    program_headers = outcomes_bundle['program_headers']
    course_outcomes = outcomes_bundle['course_outcomes']

    # 7. Fetch default rejection reasons
    from app.utils import get_system_prompt
    reasons_str = get_system_prompt(supabase, 'default_rejection_reasons', '')
    reasons = [r.strip() for r in reasons_str.split('\n') if r.strip()]

    return render_template('dean_review_clp.html',
                           plan=plan,
                           form=form,
                           content_data=content_data,
                           history=history,
                           program_outcomes=program_outcomes,
                           course_outcomes=course_outcomes,
                           institutional_headers=institutional_headers,
                           program_headers=program_headers,
                           rejection_reasons=reasons)

@dean_bp.route('/profile', methods=['GET', 'POST'])
@login_required
@roles_required('dean')
def dean_profile():
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
            'title': info_form.title.data
        }
        if info_form.signature.data:
            file = info_form.signature.data
            filename = secure_filename(file.filename)
            file_path = f"signatures/{session['user_id']}_{int(time.time())}_{filename}"
            try:
                supabase.storage.from_(STORAGE_BUCKET_NAME).upload(
                    path=file_path, file=file.read(),
                    file_options={"content-type": file.mimetype}
                )
                update_data['signature_url'] = build_public_storage_url(STORAGE_BUCKET_NAME, file_path)
            except Exception as e:
                flash(f"Error uploading signature: {e}", "danger")

        try:
            supabase.table('users').update(update_data).eq('id', session['user_id']).execute()
            invalidate_user_profile_cache(session['user_id'])
            flash('Profile updated.', 'success')
            return redirect(url_for('dean.dean_profile'))
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
                return render_template('dean_profile.html', info_form=info_form, pwd_form=pwd_form, user=user)
            supabase.auth.update_user({"password": pwd_form.new_password.data})
            flash('Password changed successfully.', 'success')
            return redirect(url_for('dean.dean_profile'))
        except Exception as e:
            flash(f"Error changing password: {e}", 'danger')

    return render_template('dean_profile.html', info_form=info_form, pwd_form=pwd_form, user=user)

@dean_bp.route('/clp/<int:plan_id>/review_document')
@login_required
@roles_required('dean')
def review_clp_document(plan_id):
    """Open the CLP in ONLYOFFICE editor for review/editing."""
    started_at = time.perf_counter()
    try:
        dean_dept = get_dean_department()
        res = supabase.table('course_learning_plans').select('id, user_id, department, filename, subject').eq('id', plan_id).single().execute()
        plan = res.data
        
        if not plan: abort(404)
        
        # Security check
        if not user_can_access_clp(plan, role='dean', user_id=session.get('user_id'), department=dean_dept):
            flash("Unauthorized access to document.", "danger")
            return redirect(url_for('dean.dean_courses'))

        if not plan.get('filename') or not plan['filename'].lower().endswith('.docx'):
            flash('This plan is not a .docx document.', 'warning')
            return redirect(url_for('dean.dean_review_clp', plan_id=plan_id))

        key_string = f"dean_review_{plan['id']}_{plan['filename']}"
        doc_key = hashlib.md5(key_string.encode()).hexdigest()
        doc_title = f"REVIEW: {plan['subject']}"

        base_url = get_onlyoffice_base_url(internal=True)
            
        doc_url = f"{base_url}/dean/serve_clp_doc/{plan_id}/{doc_key}"
        callback_url = f"{base_url}/dean/onlyoffice_callback/{plan_id}/{doc_key}"

        config = {
            "document": {
                "title": doc_title, 
                "url": doc_url, 
                "fileType": "docx", 
                "key": doc_key,
                "permissions": {"edit": True, "download": True, "review": True}
            },
            "documentType": "word",
            "editorConfig": {
                "mode": "edit", "callbackUrl": callback_url,
                "user": {"id": str(session['user_id']), "name": f"Dean {session.get('username', '')}"},
                "customization": {
                    "autosave": True, 
                    "forcesave": True,
                    "hideRightMenu": False
                }
            },
            "width": "100%",
            "height": "100%"
        }
        attach_onlyoffice_ai_plugin(config)
        
        # Generate JWT Token for ONLYOFFICE
        token = generate_jwt_token(config) or ""
        log_document_timing('dean_review_document_config', started_at, plan_id=plan_id, user_id=session.get('user_id'))

        return render_template("dean_review_document.html", 
                               config=config,
                               doc_title=doc_title, 
                               doc_url=doc_url, 
                               callback_url=callback_url, 
                               doc_key=doc_key, 
                               token=token, 
                               plan_id=plan_id)

    except HTTPException:
        raise
    except Exception as e:
        current_app.logger.error(f"Error opening Dean editor: {e}")
        flash(f"Error loading editor: {str(e)}", "danger")
        return redirect(url_for('dean.dean_review_clp', plan_id=plan_id))

@dean_bp.route('/serve_clp_doc/<int:plan_id>/<doc_key>')
def serve_clp_doc(plan_id, doc_key):
    started_at = time.perf_counter()
    try:
        res = supabase.table('course_learning_plans').select('filename, user_id, subject, department, content, upload_type').eq('id', plan_id).single().execute()
        if not res.data: abort(404)
        plan = {**res.data, 'id': plan_id}
        filename = plan.get('filename')
        expected_key = hashlib.md5(f"dean_review_{plan_id}_{filename}".encode()).hexdigest()
        if doc_key != expected_key:
            abort(403)
        recovered = False
        try:
            response = stream_storage_file(
                STORAGE_BUCKET_NAME,
                filename,
                "review.docx",
                mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                inline=True,
                cache_seconds=300,
            )
        except FileNotFoundError:
            filename = ensure_plan_document_file(plan)
            recovered = True
            response = stream_storage_file(
                STORAGE_BUCKET_NAME,
                filename,
                "review.docx",
                mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                inline=True,
                cache_seconds=300,
            )
        log_document_timing('dean_serve_clp', started_at, plan_id=plan_id, recovered=recovered, filename=filename)
        return response
    except FileNotFoundError:
        abort(404)
    except Exception:
        abort(500)

@dean_bp.route('/onlyoffice_callback/<int:plan_id>/<doc_key>', methods=['POST'])
@csrf.exempt
def onlyoffice_callback(plan_id, doc_key):
    try:
        data = request.get_json(silent=True) or {}
        if not _validate_onlyoffice_callback(data, expected_key=doc_key):
            log_system_event('onlyoffice', 'warning', 'Dean callback rejected', details={'plan_id': plan_id, 'status': data.get('status')}, plan_id=plan_id)
            return jsonify({"error": 1})
        if data.get("status") in [2, 6]: # 2 = Ready for saving, 6 = Force save
            download_url = data.get("url")
            if not download_url: return jsonify({"error": 1})
            
            # Fetch current file info
            res = supabase.table('course_learning_plans').select('filename, user_id').eq('id', plan_id).single().execute()
            if not res.data: return jsonify({"error": 1})
            
            old_filename = res.data['filename']
            user_id = res.data['user_id']
            
            # Download updated content from OnlyOffice
            resp = requests.get(download_url, timeout=30)
            resp.raise_for_status()
            
            # Create NEW filename to force cache busting and key update for Teacher
            clean_name = os.path.basename(old_filename).split('_', 1)[-1] or "document.docx"
            new_filename = f"{user_id}/{int(time.time())}_{clean_name}"
            
            # Upload NEW file
            write_storage_bytes(STORAGE_BUCKET_NAME, new_filename, resp.content)
            
            # Update Database to point to new file
            supabase.table('course_learning_plans').update({'filename': new_filename}).eq('id', plan_id).execute()
            
            if old_filename != new_filename:
                cleanup_storage_after_commit(
                    old_filename,
                    context='Dean OnlyOffice save',
                    user_id=user_id,
                    plan_id=plan_id,
                )

            return jsonify({"error": 0})
        return jsonify({"error": 0})
    except Exception as e: 
        current_app.logger.error(f"Callback Error: {e}")
        log_system_event('onlyoffice', 'error', 'Dean callback failed', details={'plan_id': plan_id, 'error': str(e)}, plan_id=plan_id)
        return jsonify({"error": 1})
