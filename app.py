# --- IMPORTS ---
import os
from dotenv import load_dotenv

# --- MAIN EXECUTION ---
if __name__ == '__main__':
    # Load environment variables from a .env file
    load_dotenv()
    
    # Check for missing environment variables at startup.
    required_env_vars = ['FLASK_SECRET_KEY', 'GEMINI_API_KEY', 'DATABASE_URL']
    for var in required_env_vars:
        if not os.environ.get(var):
            raise ValueError(f"FATAL ERROR: The required environment variable '{var}' is not set.")
    
    # Create the Flask application instance
    from app import create_app
    app = create_app()
    
    # Run the application
    print("Starting Flask application...")
    # Use environment variable for debug mode, defaulting to False for security.
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(host="0.0.0.0", port=3000, debug=debug_mode, use_reloader=debug_mode)
