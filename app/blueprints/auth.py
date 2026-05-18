# app/blueprints/auth.py

import os
import time
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, jsonify
from app import supabase, limiter, create_client
from app.forms import LoginForm, SignupForm
from app.decorators import login_required, roles_required
from license_manager import license_manager
from urllib.parse import urlparse, urljoin
import base64
import httpx
from app.compat_supabase import ClientOptions
from app.compat_supabase import PostgrestAPIError

auth_bp = Blueprint('auth', __name__)


def _create_auth_client():
    timeout_config = httpx.Timeout(10.0, connect=5.0)
    options = ClientOptions(
        postgrest_client_timeout=timeout_config,
        storage_client_timeout=timeout_config
    )
    return create_client(
        current_app.config['DATABASE_URL'],
        current_app.config['SUPABASE_KEY'],
        options=options
    )


def _is_network_auth_error(error):
    if isinstance(error, httpx.HTTPError):
        return True

    message = str(error).lower()
    network_markers = (
        'temporary failure in name resolution',
        'name or service not known',
        'nodename nor servname provided',
        'connection refused',
        'connection reset',
        'network is unreachable',
        'timed out',
    )
    return any(marker in message for marker in network_markers)


@auth_bp.before_app_request
def check_maintenance_mode():
    # Only check if maintenance mode is enabled
    from app.utils import get_system_prompt
    client = current_app.config.get('SUPABASE_SERVICE') or supabase
    mode = get_system_prompt(client, 'maintenance_mode', 'off')
    
    if mode == 'on':
        # Allow admins to bypass maintenance mode
        if session.get('role') == 'admin':
            return
            
        # Allow access to login and logout so admins can log in and others can log out
        # We must allow BOTH the GET (to see form) and POST (to submit) for auth.login
        if request.endpoint in ['auth.login', 'auth.logout', 'static']:
            return
            
        # Everything else gets a maintenance message
        return render_template('500.html', 
                               error_title="System Maintenance", 
                               error_message="The system is currently undergoing scheduled maintenance. Please check back later."), 503

@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
def login():
    # Use cached local license state on login to avoid blocking the page on slow networks.
    is_valid = license_manager.check_local_license()
    needs_license = not is_valid

    if 'user_id' in session:
        if session.get('role') == 'admin':
            return redirect(url_for('admin.admin_dashboard'))
        if is_valid:
            return redirect(url_for('main.dashboard'))
    
    # We no longer aggressively clear env keys here to prevent 
    # transient issues from forcing a re-input of the license.
    # The gateway modal will show if is_valid is False.
        
    form = LoginForm()

    if form.validate_on_submit():
        try:
            # Decode password from frontend b64
            raw_password = base64.b64decode(form.password.data).decode('utf-8')
            auth_client = _create_auth_client()
            
            auth_response = auth_client.auth.sign_in_with_password({
                "email": form.email.data,
                "password": raw_password
            })
            user_id = auth_response.user.id
            
            service_client = current_app.config.get('SUPABASE_SERVICE') or supabase
            profile_res = service_client.table('users').select(
                'id, role, approved, active, username, first_name, last_name'
            ).eq('id', user_id).single().execute()
            profile = profile_res.data
            
            if profile and profile.get('active') is False:
                flash('Your account is disabled. Please contact an administrator.', 'danger')
            elif profile and profile.get('approved'):
                if needs_license and profile.get('role') != 'admin':
                    flash('System is not activated. Please contact an administrator.', 'danger')
                    return redirect(url_for('auth.login'))

                session.clear()
                session.permanent = True
                session['user_id'] = profile['id']
                session['role'] = profile['role']
                session['username'] = profile['username']
                session['name'] = f"{profile.get('first_name')} {profile.get('last_name')}"
                session['profile_photo_url'] = profile.get('profile_photo_url') or ''
                session['last_activity'] = time.time()
                session['login_at'] = time.time()

                from app.utils import log_audit_async
                log_audit_async(profile['id'], 'Login', {'event_type': 'auth', 'result': 'success'})

                flash(f'Welcome back, {session["name"]}!', 'success')

                next_page = request.args.get('next')
                if not next_page or urlparse(next_page).netloc != '':
                    next_page = url_for('main.dashboard')
                return redirect(next_page)
            elif profile:
                flash('Your account is pending approval.', 'warning')
            else:
                flash('User profile not found.', 'danger')
        except Exception as e:
            if _is_network_auth_error(e):
                current_app.logger.error(f"Login unavailable due to upstream network error: {e}")
                flash('Authentication service is temporarily unavailable. Please try again in a moment.', 'warning')
                return render_template('login.html', form=form, needs_license=needs_license), 503

            if isinstance(e, PostgrestAPIError) and e.message == 'Account inactive':
                flash('Your account is disabled. Please contact an administrator.', 'danger')
                return render_template('login.html', form=form, needs_license=needs_license), 403

            current_app.logger.warning(f"Login failed: {e}")
            flash('Invalid email or password.', 'danger')

    return render_template('login.html', form=form, needs_license=needs_license)

