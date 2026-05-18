# app/decorators.py

from functools import wraps
from flask import session, redirect, url_for, flash, abort, request, jsonify


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check for user_id in session, which we set upon successful login.
        if 'user_id' not in session:
            if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'ok': False, 'error': 'Session expired. Please log in again.'}), 401
            flash('You must be logged in to view this page.', 'warning')
            return redirect(url_for('auth.login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def roles_required(*roles):
    def wrapper(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if session.get('role') not in roles:
                abort(403)  # Forbidden
            return f(*args, **kwargs)
        return decorated_function
    return wrapper



def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Assuming you store role in session['role']
        if 'role' not in session or session['role'] != 'admin':
            flash("You do not have permission to access this page.", "danger")
            return redirect(url_for('main.dashboard'))
        return f(*args, **kwargs)
    return decorated_function
