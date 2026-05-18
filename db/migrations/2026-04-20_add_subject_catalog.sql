-- Add subject catalog for teachers and admins
-- Run this once in your database SQL editor before using the subject catalog feature

-- Create teacher_subjects table
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
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, department, semester, academic_year, course_code)
);

-- Create indexes for efficient filtering
CREATE INDEX IF NOT EXISTS idx_teacher_subjects_user_term ON teacher_subjects(user_id, semester, academic_year, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_teacher_subjects_department_term ON teacher_subjects(department, semester, academic_year, created_at DESC);

-- Add active semester and academic year system settings
INSERT INTO system_settings (key, value, description) VALUES
  ('active_semester', '1st Semester', 'Active semester for subject catalog filtering'),
  ('active_academic_year', '2024-2025', 'Active academic year for subject catalog filtering')
ON CONFLICT (key) DO NOTHING;
