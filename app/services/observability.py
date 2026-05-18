import uuid
import time
import logging
import json
from flask import g, has_request_context, current_app, session

class StructuredLogger:
    """JSON-structured logger with correlation ID support."""

    @staticmethod
    def get_correlation_id():
        """Returns the current request's correlation ID, or generates one if missing."""
        if has_request_context():
            if not hasattr(g, 'correlation_id'):
                g.correlation_id = str(uuid.uuid4())
            return g.correlation_id
        return "background-task-" + str(uuid.uuid4())[:8]

    @staticmethod
    def log(level, message, **context):
        """Emits a structured JSON log line."""
        log_entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "level": level.upper(),
            "message": message,
            "correlation_id": StructuredLogger.get_correlation_id(),
        }
        
        # Add context-specific data
        if has_request_context():
            log_entry["user_id"] = session.get("user_id")
            log_entry["path"] = getattr(g, 'path', None)
        
        # Merge additional context
        log_entry.update(context)
        
        # Format as JSON string
        log_string = json.dumps(log_entry)
        
        # Output to appropriate logger
        if current_app:
            if level.lower() == 'error':
                current_app.logger.error(log_string)
            elif level.lower() == 'warning':
                current_app.logger.warning(log_string)
            else:
                current_app.logger.info(log_string)
        else:
            print(log_string)

    @staticmethod
    def info(message, **ctx): StructuredLogger.log('info', message, **ctx)
    @staticmethod
    def error(message, **ctx): StructuredLogger.log('error', message, **ctx)
    @staticmethod
    def warning(message, **ctx): StructuredLogger.log('warning', message, **ctx)

def timed(label):
    """Decorator that logs the duration of the wrapped function."""
    def decorator(func):
        import functools
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            try:
                result = func(*args, **kwargs)
                duration_ms = int((time.time() - start_time) * 1000)
                StructuredLogger.info(f"Finished {label}", duration_ms=duration_ms, function=func.__name__)
                return result
            except Exception as e:
                duration_ms = int((time.time() - start_time) * 1000)
                StructuredLogger.error(f"Failed {label}", duration_ms=duration_ms, error=str(e), function=func.__name__)
                raise
        return wrapper
    return decorator

def log_ai_usage(local_supabase, plan_id, user_id, task_type, model_used, duration_ms, status='success', error_message=None):
    """Logs an AI API call to the ai_usage_log table."""
    try:
        log_entry = {
            'correlation_id': StructuredLogger.get_correlation_id(),
            'plan_id': plan_id,
            'user_id': user_id,
            'task_type': task_type,
            'model_used': model_used,
            'duration_ms': duration_ms,
            'status': status,
            'error_message': error_message
        }
        local_supabase.table('ai_usage_log').insert(log_entry).execute()
    except Exception as e:
        StructuredLogger.warning(f"Failed to log AI usage to database: {e}")
