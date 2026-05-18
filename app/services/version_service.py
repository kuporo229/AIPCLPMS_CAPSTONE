from datetime import datetime
import json
from app import supabase
from app.services.observability import StructuredLogger

class VersionService:
    @staticmethod
    def save_version(plan_id, content, summary, actor_id):
        """Creates a new version snapshot. Auto-increments version_number."""
        try:
            # 1. Get latest version number
            next_version = 1
            try:
                res = supabase.table('clp_versions').select('version_number')\
                    .eq('plan_id', plan_id)\
                    .order('version_number', desc=True)\
                    .limit(1).execute()
                if res.data:
                    next_version = res.data[0]['version_number'] + 1
            except Exception as e:
                StructuredLogger.warning(f"Could not determine next version number for {plan_id}, defaulting to 1: {e}")
            
            # 2. Build entry
            version_entry = {
                'plan_id': plan_id,
                'version_number': next_version,
                'content': content if isinstance(content, dict) else json.loads(content),
                'change_summary': summary,
                'actor_id': actor_id,
                'created_at': datetime.now().isoformat()
            }
            
            # 3. Attempt insert
            try:
                supabase.table('clp_versions').insert(version_entry).execute()
            except Exception as insert_e:
                # If actor_id is missing from schema, try without it
                if 'actor_id' in str(insert_e):
                    StructuredLogger.warning(f"Retrying version save without actor_id for {plan_id}")
                    del version_entry['actor_id']
                    supabase.table('clp_versions').insert(version_entry).execute()
                else:
                    raise insert_e

            StructuredLogger.info(f"Saved version {next_version} for plan {plan_id}", plan_id=plan_id)
            return next_version
        except Exception as e:
            StructuredLogger.error(f"Failed to save version for {plan_id}: {e}")
            return None

    @staticmethod
    def get_versions(plan_id):
        """Returns all versions for a plan, ordered by version_number desc."""
        try:
            res = supabase.table('clp_versions').select('*, actor:users(username)')\
                .eq('plan_id', plan_id)\
                .order('version_number', desc=True).execute()
            return res.data
        except Exception as e:
            StructuredLogger.error(f"Failed to fetch versions for {plan_id}: {e}")
            return []

    @staticmethod
    def restore_version(plan_id, version_number, actor_id):
        """Restores a previous version by copying its content as current."""
        try:
            # Fetch the specific version
            res = supabase.table('clp_versions').select('content')\
                .eq('plan_id', plan_id)\
                .eq('version_number', version_number).single().execute()
            
            if not res.data:
                return False, "Version not found"
            
            content = res.data['content']
            
            # Update the main CLP record
            supabase.table('course_learning_plans').update({
                'content': json.dumps(content)
            }).eq('id', plan_id).execute()
            
            # Save as a NEW version to maintain history chain
            VersionService.save_version(plan_id, content, f"Restored from v{version_number}", actor_id)
            
            StructuredLogger.info(f"Restored plan {plan_id} to version {version_number}", plan_id=plan_id)
            return True, "Success"
        except Exception as e:
            StructuredLogger.error(f"Restore failed for {plan_id} v{version_number}: {e}")
            return False, str(e)
