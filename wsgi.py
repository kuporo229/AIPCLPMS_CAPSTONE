import os

from app import create_app

# This creates the application instance that gunicorn can see
app = create_app()

if __name__ == "__main__":
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(host="0.0.0.0", port=3000, debug=debug_mode, use_reloader=debug_mode)