@auth_bp.route('/signup', methods=['GET', 'POST'])
@limiter.limit("5 per hour") # Stricter limit for signup
def signup():
    from app.utils import get_system_prompt
    if get_system_prompt(supabase, 'allow_signups', 'yes') == 'no':
        flash('New registrations are currently closed. Please contact an administrator.', 'warning')
        return redirect(url_for('auth.login'))
        
    form = SignupForm()
    auto_approve = get_system_prompt(supabase, 'auto_approve_signups', 'no') == 'yes'
    if form.validate_on_submit():
        try:
            # Check if username or email already exists in our public users table
            existing_user_res = supabase.table('users').select('id').or_(f"username.eq.{form.username.data},email.eq.{form.email.data}").execute()
            if existing_user_res.data:
                flash('Username or email already exists. Please choose a different one or login.', 'danger')
                return redirect(url_for('auth.signup'))
            
            # Step 1: Create the user in Supabase Auth
            auth_user = supabase.auth.sign_up({
                'email': form.email.data,
                'password': form.password.data
            })
            
            if auth_user.user:
                # Step 2: Create the user profile in the public 'users' table
                selected_role = form.role.data if hasattr(form, 'role') else 'teacher'
                # Deans are NEVER auto-approved (only admin can approve)
                is_auto_approved = auto_approve if selected_role == 'teacher' else False
                profile_data = {
                    'id': auth_user.user.id,
                    'username': form.username.data,
                    'first_name': form.first_name.data,
                    'last_name': form.last_name.data,
                    'email': form.email.data,
                    'title': form.title.data,
                    'role': selected_role,
                    'approved': is_auto_approved,
                    'assigned_department': form.department.data if form.department.data else None
                }
                supabase.table('users').insert(profile_data).execute()
                
                from app.utils import log_audit
                log_audit(auth_user.user.id, 'Registration', {'department': form.department.data, 'auto_approved': auto_approve})
                
                if auto_approve:
                    flash('Registration successful! You can now log in.', 'success')
                else:
                    flash('Registration successful! Your account is pending administrator approval.', 'info')
                return redirect(url_for('auth.login'))
            else:
                 flash('Could not create authentication user. Please try again.', 'danger')
        except Exception as e:
            # --- FIX: Use current_app instead of app ---
            current_app.logger.error(f"Signup failed: {e}")
            flash(f'An error occurred during registration: {str(e)}', 'danger')
    return render_template('signup.html', form=form)

@auth_bp.route('/logout')
@login_required
def logout():
    user_id = session.get('user_id')
    if user_id:
        from app.utils import log_audit_async
        log_audit_async(user_id, 'Logout', {'event_type': 'auth', 'result': 'success'})
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))

@auth_bp.route('/api/license/activate', methods=['POST'])
@limiter.limit("5 per minute")
@login_required
@roles_required('admin')
def activate_license_api():
    """Endpoint for frontend AJAX calls to activate a license"""
    data = request.get_json()
    if not data or 'license_key' not in data or 'license_api_key' not in data:
        return jsonify({"status": "error", "message": "License and API key required"}), 400
        
    license_key = str(data['license_key']).strip()
    api_key = str(data['license_api_key']).strip()
    if not license_key or not api_key or len(license_key) > 512 or len(api_key) > 512:
        return jsonify({"status": "error", "message": "Invalid license or API key"}), 400
    
    # Update manager in-memory for the activation attempt
    license_manager.set_api_key(api_key)
    domain = request.host_url 
    
    result = license_manager.activate_license(license_key, domain)
    if result.get('success') is True or result.get('status') == 'success' or 'license_id' in result:
        # Persist to .env
        from app.utils import update_env
        try:
            update_env({
                'LOUIS_LICENSE_KEY': license_key,
                'LOUIS_LICENSE_API': api_key
            })
        except Exception as e:
            current_app.logger.error(f"Failed to persist keys to .env: {e}")
            
        return jsonify({"status": "success", "message": "License activated and saved successfully!"})
    else:
        return jsonify({
            "status": "error", 
            "message": result.get("message", "Invalid license key or API Master Key.")
        }), 400

@auth_bp.route('/api/license/status')
@login_required
@roles_required('admin')
def get_license_status_api():
    """Endpoint to check the status of the current active license"""
    result = license_manager.get_full_status()
    # Ensure nested objects or error info is passed safely
    return jsonify({
        "status": "success" if result.get("valid") else "error",
        "data": result
    })
