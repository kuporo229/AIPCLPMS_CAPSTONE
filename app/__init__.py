import os
import time
import logging
import sys
import traceback
import logging
import sys
import httpx
from datetime import timedelta
from flask import (Flask, render_template, request, redirect, flash, jsonify, Response, current_app, url_for, session, g)
from app.compat_supabase import create_client, Client, PostgrestAPIError, ClientOptions
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_caching import Cache
from flask_wtf.csrf import CSRFProtect
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from app.extensions import db
from app.local_schema import ensure_local_schema

# Initialize supabase and limiter at the top level
supabase: Client = None
supabase_service: Client = None
limiter = Limiter(key_func=get_remote_address, default_limits=["1000 per day", "200 per hour"], storage_uri="memory://")
cache = Cache()
csrf = CSRFProtect()
# Shared executor for background tasks (Production stability)
executor = ThreadPoolExecutor(max_workers=4)

# --- STATIC DATA & CONFIGURATION ---
PROGRAM_OUTCOMES = [
    {"code": "IT01", "description": "Apply knowledge of computing, science and mathematics appropriate to the discipline"},
    {"code": "IT02", "description": "Understand best practices and standards and their applications"},
    {"code": "IT03", "description": "Analyze complex problems, and identify and define the computing requirements appropriate to its solution"},
    {"code": "IT04", "description": "Identify and analyze user needs and take them into account in the selection, creation, evaluation and administration of computer-based systems"},
    {"code": "IT05", "description": "Design, implement and evaluate computer-based systems, processes, components or programs to meet desired needs and requirements under various constraints"},
    {"code": "IT06", "description": "Integrate IT-based solutions into the user environment effectively"},
    {"code": "IT07", "description": "Apply knowledge through the use of current techniques, skills, tools and practices necessary for the IT profession"},
    {"code": "IT08", "description": "Function effectively as a member or leader of a development team recognizing the different roles within a team to accomplish a common goal"},
    {"code": "IT09", "description": "Assist in the creation of an effective IT project plan"},
    {"code": "IT10", "description": "Communicate effectively with the computing community and with society at large about complex computing activities through logical writing, presentations and clear instructions"},
    {"code": "IT11", "description": "Analyze the local and global impact of computing information technology on individuals, organizations and society"},
    {"code": "IT12", "description": "Understand professional, ethical, legal, security and social issues and responsibilities in the utilization of information technology."},
    {"code": "IT13", "description": "Recognize the need for and engage in planning self-learning and improving performance as a foundation for continuing professional development"}
]
COURSE_OUTCOMES = [
    {"code": "L012", "description": "Analyze different user populations with regard to their abilities and characteristics for using both software and hardware products, and Evaluate the design of existing user interfaces based on the cognitive models of target user"},
]
INSTITUTIONAL_OUTCOMES_HEADERS = ['T', 'R1', 'I1', 'R2', 'I2', 'C', 'H']
PROGRAM_OUTCOMES_HEADERS = ['IT01', 'IT02', 'IT03', 'IT04', 'IT05', 'IT06', 'IT07', 'IT08', 'IT09', 'IT10', 'IT11', 'IT12', 'IT13']
STORAGE_BUCKET_NAME = 'clp_files'
ALLOWED_EXTENSIONS = {'docx', 'pdf'}

