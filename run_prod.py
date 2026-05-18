import os
from dotenv import load_dotenv
from waitress import serve
from app import create_app

if __name__ == '__main__':
    load_dotenv()
    
    # Initialize application
    app = create_app()
    
    # Waitress runs on 0.0.0.0 by default from this specific interface
    # Setting port to 3000 to match the previous local execution behavior
    port = int(os.environ.get('PORT', 3000))
    print(f"Starting production server on http://0.0.0.0:{port} via Waitress...")
    serve(app, host='0.0.0.0', port=port, threads=4)
