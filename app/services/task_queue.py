import json
import traceback
from datetime import datetime
from app import supabase, executor
from app.services.observability import StructuredLogger

class TaskQueue:
    """Database-backed task queue for reliable background processing."""

    @staticmethod
    def enqueue(task_name, payload, user_id=None, plan_id=None):
        """Adds a task to the background_tasks table and triggers execution."""
        from flask import current_app
        try:
            # Use service client to bypass RLS for task management
            client = current_app.config.get('SUPABASE_SERVICE') or supabase
            
            task_entry = {
                'task_name': task_name,
                'payload': payload,
                'user_id': user_id,
                'plan_id': plan_id,
                'status': 'queued',
                'created_at': datetime.now().isoformat()
            }
            res = client.table('background_tasks').insert(task_entry).execute()
            if not res.data:
                raise Exception("Failed to insert task into database.")
            
            task_id = res.data[0]['id']
            StructuredLogger.info(
                f"Enqueued task {task_name} (ID: {task_id})",
                task_id=task_id,
                plan_id=plan_id,
                user_id=user_id,
                task_name=task_name,
                payload_keys=sorted(list(payload.keys())) if isinstance(payload, dict) else [],
            )
            
            # Start execution in thread pool
            app_context = current_app.app_context()
            future = executor.submit(TaskQueue._execute_wrapper, app_context, task_id)
            StructuredLogger.info(
                "Submitted task to executor",
                task_id=task_id,
                plan_id=plan_id,
                task_name=task_name,
                future_state="submitted",
            )
            
            return task_id
        except Exception as e:
            StructuredLogger.error(f"Failed to enqueue task {task_name}: {e}")
            return None

    @staticmethod
    def _execute_wrapper(app_context, task_id):
        """Internal wrapper to run task with app context and update status."""
        with app_context:
            from flask import current_app
            client = current_app.config.get('SUPABASE_SERVICE') or supabase
            try:
                # 1. Fetch task details
                res = client.table('background_tasks').select('*').eq('id', task_id).single().execute()
                if not res.data:
                    StructuredLogger.warning("Background task row missing during execution", task_id=task_id)
                    return
                task = res.data
                StructuredLogger.info(
                    "Starting background task execution",
                    task_id=task_id,
                    plan_id=task.get('plan_id'),
                    user_id=task.get('user_id'),
                    task_name=task.get('task_name'),
                    status=task.get('status'),
                )
                
                # Check for premature abort
                if task.get('status') == 'failed' and 'Aborted' in (task.get('error_message') or ''):
                    StructuredLogger.info(f"Task {task_id} aborted before start.")
                    return

                # 2. Update to 'processing'
                client.table('background_tasks').update({
                    'status': 'processing',
                    'started_at': datetime.now().isoformat()
                }).eq('id', task_id).execute()
                StructuredLogger.info("Marked task as processing", task_id=task_id, task_name=task.get('task_name'))
                
                # 3. Resolve the actual function
                task_name = task['task_name']
                payload = task['payload']
                user_id = task['user_id']
                plan_id = task['plan_id']
                StructuredLogger.info(
                    "Dispatching task handler",
                    task_id=task_id,
                    task_name=task_name,
                    plan_id=plan_id,
                    user_id=user_id,
                )
                
                # Dynamic import/dispatch
                if task_name == 'generate_clp':
                    from app.services.ai_tasks import generate_clp_data_task
                    generate_clp_data_task(app_context, plan_id, user_id, payload)
                elif task_name == 'refine_clp':
                    from app.services.ai_tasks import refine_clp_data_task
                    refine_clp_data_task(app_context, plan_id, user_id, payload['original'], payload['updated'])
                elif task_name == 'finalize_clp':
                    from app.services.ai_tasks import finalize_clp_task
                    finalize_clp_task(app_context, plan_id, user_id)
                elif task_name == 'apply_fixes':
                    from app.services.ai_tasks import apply_validation_fixes_task
                    apply_validation_fixes_task(app_context, plan_id, user_id, payload['content'], payload['validation'])
                elif task_name == 'beta_action':
                    from app.services.copilot_beta_tasks import run_beta_action_task
                    run_beta_action_task(app_context, task_id, plan_id, user_id, payload)
                elif task_name == 'template_beta_ai_assist':
                    from app.services.template_ai_service import run_template_beta_ai_assist_task
                    run_template_beta_ai_assist_task(app_context, task_id, payload)
                else:
                    raise ValueError(f"Unknown task type: {task_name}")

                # 4. Respect task handlers that explicitly mark themselves failed/completed.
                final_res = client.table('background_tasks').select('status, error_message').eq('id', task_id).single().execute()
                if final_res.data and final_res.data.get('status') == 'failed':
                    if 'Aborted' in (final_res.data.get('error_message') or ''):
                        StructuredLogger.info(f"Task {task_id} finished but was aborted by user. Skipping completion update.")
                    else:
                        StructuredLogger.info(
                            "Task handler marked task as failed; preserving failure state.",
                            task_id=task_id,
                            task_name=task_name,
                            error_message=final_res.data.get('error_message'),
                        )
                    return
                if final_res.data and final_res.data.get('status') == 'completed':
                    StructuredLogger.info("Task handler already marked task as completed.", task_id=task_id, task_name=task_name)
                    return

                # Mark completed
                client.table('background_tasks').update({
                    'status': 'completed',
                    'completed_at': datetime.now().isoformat()
                }).eq('id', task_id).execute()
                
                StructuredLogger.info(
                    f"Task {task_id} completed successfully.",
                    task_id=task_id,
                    task_name=task_name,
                    plan_id=plan_id,
                    user_id=user_id,
                )

            except Exception as e:
                tb = traceback.format_exc()
                StructuredLogger.error(f"Task {task_id} failed: {e}", task_id=task_id, traceback=tb)
                
                # Mark failed in DB
                try:
                    # Don't overwrite Aborted status if it was set during execution
                    check = client.table('background_tasks').select('status').eq('id', task_id).single().execute()
                    if check.data and check.data.get('status') == 'failed':
                        return

                    client.table('background_tasks').update({
                        'status': 'failed',
                        'error_message': str(e),
                        'completed_at': datetime.now().isoformat()
                    }).eq('id', task_id).execute()
                except: pass

    @staticmethod
    def get_status(task_id):
        """Returns the current status of a task."""
        from flask import current_app
        try:
            client = current_app.config.get('SUPABASE_SERVICE') or supabase
            res = client.table('background_tasks').select('status, error_message, progress_percent, progress_label').eq('id', task_id).single().execute()
            return res.data
        except: return None