def create_app():
    
    global supabase, supabase_service, limiter

    # Load environment variables before reading any config flags such as FLASK_DEBUG.
    load_dotenv()

    app = Flask(__name__)

    # Explicitly configure Flask's local logger and Werkzeug logger
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    log_level = logging.DEBUG if debug_mode else logging.INFO

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    ))

    # Configure app logger
    for h in app.logger.handlers[:]:
        app.logger.removeHandler(h)
    app.logger.addHandler(handler)
    app.logger.setLevel(log_level)

    # Configure werkzeug logger (so HTTP requests show up)
    werkzeug_logger = logging.getLogger('werkzeug')
    for h in werkzeug_logger.handlers[:]:
        werkzeug_logger.removeHandler(h)
    werkzeug_logger.addHandler(handler)
    werkzeug_logger.setLevel(log_level)

    # SECURITY: Use environment variables for all sensitive keys.
    app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY')
    app.config['GEMINI_API_KEY'] = os.environ.get('GEMINI_API_KEY')
    app.config['DATABASE_URL'] = os.environ.get('DATABASE_URL')
    app.config['LPMS_STORAGE_ROOT'] = os.environ.get('LPMS_STORAGE_ROOT', os.path.join(os.getcwd(), 'local_storage'))
    app.config['PUBLIC_APP_URL'] = os.environ.get('PUBLIC_APP_URL') or os.environ.get('ONLYOFFICE_CALLBACK_URL', '').rstrip('/')
    app.config['ONLYOFFICE_CALLBACK_URL'] = os.environ.get('ONLYOFFICE_CALLBACK_URL', '').rstrip('/')
    app.config['ONLYOFFICE_INTERNAL_APP_URL'] = os.environ.get('ONLYOFFICE_INTERNAL_APP_URL', '').rstrip('/')
    app.config['TEMPLATE_BETA_AI_ENABLED'] = os.environ.get('TEMPLATE_BETA_AI_ENABLED', 'false').lower() == 'true'
    app.config['SUPABASE_URL'] = app.config['PUBLIC_APP_URL']
    app.config['SUPABASE_KEY'] = 'local'
    app.config['ONLYOFFICE_JWT_SECRET'] = os.environ.get('ONLYOFFICE_JWT_SECRET', "")
    app.config['ONLYOFFICE_API_JS_URL'] = os.environ.get('ONLYOFFICE_API_JS_URL', "http://localhost:8080/web-apps/apps/api/documents/api.js")
    app.config['ONLYOFFICE_FORCE_AI_PLUGIN_SETTINGS'] = os.environ.get('ONLYOFFICE_FORCE_AI_PLUGIN_SETTINGS', 'false').lower() == 'true'
    app.config['ONLYOFFICE_FORCE_AI_PLUGIN_MODEL_OVERRIDES'] = os.environ.get('ONLYOFFICE_FORCE_AI_PLUGIN_MODEL_OVERRIDES', 'false').lower() == 'true'
    app.config['TIPTAP_CONVERT_APP_ID'] = os.environ.get('TIPTAP_CONVERT_APP_ID', '').strip()
    app.config['TIPTAP_CONVERT_SECRET'] = os.environ.get('TIPTAP_CONVERT_SECRET', '').strip()
    app.config['TIPTAP_CONVERT_BASE_URL'] = os.environ.get('TIPTAP_CONVERT_BASE_URL', 'https://api.tiptap.dev/v2/convert').rstrip('/')
    app.config['TIPTAP_DOCUMENT_SERVER_ID'] = os.environ.get('TIPTAP_DOCUMENT_SERVER_ID', '').strip()
    app.config['TIPTAP_DOCUMENT_SERVER_SECRET'] = os.environ.get('TIPTAP_DOCUMENT_SERVER_SECRET', '').strip()
    app.config['TIPTAP_DOCUMENT_SERVER_API_SECRET'] = os.environ.get(
        'TIPTAP_DOCUMENT_SERVER_API_SECRET',
        os.environ.get('TIPTAP_CLOUD_DOCUMENT_SERVER_MANAGEMENT_API_SECRET', ''),
    ).strip()
    app.config['TIPTAP_DOCUMENT_SERVER_BASE_URL'] = os.environ.get(
        'TIPTAP_DOCUMENT_SERVER_BASE_URL',
        f"https://{app.config['TIPTAP_DOCUMENT_SERVER_ID']}.collab.tiptap.cloud" if app.config['TIPTAP_DOCUMENT_SERVER_ID'] else '',
    ).rstrip('/')
    app.config['DEBUG'] = debug_mode
    app.config['ENV'] = 'development' if debug_mode else 'production'
    app.config['TEMPLATES_AUTO_RELOAD'] = debug_mode
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['DATABASE_URL']
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    
    # Cookie Security Settings
    app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', 'False').lower() == 'true'
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    # Absolute session lifetime (recommended policy)
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
    
    # Check for missing environment variables at startup.
    if not all([app.config['SECRET_KEY'], app.config['GEMINI_API_KEY'], app.config['DATABASE_URL']]):
        raise ValueError("FATAL ERROR: One or more required environment variables are not set. "
                         "Please set FLASK_SECRET_KEY, GEMINI_API_KEY, and DATABASE_URL.")
    ensure_local_schema(app.config['DATABASE_URL'])
    
    # Local DB-backed client initialization with Supabase-compatible interfaces
    timeout_config = httpx.Timeout(30.0, connect=10.0)
    options = ClientOptions(
        postgrest_client_timeout=timeout_config,
        storage_client_timeout=timeout_config
    )
    
    supabase = create_client(app.config['DATABASE_URL'], 'local', options=options)
    supabase_service = create_client(app.config['DATABASE_URL'], 'local-service', options=options)
    
    # Store in config for easy access elsewhere
    app.config['SUPABASE_CLIENT'] = supabase
    app.config['SUPABASE_SERVICE'] = supabase_service
    
    app.logger.info("Local database clients initialized and stored in app config.")
    
    # Initialize Cache (Simple for now, can use Redis in prod)
    app.config['CACHE_TYPE'] = 'SimpleCache'
    app.config['CACHE_DEFAULT_TIMEOUT'] = 300
    app.config['WTF_CSRF_TIME_LIMIT'] = None
    cache.init_app(app)
    csrf.init_app(app)
    db.init_app(app)

    # Rate Limiter Configuration - Production Baseline
    limiter.init_app(app)
    def skip_rate_limits():
        # Skip GET requests and background polling endpoints to prevent noisy 429s
        if request.method == 'GET':
            return True
        if request.endpoint in [
            'teacher.clp_status',
            'teacher.check_generation_status',
            'main.check_notifications',
            'static'
        ]:
            return True
        return False
    limiter.request_filter(skip_rate_limits)


    
    # Pass static data to the Jinja templates
    app.config['PROGRAM_OUTCOMES'] = PROGRAM_OUTCOMES
    app.config['COURSE_OUTCOMES'] = COURSE_OUTCOMES
    app.config['INSTITUTIONAL_OUTCOMES_HEADERS'] = INSTITUTIONAL_OUTCOMES_HEADERS
    app.config['PROGRAM_OUTCOMES_HEADERS'] = PROGRAM_OUTCOMES_HEADERS
    app.config['STORAGE_BUCKET_NAME'] = STORAGE_BUCKET_NAME
    app.config['ALLOWED_EXTENSIONS'] = ALLOWED_EXTENSIONS

    # --- Register Blueprints ---
    from .blueprints.auth import auth_bp
    from .blueprints.main import main_bp
    from .blueprints.admin import admin_bp
    from .blueprints.dean import dean_bp
    from .blueprints.teacher import teacher_bp
    from .blueprints.clp_editor import clp_editor_bp
    
    app.register_blueprint(auth_bp, url_prefix='/')
    app.register_blueprint(main_bp, url_prefix='/')
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(dean_bp, url_prefix='/dean')
    app.register_blueprint(teacher_bp, url_prefix='/teacher')
    app.register_blueprint(clp_editor_bp)
    
    # Initialize License Management
    from license_manager import license_manager

    @app.context_processor
    def inject_onlyoffice_url():
        return dict(onlyoffice_api_js_url=app.config.get('ONLYOFFICE_API_JS_URL'))

    @app.context_processor
    def inject_global_settings():
        from app.utils import get_system_settings_map
        
        cached = cache.get('global_settings')
        if cached:
            return cached

        settings = get_system_settings_map(current_app.config.get('SUPABASE_SERVICE'))
        inst_name = settings.get('institution_name') or 'AI Learning Plan System'
        logo_url = settings.get('institution_logo_url') or ''
        announcement = settings.get('announcement_text') or ''
        semester = settings.get('current_semester') or 'Not Set'
        
        # Debug log to verify if logo is fetched
        if not logo_url:
            current_app.logger.debug("Logo URL fetched as empty string in context processor.")
        
        result = dict(
            institution_name=inst_name, 
            institution_logo=logo_url,
            announcement=announcement,
            current_semester=semester
        )
        
        try:
            cache.set('global_settings', result, timeout=300)
        except Exception: pass
        
        return result

    # --- CONSOLIDATED SECURITY HEADERS ---
    @app.after_request
    def add_security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        relaxed_frame_paths = ()
        if request.path.startswith(relaxed_frame_paths):
            response.headers.pop('X-Frame-Options', None)
        else:
            response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        
        # PREVENT CACHING for sensitive pages (prevents 'back' button issue after logout)
        # We only apply this to non-static files to ensure performance for assets
        cacheable_paths = (
            '/teacher/serve_clp/',
            '/dean/serve_clp_doc/',
            '/admin/serve_clp/',
            '/admin/serve_template/',
            '/storage/v1/object/public/',
        )
        if not request.path.startswith('/static') and not request.path.startswith(cacheable_paths):
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, post-check=0, pre-check=0'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
            
        return response

    # --- Error Handlers ---
    @app.errorhandler(403)
    def forbidden(e):
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'error': 'Forbidden'}), 403

        if 'user_id' in session and request.method == 'GET':
            flash('Access denied for that page.', 'warning')
            return redirect(url_for('main.dashboard'))

        return render_template('403.html'), 403

    @app.errorhandler(404)
    def not_found_error(e):
        return render_template('404.html'), 404

    @app.errorhandler(httpx.HTTPError)
    def handle_httpx_error(e):
        app.logger.error(f"Upstream HTTP error: {e}")
        app.logger.error(f"Traceback:\n{traceback.format_exc()}")

        user_message = "The database service is temporarily unavailable. Please try again in a moment."
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'error': user_message}), 503

        if 'user_id' in session and request.method == 'GET':
            flash(user_message, 'warning')
            referrer = request.referrer
            if referrer:
                return redirect(referrer)
            return redirect(url_for('main.dashboard'))

        return render_template(
            '500.html',
            error_title="Service Temporarily Unavailable",
            error_message=user_message
        ), 503

    @app.errorhandler(500)
    def internal_server_error(e):
        app.logger.error(f"Internal Server Error: {e}")
        app.logger.error(f"Traceback:\n{traceback.format_exc()}")
        return render_template('500.html'), 500

    @app.errorhandler(400)
    def bad_request(e):
        # Specific handling for CSRF token missing errors from flask-wtf
        if 'CSRF' in str(e).upper():
            if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'ok': False, 'error': 'CSRF token expired. Please refresh the page and try again.'}), 400
            flash("Your security token has expired. For your safety, please log in again.", "warning")
            return redirect(url_for('auth.login'))
        return render_template('400.html'), 400

    @app.errorhandler(429)
    def ratelimit_handler(e):
        flash("You have exceeded the rate limit. Please try again later.", "warning")
        return render_template('500.html', error_title="Too Many Requests", error_message="You have exceeded the rate limit. Please try again later."), 429

    # Register Jinja filters
    from app.utils import parse_iso, format_datetime
    app.jinja_env.filters['parse_iso'] = parse_iso
    app.jinja_env.filters['format_datetime'] = format_datetime

    # --- Correlation ID & Audit Logging Hook ---
    @app.before_request
    def setup_request():
        from app.services.observability import StructuredLogger
        from flask import g
        import uuid
        import time
        # Generate correlation ID for the request
        g.correlation_id = str(uuid.uuid4())
        g.start_time = time.time()
        
        # Skip logging for static files, health checks, and background polling
        ignored_endpoints = [
            'static', 
            'auth.login',
            'auth.logout',
            'auth.signup',
            'teacher.clp_status', 
            'teacher.check_generation_status', 
            'main.check_notifications'
        ]
        if not request.endpoint or request.endpoint in ignored_endpoints:
            return

        user_id = session.get('user_id')
        if user_id:
            from app.utils import log_audit
            # We log the 'Page View' or 'Action' based on the endpoint
            action = f"Access: {request.endpoint}"
            
            # For POST requests, we might want to log that it was a submission
            if request.method == 'POST':
                action = f"Submit: {request.endpoint}"
            
            details = {
                'path': request.path,
                'args': dict(request.args),
                'correlation_id': g.correlation_id
            }
            
            if request.method == 'POST':
                details['form_keys'] = list(request.form.keys())

            log_audit(user_id, action, details)

    @app.before_request
    def enforce_session_timeouts():
        # Skip checks for non-authenticated users and auth/static routes
        if 'user_id' not in session:
            return

        if request.endpoint in ['auth.login', 'auth.logout', 'static']:
            return

        now_ts = time.time()
        role = session.get('role')
        idle_limit_minutes = 30 if role == 'admin' else 60
        idle_limit_seconds = idle_limit_minutes * 60
        absolute_limit_seconds = app.config['PERMANENT_SESSION_LIFETIME'].total_seconds()

        last_activity = session.get('last_activity')
        login_at = session.get('login_at')
        is_ajax = request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest'

        if login_at and (now_ts - login_at) > absolute_limit_seconds:
            session.clear()
            if is_ajax:
                return jsonify({'ok': False, 'error': 'Your session expired. Please log in again.'}), 401
            flash('Your session expired. Please log in again.', 'warning')
            return redirect(url_for('auth.login'))

        if last_activity and (now_ts - last_activity) > idle_limit_seconds:
            session.clear()
            if is_ajax:
                return jsonify({'ok': False, 'error': 'You were logged out due to inactivity. Please log in again.'}), 401
            flash('You were logged out due to inactivity. Please log in again.', 'warning')
            return redirect(url_for('auth.login'))

        # Update activity timestamp for sliding idle timeout
        session['last_activity'] = now_ts

    # --- Global License Enforcement ---
    @app.before_request
    def check_license_globally():
        # Define endpoints that are exempt from license checks
        # These are needed to allow activation and basic static assets
        exempt_endpoints = [
            'static', 
            'auth.login', 
            'auth.logout', 
            'auth.activate_license_api',
            'auth.get_license_status_api',
            'main.license_setup',
            'main.index'
        ]
        
        # Also exempt error handlers or internal redirects
        if not request.endpoint or any(request.endpoint.startswith(e) for e in exempt_endpoints):
            return

        if session.get('role') == 'admin':
            return

        from license_manager import license_manager
        
        # Check license using cached local state first; remote verification is throttled
        # in the license manager to avoid blocking navigation on transient network latency.
        if not license_manager.check_local_license():
            # If we are in an AJAX/JSON request, return a 403 JSON
            if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"status": "error", "message": "System license is invalid or revoked."}), 403
            
            # For normal browser requests, redirect to login where the gateway modal will block access
            return redirect(url_for('auth.login'))

    return app
