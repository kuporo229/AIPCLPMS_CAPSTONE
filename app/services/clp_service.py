from app import supabase, STORAGE_BUCKET_NAME
from app.utils import (start_clp_data_generation, start_clp_finalization, 
                       start_clp_refinement, create_notification, log_audit, check_ai_quota)
import json
import time
import os
from datetime import datetime

class CLPService:
    @staticmethod
    def get_clp_by_id(plan_id):
        """Fetches a single CLP with its author."""
        try:
            res = supabase.table('course_learning_plans').select('*, author:users(*)').eq('id', plan_id).single().execute()
            return res.data
        except Exception as e:
            print(f"Error fetching CLP {plan_id}: {e}")
            return None

    @staticmethod
    def get_clps_by_user(user_id):
        """Fetches all CLPs for a specific user."""
        try:
            res = supabase.table('course_learning_plans').select('*').eq('user_id', user_id).order('date_posted', desc=True).execute()
            return res.data
        except Exception as e:
            print(f"Error fetching CLPs for user {user_id}: {e}")
            return []

    @staticmethod
    def create_ai_clp(user_id, form_data, username):
        """Initializes a new AI-generated CLP."""
        # Check Quota
        allowed, count, limit = check_ai_quota(user_id)
        if not allowed:
            raise Exception(f"Daily AI generation limit reached ({count}/{limit}). Please try again tomorrow.")
            
        department_name = form_data.get('department')
        
        course_data = {
            "department": department_name,
            "subject": form_data.get('course_title'),
            "course_number": form_data.get('course_code'),
            "course_title": form_data.get('course_title'),
            "course_description": form_data.get('course_description'),
            "descriptive_title": form_data.get('course_title'),
            "type_of_course": form_data.get('type_of_course'),
            "units": str(form_data.get('unit')),
            "pre_requisite": form_data.get('pre_requisites'),
            "co_requisite": form_data.get('co_requisites'),
            "Contact_hours_per_week": form_data.get('contact_hours_per_week'),
            "class_schedule": form_data.get('class_schedule'),
            "room_assignment": form_data.get('room_assignment'),
            "instructor_name": username
        }

        try:
            initial_content = {
                'status': 'initializing',
                'progress': {'step': 0, 'label': 'Connecting to AI Engine...', 'percent': 1}
            }
            new_plan = supabase.table('course_learning_plans').insert({
                "user_id": user_id, 
                "subject": course_data['subject'],
                "department": course_data['department'],
                "status": "generating",
                "upload_type": "ai_generated",
                "filename": '',
                "content": json.dumps(initial_content)
            }).execute()
            
            if not new_plan.data:
                raise Exception("Failed to initialize CLP record.")
            
            plan_id = new_plan.data[0]['id']
            log_audit(user_id, 'CLP Creation Started', {'plan_id': plan_id, 'subject': course_data['subject']})
            # Start Background Task
            start_clp_data_generation(plan_id, user_id, course_data)
            return plan_id
        except Exception as e:
            raise e

    @staticmethod
    def update_clp_content(plan_id, user_id, original_data, updated_data):
        """Updates CLP content and triggers AI refinement."""
        try:
            # Initialize progress for refinement
            updated_data['progress'] = {'step': 1, 'label': 'Analyzing User Edits...', 'percent': 5}
            supabase.table('course_learning_plans').update({
                'status': 'generating',
                'content': json.dumps(updated_data)
            }).eq('id', plan_id).execute()
            
            start_clp_refinement(plan_id, user_id, original_data, updated_data)
        except Exception as e:
            raise e

    @staticmethod
    def finalize_clp(plan_id, user_id):
        """Triggers the final document generation."""
        try:
            supabase.table('course_learning_plans').update({'status': 'generating_doc'}).eq('id', plan_id).execute()
            start_clp_finalization(plan_id, user_id)
        except Exception as e:
            raise e

    @staticmethod
    def is_legacy_clp_content(plan):
        """Returns True when the plan is not an AI Copilot beta plan."""
        if not isinstance(plan, dict):
            return True
        if plan.get('upload_type') != 'ai_copilot_beta':
            return True
        raw_content = plan.get('content')
        if isinstance(raw_content, str):
            try:
                parsed = json.loads(raw_content)
            except Exception:
                parsed = None
        else:
            parsed = raw_content
        if not isinstance(parsed, dict):
            return True
        return parsed.get('workflow_type') != 'copilot_beta'

    @staticmethod
    def clone_clp(original_plan_id, new_user_id):
        """Clone a CLP into a fresh AI Copilot beta draft.

        Always produces an ``ai_copilot_beta`` plan in ``beta_review`` status so the
        cloned plan opens in the new copilot editor, not the legacy one.

        - If the source is already a beta plan, the full beta content is copied
          (with finalization/insert flags reset).
        - If the source is a legacy plan, only metadata (course code, title,
          description, classrooms, etc.) is preserved and the rest is rebuilt
          into a blank beta shape.

        Returns ``(new_plan_id, is_legacy)``.
        """
        from app.services.copilot_beta_service import (
            build_initial_beta_content,
            ensure_beta_shape,
            normalize_beta_content,
        )

        try:
            res = supabase.table('course_learning_plans').select('*').eq('id', original_plan_id).single().execute()
            orig = res.data
            if not orig:
                raise Exception("Original plan not found.")

            is_legacy = CLPService.is_legacy_clp_content(orig)

            # Load the cloning user's profile so signatory fields populate correctly.
            user_profile = {}
            try:
                user_res = supabase.table('users').select('*').eq('id', new_user_id).single().execute()
                user_profile = user_res.data or {}
            except Exception:
                user_profile = {}

            raw_content = orig.get('content')
            if isinstance(raw_content, str):
                try:
                    source_content = json.loads(raw_content) or {}
                except Exception:
                    source_content = {}
            elif isinstance(raw_content, dict):
                source_content = raw_content
            else:
                source_content = {}

            if is_legacy:
                # Extract best-effort metadata from legacy shape (top-level or nested).
                legacy_meta = {}
                if isinstance(source_content.get('metadata'), dict):
                    legacy_meta = source_content['metadata']

                def pick(*keys, default=''):
                    for key in keys:
                        val = legacy_meta.get(key)
                        if val not in (None, ''):
                            return val
                        val = source_content.get(key)
                        if val not in (None, ''):
                            return val
                    return default

                course_data = {
                    'department': pick('department', default=orig.get('department') or ''),
                    'course_code': pick('course_code', 'course_number'),
                    'course_title': pick('course_title', 'descriptive_title', 'subject', default=orig.get('subject') or ''),
                    'course_description': pick('course_description'),
                    'type_of_course': pick('type_of_course'),
                    'unit': pick('units', 'unit', 'units_display'),
                    'credit_display': pick('credit_display', 'units', 'unit'),
                    'contact_hours_per_week': pick('contact_hours_per_week', 'Contact_hours_per_week', 'contact_hours_display'),
                    'pre_requisites': pick('pre_requisite', 'pre_requisites'),
                    'co_requisites': pick('co_requisite', 'co_requisites'),
                    'class_schedule': pick('class_schedule'),
                    'room_assignment': pick('room_assignment'),
                    'service_learning_component': pick('service_learning_component'),
                    'target_sdgs_display': pick('target_sdgs_display', 'target_sdg'),
                    'source_context': '',
                }
                new_content = build_initial_beta_content(course_data, user_profile=user_profile)
            else:
                # Deep-copy via JSON round-trip and reset finalization state.
                new_content = json.loads(json.dumps(source_content))
                new_content['beta_ready_for_template'] = False
                new_content['beta_document_inserted'] = False
                new_content['beta_document_filename'] = ''
                new_content['review_stage'] = 'metadata'
                new_content['teacher_locked_sections'] = []
                if isinstance(new_content.get('validation'), dict):
                    new_content['validation'] = {'errors': [], 'warnings': []}
                else:
                    new_content['validation'] = {'errors': [], 'warnings': []}
                new_content = ensure_beta_shape(normalize_beta_content(new_content))

            department_value = (
                (new_content.get('metadata') or {}).get('department')
                or orig.get('department')
                or ''
            )
            course_title = (
                (new_content.get('metadata') or {}).get('course_title')
                or orig.get('subject')
                or ''
            )

            new_data = {
                'user_id': new_user_id,
                'subject': f"COPY: {course_title}" if course_title else f"COPY: {orig.get('subject', '')}",
                'department': department_value,
                'content': json.dumps(new_content),
                'upload_type': 'ai_copilot_beta',
                'status': 'beta_review',
                'filename': '',
            }

            insert_res = supabase.table('course_learning_plans').insert(new_data).execute()
            if not insert_res.data:
                raise Exception("Failed to create cloned record.")

            new_plan_id = insert_res.data[0]['id']
            log_audit(
                new_user_id,
                'CLP Cloned',
                {
                    'original_id': original_plan_id,
                    'new_id': new_plan_id,
                    'source_legacy': is_legacy,
                },
            )
            return new_plan_id, is_legacy
        except Exception as e:
            raise e
