import os

import psycopg2


LOCAL_SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS departments (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name text NOT NULL UNIQUE,
  dean_name text,
  dean_title text,
  program_coordinator_name text,
  program_coordinator_title text,
  vice_president_name text,
  vice_president_title text,
  created_at timestamptz DEFAULT now()
);

ALTER TABLE departments ADD COLUMN IF NOT EXISTS dean_name text;
ALTER TABLE departments ADD COLUMN IF NOT EXISTS dean_title text;
ALTER TABLE departments ADD COLUMN IF NOT EXISTS program_coordinator_name text;
ALTER TABLE departments ADD COLUMN IF NOT EXISTS program_coordinator_title text;
ALTER TABLE departments ADD COLUMN IF NOT EXISTS vice_president_name text;
ALTER TABLE departments ADD COLUMN IF NOT EXISTS vice_president_title text;

CREATE TABLE IF NOT EXISTS institutional_outcomes (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code text NOT NULL,
  description text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT timezone('utc', now())
);

CREATE TABLE IF NOT EXISTS system_settings (
  key text PRIMARY KEY,
  value text NOT NULL,
  description text,
  updated_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
  id uuid PRIMARY KEY,
  username text UNIQUE,
  first_name text,
  last_name text,
  email text UNIQUE,
  role text NOT NULL DEFAULT 'teacher',
  approved boolean NOT NULL DEFAULT false,
  active boolean NOT NULL DEFAULT true,
  assigned_department text,
  title text,
  signature_url text,
  consultation_hours jsonb DEFAULT '{}'::jsonb,
  password_hash text,
  password_reset_required boolean NOT NULL DEFAULT false,
  deactivated_at timestamptz,
  deactivation_reason text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  last_login_at timestamptz
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS active boolean NOT NULL DEFAULT true;
ALTER TABLE users ADD COLUMN IF NOT EXISTS deactivated_at timestamptz;
ALTER TABLE users ADD COLUMN IF NOT EXISTS deactivation_reason text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_photo_url text;

CREATE TABLE IF NOT EXISTS course_learning_plans (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now(),
  department text NOT NULL,
  subject text NOT NULL,
  content text,
  filename text,
  upload_type text NOT NULL DEFAULT 'manual_text',
  date_posted timestamptz NOT NULL DEFAULT now(),
  user_id uuid NOT NULL REFERENCES users(id),
  status text NOT NULL DEFAULT 'draft',
  dean_comments text,
  last_updated timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS clp_documents (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  owner_id uuid NOT NULL REFERENCES users(id),
  department text NOT NULL,
  title text NOT NULL,
  status text NOT NULL DEFAULT 'draft',
  editor_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  tiptap_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS clp_editor_documents (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  plan_id bigint NOT NULL UNIQUE REFERENCES course_learning_plans(id) ON DELETE CASCADE,
  editor_schema_version text NOT NULL DEFAULT 'semantic_clp_v2',
  editor_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  tiptap_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  html_snapshot text,
  document_mode text NOT NULL DEFAULT 'editor_native',
  migration_status text NOT NULL DEFAULT 'native',
  original_docx_path text,
  current_export_docx_path text,
  current_export_pdf_path text,
  created_by uuid REFERENCES users(id),
  updated_by uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  last_exported_at timestamptz,
  lock_version integer NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS clp_editor_versions (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  editor_document_id bigint NOT NULL REFERENCES clp_editor_documents(id) ON DELETE CASCADE,
  plan_id bigint REFERENCES course_learning_plans(id) ON DELETE CASCADE,
  version_number integer NOT NULL,
  editor_json jsonb NOT NULL,
  tiptap_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  html_snapshot text,
  change_summary text,
  actor_id uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (editor_document_id, version_number)
);

ALTER TABLE clp_editor_versions ALTER COLUMN plan_id DROP NOT NULL;

CREATE TABLE IF NOT EXISTS clp_document_versions (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  document_id bigint NOT NULL REFERENCES clp_documents(id) ON DELETE CASCADE,
  version_number integer NOT NULL,
  editor_json jsonb NOT NULL,
  tiptap_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  change_summary text,
  actor_id uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (document_id, version_number)
);

CREATE TABLE IF NOT EXISTS clp_document_artifacts (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  plan_id bigint NOT NULL REFERENCES course_learning_plans(id) ON DELETE CASCADE,
  editor_document_id bigint REFERENCES clp_editor_documents(id) ON DELETE SET NULL,
  artifact_type text NOT NULL,
  storage_path text NOT NULL,
  mime_type text NOT NULL,
  source text NOT NULL DEFAULT 'editor_export',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_by uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS clp_ai_edit_requests (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  plan_id bigint REFERENCES course_learning_plans(id) ON DELETE CASCADE,
  editor_document_id bigint REFERENCES clp_editor_documents(id) ON DELETE SET NULL,
  actor_id uuid REFERENCES users(id),
  operation text NOT NULL,
  request_context jsonb NOT NULL DEFAULT '{}'::jsonb,
  response_patches jsonb NOT NULL DEFAULT '[]'::jsonb,
  status text NOT NULL DEFAULT 'preview',
  warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  applied_at timestamptz
);

CREATE TABLE IF NOT EXISTS teacher_subjects (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id uuid REFERENCES users(id),
  department text NOT NULL,
  semester text NOT NULL,
  academic_year text NOT NULL,
  course_code text NOT NULL,
  course_title text NOT NULL,
  course_description text,
  type_of_course text,
  units text,
  contact_hours text,
  pre_requisites text,
  co_requisites text,
  class_schedule text,
  room_assignment text,
  service_learning_component text,
  target_sdg text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, department, semester, academic_year, course_code)
);

CREATE TABLE IF NOT EXISTS teacher_template_profiles (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES users(id),
  department text NOT NULL,
  name text NOT NULL,
  profile_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  source_filename text,
  source_file_hash text,
  source_storage_path text,
  status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'confirmed', 'archived')),
  confirmed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE teacher_template_profiles ADD COLUMN IF NOT EXISTS source_storage_path text;
ALTER TABLE teacher_template_profiles ADD COLUMN IF NOT EXISTS is_department_default boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS template_profile_versions (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  profile_id bigint NOT NULL REFERENCES teacher_template_profiles(id) ON DELETE CASCADE,
  version_number int NOT NULL DEFAULT 1,
  profile_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  source_filename text,
  source_file_hash text,
  created_at timestamptz NOT NULL DEFAULT now(),
  note text
);

ALTER TABLE course_learning_plans ADD COLUMN IF NOT EXISTS template_profile_id bigint REFERENCES teacher_template_profiles(id);

CREATE TABLE IF NOT EXISTS templates (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name text NOT NULL,
  filename text NOT NULL,
  department_id bigint REFERENCES departments(id),
  is_default boolean DEFAULT false,
  created_at timestamptz DEFAULT now(),
  last_updated timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS generated_template_drafts (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name text NOT NULL,
  source_filename text NOT NULL,
  generated_filename text NOT NULL,
  source_type text NOT NULL DEFAULT 'docx',
  status text NOT NULL DEFAULT 'generated',
  notes text,
  department_id bigint REFERENCES departments(id),
  placeholder_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
  warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
  generation_meta jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_by uuid REFERENCES users(id),
  promoted_template_id bigint REFERENCES templates(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS notifications (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES users(id),
  message varchar NOT NULL,
  is_read boolean DEFAULT false,
  timestamp timestamptz NOT NULL DEFAULT now(),
  reference_type varchar,
  reference_id bigint
);

CREATE TABLE IF NOT EXISTS program_outcomes (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  department_id bigint REFERENCES departments(id),
  code text NOT NULL,
  description text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT timezone('utc', now())
);

CREATE TABLE IF NOT EXISTS course_outcomes (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  department_id bigint REFERENCES departments(id),
  code text NOT NULL,
  description text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT timezone('utc', now())
);

CREATE TABLE IF NOT EXISTS copilot_reference_entries (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  department_id bigint REFERENCES departments(id),
  category text NOT NULL,
  code text,
  title text NOT NULL,
  description text,
  sort_order integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT timezone('utc', now())
);

CREATE TABLE IF NOT EXISTS clp_history (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  plan_id bigint NOT NULL REFERENCES course_learning_plans(id),
  actor_id uuid NOT NULL REFERENCES users(id),
  action text NOT NULL,
  comment text,
  timestamp timestamptz NOT NULL DEFAULT timezone('utc', now())
);

CREATE TABLE IF NOT EXISTS clp_comments (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  plan_id bigint REFERENCES course_learning_plans(id),
  user_id uuid REFERENCES users(id),
  section_id text,
  comment text NOT NULL,
  is_resolved boolean DEFAULT false,
  created_at timestamptz DEFAULT timezone('utc', now())
);

CREATE TABLE IF NOT EXISTS clp_versions (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  plan_id bigint REFERENCES course_learning_plans(id),
  content jsonb NOT NULL,
  created_at timestamptz DEFAULT timezone('utc', now()),
  version_number integer NOT NULL DEFAULT 1,
  actor_id uuid REFERENCES users(id),
  change_summary text
);

CREATE TABLE IF NOT EXISTS clp_mapping_entries (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  plan_id bigint REFERENCES course_learning_plans(id),
  source_type text CHECK (source_type IN ('CO_PO', 'PO_IO', 'WLO_CO', 'ASSESSMENT_CO', 'CO_EXTERNAL')),
  source_code text NOT NULL,
  target_code text NOT NULL,
  mapping_value text,
  weight integer DEFAULT 1,
  created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS background_tasks (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_name text NOT NULL,
  payload jsonb,
  status text DEFAULT 'queued',
  user_id uuid REFERENCES users(id),
  plan_id bigint REFERENCES course_learning_plans(id),
  error_message text,
  progress_percent integer DEFAULT 0,
  progress_label text,
  created_at timestamptz DEFAULT now(),
  started_at timestamptz,
  completed_at timestamptz
);

CREATE TABLE IF NOT EXISTS knowledge_base (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  department_id bigint REFERENCES departments(id),
  filename text NOT NULL,
  content text NOT NULL,
  embedding vector(768),
  created_at timestamptz DEFAULT timezone('utc', now())
);

CREATE TABLE IF NOT EXISTS audit_logs (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id uuid REFERENCES users(id),
  action text NOT NULL,
  details jsonb,
  ip_address text,
  timestamp timestamptz DEFAULT timezone('utc', now()),
  method text,
  endpoint text,
  event_type text,
  result text DEFAULT 'success',
  status_code integer,
  resource_id text,
  latency_ms integer
);

CREATE TABLE IF NOT EXISTS ai_usage_log (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  correlation_id text,
  plan_id bigint REFERENCES course_learning_plans(id),
  user_id uuid REFERENCES users(id),
  task_type text,
  model_used text,
  prompt_tokens integer,
  response_tokens integer,
  duration_ms integer,
  status text,
  error_message text,
  created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS system_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  category text NOT NULL,
  level text NOT NULL DEFAULT 'info',
  message text NOT NULL,
  details jsonb,
  user_id uuid REFERENCES users(id),
  plan_id bigint REFERENCES course_learning_plans(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_role_approved_active ON users(role, approved, active);
CREATE INDEX IF NOT EXISTS idx_course_learning_plans_user_date ON course_learning_plans(user_id, date_posted DESC);
CREATE INDEX IF NOT EXISTS idx_course_learning_plans_status ON course_learning_plans(status);
CREATE INDEX IF NOT EXISTS idx_course_learning_plans_department_status ON course_learning_plans(department, status);
CREATE INDEX IF NOT EXISTS idx_clp_documents_owner_updated ON clp_documents(owner_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_clp_editor_documents_plan_id ON clp_editor_documents(plan_id);
CREATE INDEX IF NOT EXISTS idx_clp_editor_versions_plan_version ON clp_editor_versions(plan_id, version_number DESC);
CREATE INDEX IF NOT EXISTS idx_clp_document_versions_document_version ON clp_document_versions(document_id, version_number DESC);
CREATE INDEX IF NOT EXISTS idx_clp_document_artifacts_plan_type ON clp_document_artifacts(plan_id, artifact_type);
CREATE INDEX IF NOT EXISTS idx_clp_ai_edit_requests_plan_created ON clp_ai_edit_requests(plan_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_background_tasks_plan_status_id ON background_tasks(plan_id, status, id DESC);
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS reference_type varchar;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS reference_id bigint;
CREATE INDEX IF NOT EXISTS idx_notifications_user_read_timestamp ON notifications(user_id, is_read, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_reference ON notifications(reference_type, reference_id) WHERE reference_type IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_logs_user_timestamp ON audit_logs(user_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_clp_versions_plan_version ON clp_versions(plan_id, version_number DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_base_department_id ON knowledge_base(department_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_base_embedding ON knowledge_base USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_teacher_subjects_user_term ON teacher_subjects(user_id, semester, academic_year, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_teacher_subjects_department_term ON teacher_subjects(department, semester, academic_year, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_teacher_template_profiles_user_dept ON teacher_template_profiles(user_id, department, status);
CREATE INDEX IF NOT EXISTS idx_clp_template_profile_id ON course_learning_plans(template_profile_id);
CREATE INDEX IF NOT EXISTS idx_system_events_created_at ON system_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_system_events_category_created_at ON system_events(category, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_generated_template_drafts_status_created_at ON generated_template_drafts(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_generated_template_drafts_department_created_at ON generated_template_drafts(department_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_copilot_reference_entries_department_category_sort ON copilot_reference_entries(department_id, category, sort_order, id);

INSERT INTO system_settings (key, value, description)
VALUES
  ('custom_editor_enabled', 'false', 'Enable custom CLP editor features.'),
  ('custom_editor_new_plans_enabled', 'false', 'Enable custom editor for newly created plans.'),
  ('custom_editor_migration_enabled', 'false', 'Enable migration into semantic CLP editor documents.'),
  ('custom_editor_dean_review_enabled', 'false', 'Enable dean review surfaces for custom editor documents.'),
  ('custom_editor_admin_enabled', 'false', 'Enable admin management surfaces for custom editor documents.'),
  ('custom_editor_department_allowlist', '', 'Comma-separated department names allowed to use custom editor features.')
ON CONFLICT (key) DO NOTHING;

CREATE OR REPLACE FUNCTION match_documents(
  query_embedding vector(768),
  match_threshold double precision,
  match_count integer,
  filter_department_id bigint
)
RETURNS TABLE (
  id bigint,
  department_id bigint,
  filename text,
  content text,
  similarity double precision
)
LANGUAGE sql
AS $$
  SELECT
    kb.id,
    kb.department_id,
    kb.filename,
    kb.content,
    1 - (kb.embedding <=> query_embedding) AS similarity
  FROM knowledge_base kb
  WHERE kb.department_id = filter_department_id
    AND 1 - (kb.embedding <=> query_embedding) >= match_threshold
  ORDER BY kb.embedding <=> query_embedding
  LIMIT match_count;
$$;
"""


_schema_initialized = False


def ensure_local_schema(database_url):
    global _schema_initialized
    if _schema_initialized:
        return

    if not database_url:
        raise ValueError("DATABASE_URL is not configured")

    conn = psycopg2.connect(database_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(LOCAL_SCHEMA_SQL)
    finally:
        conn.close()

    _schema_initialized = True


def ensure_storage_root(storage_root):
    if storage_root:
        os.makedirs(storage_root, exist_ok=True)
