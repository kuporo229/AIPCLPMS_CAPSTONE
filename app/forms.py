# app/forms.py

from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileAllowed, FileRequired
from wtforms import (StringField, PasswordField, SubmitField, SelectField,
                     TextAreaField, HiddenField, EmailField, IntegerField, BooleanField)
from wtforms.validators import DataRequired, Length, EqualTo, Regexp, Email, Optional, NumberRange, ValidationError
from app import supabase
import re
from datetime import datetime as _dt


def _academic_year_choices():
    current = _dt.now().year
    years = []
    for y in range(current - 2, current + 4):
        label = f"{y}-{y+1}"
        years.append((label, label))
    return years


def _semester_choices():
    return [
        ('1st Semester', '1st Semester'),
        ('2nd Semester', '2nd Semester'),
        ('Summer', 'Summer Term'),
    ]


def _academic_session_choices():
    choices = []
    for sem, sem_label in _semester_choices():
        for yr, _ in _academic_year_choices():
            choices.append((f"{sem} {yr}", f"{sem_label} — {yr}"))
    return choices

DEPARTMENT_CHOICES = [
    ('Department of Information Technology', 'Department of Information Technology'),
    ('Department of Engineering', 'Department of Engineering'),
    ('Department of Business', 'Department of Business'),
    ('Department of Arts and Sciences', 'Department of Arts and Sciences')
]

def get_department_choices():
    try:
        from app.utils import get_department_choices as get_cached_department_choices
        return get_cached_department_choices()
    except Exception:
        return []


def validate_strong_password(_form, field):
    password = field.data or ""
    if len(password) < 8:
        raise ValidationError("Password must be at least 8 characters long.")
    if not re.search(r"[A-Z]", password):
        raise ValidationError("Password must include uppercase, lowercase, number, and special character.")
    if not re.search(r"[a-z]", password):
        raise ValidationError("Password must include uppercase, lowercase, number, and special character.")
    if not re.search(r"\d", password):
        raise ValidationError("Password must include uppercase, lowercase, number, and special character.")
    if not re.search(r"[^A-Za-z0-9]", password):
        raise ValidationError("Password must include uppercase, lowercase, number, and special character.")


def _normalized_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _word_count(value):
    return len(re.findall(r"\b[\w/-]+\b", _normalized_text(value)))

class LoginForm(FlaskForm):
    email = EmailField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    submit = SubmitField('Sign In')

class SignupForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired(), Length(max=80)])
    last_name = StringField('Last Name', validators=[DataRequired(), Length(max=80)])
    username = StringField('Username', validators=[DataRequired(), Length(min=4, max=80), Regexp('^[A-Za-z][A-Za-z0-9_.]*$', 0, 'Usernames must have only letters, numbers, dots or underscores')])
    email = StringField('Email', validators=[DataRequired(), Email(), Length(max=120)])
    title = StringField('Title (e.g., RMT, LPT, DIT, Ph.D.)', validators=[Optional(), Length(max=50)])
    password = PasswordField('Password', validators=[
        DataRequired(),
        Length(min=8, message="Password must be at least 8 characters long."),
        validate_strong_password
    ])
    confirm_password = PasswordField('Confirm Password', validators=[DataRequired(), EqualTo('password', message='Passwords must match.')])
    role = SelectField('Apply As', choices=[('teacher', 'Teacher'), ('dean', 'Dean / Department Head')], default='teacher')
    department = SelectField('Department', choices=[], validators=[Optional()])
    submit = SubmitField('Register Account')

    def __init__(self, *args, **kwargs):
        super(SignupForm, self).__init__(*args, **kwargs)
        self.department.choices = get_department_choices()

class ChangePasswordForm(FlaskForm):
    current_password = PasswordField('Current Password', validators=[DataRequired()])
    new_password = PasswordField('New Password', validators=[
        DataRequired(),
        Length(min=8, message="New password must be at least 8 characters long."),
        validate_strong_password
    ])
    confirm_new_password = PasswordField('Confirm New Password', validators=[DataRequired(), EqualTo('new_password', message='New passwords must match.')])
    submit = SubmitField('Change Password')

class UserProfileForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired(), Length(max=80)])
    last_name = StringField('Last Name', validators=[DataRequired(), Length(max=80)])
    title = StringField('Academic Title', validators=[Optional(), Length(max=50)], render_kw={"placeholder": "e.g. LPT, Ph.D."})
    signature = FileField('Digital Signature (PNG/Transparent)', validators=[Optional(), FileAllowed(['png'], 'PNG files only!')])
    profile_photo = FileField('Profile Photo', validators=[Optional(), FileAllowed(['jpg', 'jpeg', 'png', 'webp'], 'Image files only (jpg, png, webp)!')])
    
    # Consultation Hours
    cons_mon_time = StringField('Monday Time')
    cons_mon_room = StringField('Monday Room')
    cons_tue_time = StringField('Tuesday Time')
    cons_tue_room = StringField('Tuesday Room')
    cons_wed_time = StringField('Wednesday Time')
    cons_wed_room = StringField('Wednesday Room')
    cons_thu_time = StringField('Thursday Time')
    cons_thu_room = StringField('Thursday Room')
    cons_fri_time = StringField('Friday Time')
    cons_fri_room = StringField('Friday Room')

    submit_info = SubmitField('Save Profile Changes')

class CLPUploadForm(FlaskForm):
    department = SelectField('Department', choices=[], validators=[DataRequired()])
    subject = StringField('Subject Name', validators=[DataRequired(), Length(min=3, max=100)])
    content = TextAreaField('Course Learning Plan Content (Optional)')
    file = FileField('Upload CLP Document (Optional)', validators=[FileAllowed(['docx', 'pdf'], 'Only .docx and .pdf files are allowed!')])
    submit = SubmitField('Upload Plan')

    def __init__(self, *args, **kwargs):
        super(CLPUploadForm, self).__init__(*args, **kwargs)
        self.department.choices = get_department_choices()

class CLPUpdateForm(CLPUploadForm):
    submit = SubmitField('Update Plan')


class AIClpForm(FlaskForm):
    department = SelectField('Department', validators=[DataRequired()], choices=[])
    course_title = StringField('Course / Subject Title', validators=[DataRequired(), Length(max=200)], render_kw={"placeholder": "e.g. Introduction to Computing"})
    course_code = StringField('Course Code', validators=[DataRequired()], render_kw={"placeholder": "e.g. ITE 101"})
    course_description = TextAreaField('Course Description', validators=[DataRequired()], render_kw={"rows": 3, "placeholder": "Brief description of the course..."})
    type_of_course = StringField('Type of Course', validators=[Optional()], render_kw={"placeholder": "e.g. Major / General Education"})
    unit = StringField('Unit', validators=[DataRequired()], render_kw={"placeholder": "e.g. 3.0"})
    contact_hours_per_week = StringField('Contact Hours', validators=[DataRequired()], render_kw={"placeholder": "e.g. 3 Hours/Week"})
    pre_requisites = StringField('Pre-requisites', validators=[Optional()], render_kw={"placeholder": "e.g. None"})
    co_requisites = StringField('Co-requisites', validators=[Optional()], render_kw={"placeholder": "e.g. None"})
    class_schedule = StringField('Class Schedule', validators=[Optional()], render_kw={"placeholder": "e.g. MWF 10:00-11:00 AM"})
    room_assignment = StringField('Room Assignment', validators=[Optional()], render_kw={"placeholder": "e.g. Lab 1"})
    submit = SubmitField('Generate CLP with AI')

    def __init__(self, *args, **kwargs):
        super(AIClpForm, self).__init__(*args, **kwargs)
        self.department.choices = get_department_choices()


class AICopilotBetaForm(FlaskForm):
    subject_id = SelectField('Subject', validators=[DataRequired()], choices=[])
    service_learning_component = TextAreaField('Service Learning Component', validators=[Optional()], render_kw={"rows": 3})
    source_context = TextAreaField('Optional Source Context', validators=[Optional()], render_kw={"rows": 4, "placeholder": "Paste syllabus notes, old CLP snippets, or drafting guidance..."})
    
    # Signatory Information — pre-fills document signature blocks
    prepared_by_name = StringField('Prepared By Name', validators=[Optional(), Length(max=200)])
    prepared_by_position = StringField('Prepared By Position / Title', validators=[Optional(), Length(max=200)])
    reviewed_by_name = StringField('Reviewed By / Program Coordinator Name', validators=[Optional(), Length(max=200)])
    reviewed_by_position = StringField('Reviewed By / Program Coordinator Title', validators=[Optional(), Length(max=200)])
    endorsed_by_name = StringField('Endorsed By / Dean Name', validators=[Optional(), Length(max=200)])
    endorsed_by_position = StringField('Endorsed By / Dean Title', validators=[Optional(), Length(max=200)])
    approved_by_name = StringField('Approved By / Vice President Name', validators=[Optional(), Length(max=200)])
    approved_by_position = StringField('Approved By / Vice President Title', validators=[Optional(), Length(max=200)])
    
    submit = SubmitField('Create Beta Draft')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.subject_id.choices = []

    def validate_class_schedule(self, field):
        value = _normalized_text(field.data)
        has_day = bool(re.search(r"\b(?:M|T|W|Th|F|S|Sat|Sun|Mon|Tue|Tues|Wed|Thu|Thur|Thurs|Fri)\b", value, re.I))
        compact_days = bool(re.fullmatch(r"[MTWHFSatSun/\- ]{3,}", value, re.I))
        has_time = bool(re.search(r"\d{1,2}:\d{2}", value))
        if len(value) < 3 or not (has_day or has_time or compact_days):
            raise ValidationError("Class schedule must use a proper schedule format, for example 'MWF' or 'MWF 10:00-11:00 AM'.")
        field.data = value

    def validate_room_assignment(self, field):
        value = _normalized_text(field.data)
        if _word_count(value) < 2:
            raise ValidationError("Room assignment must include building or room details.")
        field.data = value

    def validate_service_learning_component(self, field):
        value = _normalized_text(field.data)
        if value and _word_count(value) < 4:
            raise ValidationError("Service learning component should be a meaningful phrase or sentence if provided.")
        field.data = value

    def validate_source_context(self, field):
        value = _normalized_text(field.data)
        if value and _word_count(value) < 5:
            raise ValidationError("Source context should include enough detail to guide the AI if provided.")
        field.data = value


class TeacherSubjectForm(FlaskForm):
    department = SelectField('Department', validators=[DataRequired()], choices=[])
    course_code = StringField('Course Code', validators=[DataRequired(), Length(min=3, max=40)])
    course_title = StringField('Course Title', validators=[DataRequired(), Length(min=8, max=255)])
    course_description = TextAreaField('Course Description', validators=[DataRequired(), Length(min=40)], render_kw={"rows": 4})
    type_of_course = StringField('Type of Course', validators=[DataRequired(), Length(min=3, max=100)])
    units = StringField('Units / Display Value', validators=[DataRequired(), Length(max=100)], render_kw={"placeholder": "e.g. 3 Units"})
    contact_hours = StringField('Contact Hours Per Week', validators=[DataRequired(), Length(max=120)])
    pre_requisites = StringField('Pre-requisite', validators=[Optional(), Length(max=120)])
    co_requisites = StringField('Co-requisite', validators=[Optional(), Length(max=120)])
    class_schedule = StringField('Class Schedule', validators=[Optional(), Length(max=200)], render_kw={"placeholder": "e.g. MWF 7:30–9:00 AM"})
    room_assignment = StringField('Room Assignment', validators=[Optional(), Length(max=120)], render_kw={"placeholder": "e.g. Room 301, ICT Building"})
    service_learning_component = StringField('Service Learning Component', validators=[Optional(), Length(max=255)], render_kw={"placeholder": "e.g. Visual Communication Enhancement for Campus Offices"})
    target_sdg = StringField('Target SDG', validators=[Optional(), Length(max=120)], render_kw={"placeholder": "e.g. SDG 4, SDG 8, SDG 9"})
    template_profile_id = SelectField('Template Profile', validators=[Optional()], choices=[], render_kw={"class": "template-profile-selector"})
    prepared_by_name = StringField('Prepared By Name', validators=[Optional(), Length(max=200)])
    prepared_by_position = StringField('Prepared By Position / Title', validators=[Optional(), Length(max=200)])
    reviewed_by_name = StringField('Reviewed By / Program Coordinator Name', validators=[Optional(), Length(max=200)])
    reviewed_by_position = StringField('Reviewed By / Program Coordinator Title', validators=[Optional(), Length(max=200)])
    endorsed_by_name = StringField('Endorsed By / Dean Name', validators=[Optional(), Length(max=200)])
    endorsed_by_position = StringField('Endorsed By / Dean Title', validators=[Optional(), Length(max=200)])
    approved_by_name = StringField('Approved By / Vice President Name', validators=[Optional(), Length(max=200)])
    approved_by_position = StringField('Approved By / Vice President Title', validators=[Optional(), Length(max=200)])
    submit = SubmitField('Save Subject')

    def __init__(self, *args, include_blank_department=False, template_profiles=None, **kwargs):
        super().__init__(*args, **kwargs)
        department_choices = get_department_choices()
        self.department.choices = ([('', 'Select Department')] + department_choices) if include_blank_department else department_choices
        # Template profile choices
        self.template_profile_id.choices = [('', '— None (skip template linking) —')]
        if template_profiles:
            for tp in template_profiles:
                if isinstance(tp, dict):
                    label = f"{tp.get('source_filename', 'Profile')}"
                    if tp.get('department'):
                        label += f" ({tp['department']})"
                    self.template_profile_id.choices.append((str(tp['id']), label))

    def validate_course_code(self, field):
        value = _normalized_text(field.data)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ./_-]{2,39}", value):
            raise ValidationError("Course code must use only letters, numbers, spaces, slash, dash, underscore, or period.")
        if not re.search(r"[A-Za-z]", value) or not re.search(r"\d", value):
            raise ValidationError("Course code must include both letters and numbers.")
        field.data = value

    def validate_course_title(self, field):
        value = _normalized_text(field.data)
        if _word_count(value) < 3:
            raise ValidationError("Course title must clearly name the subject, using at least 3 words.")
        field.data = value

    def validate_course_description(self, field):
        value = _normalized_text(field.data)
        if _word_count(value) < 8:
            raise ValidationError("Course description must explain the course in at least 8 words.")
        field.data = value

    def validate_type_of_course(self, field):
        value = _normalized_text(field.data)
        if len(value) < 3:
            raise ValidationError("Type of course is too short.")
        field.data = value

class CLPGenerateForm(FlaskForm):
    subject_name = StringField('Subject Name', validators=[DataRequired(), Length(min=5, max=100)], render_kw={"placeholder": "e.g., Introduction to HCI"})
    department = SelectField('Department', choices=DEPARTMENT_CHOICES, validators=[DataRequired()])
    submit = SubmitField('Generate with AI')

class GenerateAIForm(FlaskForm):
    department = SelectField('Department', choices=[], validators=[DataRequired()])
    course_code = StringField('Course Code', validators=[DataRequired(), Length(max=20)])
    course_title = StringField('Course Title', validators=[DataRequired(), Length(max=255)])
    course_description = TextAreaField('Course Description', validators=[DataRequired()])
    type_of_course = StringField('Type of Course (e.g., Lecture, Laboratory)', validators=[DataRequired(), Length(max=50)])
    unit = IntegerField('Unit', validators=[DataRequired(), NumberRange(min=1, max=10)])
    pre_requisite = StringField('Pre-Requisite', validators=[Optional(), Length(max=100)])
    co_requisite = StringField('Co-Requisite', validators=[Optional(), Length(max=100)])
    credit = IntegerField('Credit', validators=[DataRequired(), NumberRange(min=0, max=10)])
    contact_hours_per_week = StringField('Contact Hours Per Week (e.g., 3 Lecture, 2 Lab)', validators=[DataRequired(), Length(max=50)])
    class_schedule = StringField('Class Schedule (e.g., MWF 8:00 AM - 9:00 AM)', validators=[DataRequired(), Length(max=100)])
    room_assignment = StringField('Room Assignment', validators=[Optional(), Length(max=50)])
    submit = SubmitField('CREATE')

    def __init__(self, *args, **kwargs):
        super(GenerateAIForm, self).__init__(*args, **kwargs)
        self.department.choices = get_department_choices()

class DeanReviewForm(FlaskForm):
    comments = TextAreaField('Comments (Optional)', render_kw={"placeholder": "Provide feedback for revision..."})
    submit_approve = SubmitField('Approve Plan')
    submit_return = SubmitField('Return for Revision')

class ApproveUserForm(FlaskForm):
    user_id = HiddenField(validators=[DataRequired()])
    role = SelectField('Assign Role', choices=[('teacher', 'Teacher'), ('dean', 'Dean')], validators=[DataRequired()])
    assigned_department = SelectField('Assign Department', choices=[], validators=[Optional()])
    submit = SubmitField('Approve & Assign Role')

    def __init__(self, *args, **kwargs):
        super(ApproveUserForm, self).__init__(*args, **kwargs)
        choices = get_department_choices()
        self.assigned_department.choices = [('', 'No Department (e.g., Dean)')] + choices

class EditUserForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired(), Length(max=80)])
    last_name = StringField('Last Name', validators=[DataRequired(), Length(max=80)])
    title = StringField('Title', validators=[Optional(), Length(max=50)])
    role = SelectField('Role', choices=[('teacher', 'Teacher'), ('dean', 'Dean'), ('admin', 'Admin')], validators=[DataRequired()])
    department = SelectField('Department', choices=[], validators=[Optional()])
    submit = SubmitField('Save Changes')

    def __init__(self, *args, **kwargs):
        super(EditUserForm, self).__init__(*args, **kwargs)
        self.department.choices = get_department_choices() + [('', 'None')]

class DepartmentForm(FlaskForm):
    name = StringField('Department Name', validators=[DataRequired(), Length(max=100)])
    dean_name = StringField('Dean Name', validators=[Optional(), Length(max=120)])
    dean_title = StringField('Dean Title', validators=[Optional(), Length(max=120)])
    program_coordinator_name = StringField('Program Coordinator Name', validators=[Optional(), Length(max=120)])
    program_coordinator_title = StringField('Program Coordinator Title', validators=[Optional(), Length(max=120)])
    vice_president_name = StringField('Vice President Name', validators=[Optional(), Length(max=120)])
    vice_president_title = StringField('Vice President Title', validators=[Optional(), Length(max=120)])
    submit = SubmitField('Add Department')

class TemplateUploadForm(FlaskForm):
    name = StringField('Template Name', validators=[DataRequired(), Length(max=100)])
    file = FileField('Template File (.docx)', validators=[DataRequired(), FileAllowed(['docx'], 'Only .docx files allowed!')])
    department = SelectField('Assign to Department', choices=[], validators=[Optional()])
    is_default = SelectField('Set as Global Default?', choices=[('no', 'No'), ('yes', 'Yes')], validators=[Optional()])
    submit = SubmitField('Upload Template')

    def __init__(self, *args, **kwargs):
        super(TemplateUploadForm, self).__init__(*args, **kwargs)
        try:
            from app.utils import get_department_choices as get_cached_department_choices
            self.department.choices = get_cached_department_choices(include_blank_label='None (General Use)', use_ids=True)
        except:
            self.department.choices = [('', 'None')]


class TemplateFromCLPBetaForm(FlaskForm):
    name = StringField('Draft Template Name', validators=[DataRequired(), Length(max=100)])
    source_file = FileField('Source CLP (.docx)', validators=[DataRequired(), FileAllowed(['docx'], 'Only .docx files allowed in beta!')])
    department = SelectField('Assign to Department', choices=[], validators=[Optional()])
    notes = TextAreaField('Admin Notes', validators=[Optional(), Length(max=500)], render_kw={"rows": 3, "placeholder": "Optional review notes or context for this beta draft..."})
    ai_assist = BooleanField('Use AI assist for unresolved inputs')
    submit = SubmitField('Generate Beta Draft')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            from app.utils import get_department_choices as get_cached_department_choices
            self.department.choices = get_cached_department_choices(include_blank_label='None (General Use)', use_ids=True)
        except Exception:
            self.department.choices = [('', 'None')]


class TemplateBetaPromoteForm(FlaskForm):
    is_default = SelectField('Set as Global Default?', choices=[('no', 'No'), ('yes', 'Yes')], validators=[Optional()])
    submit = SubmitField('Promote To Template Library')


class TemplateEditForm(FlaskForm):
    content = TextAreaField('Template Content', validators=[DataRequired()], 
                            render_kw={"rows": 25, "placeholder": "Paste the complete Course Learning Plan template text here..."})
    submit = SubmitField('Update Template')

class SystemSettingsForm(FlaskForm):
    # AI Engine Settings
    ai_provider = SelectField('AI Provider', choices=[
        ('google', 'Google Gemini'),
        ('deepseek', 'DeepSeek'),
    ], default='google')
    gemini_model = SelectField('AI Model', choices=[
        ('gemini-3-flash-preview', 'Gemini 3 Flash (Preview)'),
        ('gemini-3.1-pro-preview', 'Gemini 3.1 Pro (Preview)'),
        ('gemini-2.0-flash', 'Gemini 2.0 Flash (Best Balance)'),
        ('gemini-1.5-flash', 'Gemini 1.5 Flash (Standard)'),
        ('gemini-1.5-flash-8b', 'Gemini 1.5 Flash-8B (Lightweight)'),
        ('gemini-1.5-pro', 'Gemini 1.5 Pro (Deep Reasoning)'),
        ('deepseek-v4-flash', 'DeepSeek V4 Flash (Fast, No Reasoning)'),
        ('deepseek-v4-pro', 'DeepSeek V4 Pro (Deep Reasoning)'),
    ], default='gemini-2.0-flash')
    gemini_thinking_level = SelectField(
        'Thinking Level',
        choices=[
            ('minimal', 'Minimal'),
            ('low', 'Low'),
            ('medium', 'Medium'),
            ('high', 'High'),
        ],
        default='minimal'
    )
    ai_temperature = StringField('AI Temperature (0.0 - 1.0)', default='0.7')
    daily_ai_limit = IntegerField('Daily AI Generation Limit per User', default=5)
    
    # Prompt Settings
    prompt_po_io = TextAreaField('PO-IO Mapping Prompt', validators=[Optional()], render_kw={"rows": 4})
    prompt_co_po = TextAreaField('CO-PO Mapping Prompt', validators=[Optional()], render_kw={"rows": 4})
    prompt_weekly = TextAreaField('Weekly Breakdown Prompt', validators=[Optional()], render_kw={"rows": 6})
    prompt_copilot_beta_clo = TextAreaField('Beta Copilot CLO Prompt', validators=[Optional()], render_kw={"rows": 4})
    prompt_copilot_beta_alignment = TextAreaField('Beta Copilot Alignment Prompt', validators=[Optional()], render_kw={"rows": 4})
    prompt_copilot_beta_weekly = TextAreaField('Beta Copilot Weekly Prompt', validators=[Optional()], render_kw={"rows": 6})
    copilot_program_outcomes = TextAreaField('Program Outcomes for Copilot', validators=[Optional()], render_kw={"rows": 6})
    copilot_graduate_attributes = TextAreaField('Graduate Attributes', validators=[Optional()], render_kw={"rows": 4})
    copilot_core_values = TextAreaField('Core Values', validators=[Optional()], render_kw={"rows": 4})
    copilot_pqf_level_6_options = TextAreaField('PQF Level 6 Options', validators=[Optional()], render_kw={"rows": 4})
    copilot_aqrf_level_6_options = TextAreaField('AQRF Level 6 Options', validators=[Optional()], render_kw={"rows": 4})
    copilot_sdg_options = TextAreaField('SDG Options', validators=[Optional()], render_kw={"rows": 4})
    copilot_sdg_context = TextAreaField('SDG Guidance', validators=[Optional()], render_kw={"rows": 6})
    copilot_default_template_name = StringField('Production Default Template Name', validators=[Optional()], render_kw={"readonly": True})
    copilot_default_template_filename = StringField('Production Default Template File', validators=[Optional()], render_kw={"readonly": True})
    
    # Academic Session Management
    current_semester = SelectField('Current Academic Session', choices=[], default='1st Semester 2025-2026')
    active_semester = SelectField('Active Semester', choices=[], default='1st Semester')
    active_academic_year = SelectField('Active Academic Year', choices=[], default='2025-2026')
    submission_deadline = StringField('Global Submission Deadline (YYYY-MM-DD)', validators=[Optional()], render_kw={"type": "date"})
    
    # Branding & UI
    institution_name = StringField('Institution Name', default='Learning Management System')
    institution_logo_url = StringField('Logo URL (Public Image Link)', validators=[Optional()])
    institution_logo_file = FileField('Upload New Logo (PNG/JPG)', validators=[Optional(), FileAllowed(['png', 'jpg', 'jpeg'], 'Images only!')])
    announcement_text = TextAreaField('Dashboard Announcement (Public Notice)', validators=[Optional()], render_kw={"rows": 3})

    # Security & Maintenance
    allow_signups = SelectField('Allow New User Signups', choices=[('yes', 'Yes'), ('no', 'No')], default='yes')
    auto_approve_signups = SelectField('Auto-Approve New Users', choices=[('no', 'Manual — Admin must approve each user'), ('yes', 'Auto — Users are approved immediately on signup')], default='no')
    maintenance_mode = SelectField('Maintenance Mode', choices=[('off', 'Off'), ('on', 'On')], default='off')
    audit_log_retention_days = IntegerField('Audit Log Retention (Days)', default=90)
    session_lifetime_minutes = IntegerField('Session Timeout (Minutes)', default=60)
    max_upload_mb = IntegerField('Max Upload Size (MB)', default=10)
    admin_alert_email = EmailField('Admin Alert Email', validators=[Optional(), Email()])
    
    # Workflow & Approvals
    require_multi_approval = SelectField('Multi-Level Approval?', choices=[('no', 'One Approver (Dean)'), ('yes', 'Two Approvers (Dept Head + Dean)')], default='no')
    default_rejection_reasons = TextAreaField('Default Rejection Reasons (One per line)', render_kw={"rows": 4, "placeholder": "Incomplete Learning Outcomes\nMismatched Weekly Topics\nFormatting Issues"})
    use_dynamic_copilot_prompts = SelectField('Use Template-Aware AI Prompts', choices=[('false', 'Off (Hardcoded Prompts)'), ('true', 'On (Dynamic Prompts from Template Profile)')], default='false')
    
    submit = SubmitField('Save All Global System Settings')

    def __init__(self, *args, **kwargs):
        super(SystemSettingsForm, self).__init__(*args, **kwargs)
        self.current_semester.choices = _academic_session_choices()
        self.active_semester.choices = _semester_choices()
        self.active_academic_year.choices = _academic_year_choices()

class AdminCreateUserForm(FlaskForm):
    first_name = StringField('First Name', validators=[DataRequired(), Length(max=80)])
    last_name = StringField('Last Name', validators=[DataRequired(), Length(max=80)])
    username = StringField('Username', validators=[DataRequired(), Length(min=4, max=80)])
    email = EmailField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired(), Length(min=8)])
    role = SelectField('Role', choices=[('teacher', 'Teacher'), ('dean', 'Dean'), ('admin', 'Admin')], validators=[DataRequired()])
    department = SelectField('Department', choices=[], validators=[Optional()])
    submit = SubmitField('Create & Verify User')

    def __init__(self, *args, **kwargs):
        super(AdminCreateUserForm, self).__init__(*args, **kwargs)
        self.department.choices = [('', 'No Department')] + get_department_choices()

class DeleteForm(FlaskForm):
    submit = SubmitField('Delete')

class OutcomeForm(FlaskForm):
    type = SelectField('Type', choices=[
        ('program', 'Program Outcome'), 
        ('course', 'Course Outcome'),
        ('institutional', 'Institutional Outcome') 
    ], validators=[DataRequired()])
    department = SelectField('Department', choices=[], validators=[Optional()]) 
    code = StringField('Code', validators=[DataRequired(), Length(max=20)], render_kw={"placeholder": "e.g. IT01 or T"})
    description = TextAreaField('Description', validators=[DataRequired()], render_kw={"rows": 4})
    submit = SubmitField('Save Outcome')

    def __init__(self, *args, **kwargs):
        super(OutcomeForm, self).__init__(*args, **kwargs)
        try:
            from app.utils import get_department_choices as get_cached_department_choices
            self.department.choices = get_cached_department_choices(include_blank_label='None (For Institutional Only)', use_ids=True)
        except:
            self.department.choices = [('', 'Error Loading Departments')]


class CopilotReferenceForm(FlaskForm):
    category = SelectField('Category', choices=[
        ('graduate_attributes', 'Graduate Attributes'),
        ('core_values', 'Core Values'),
        ('pqf_level_6', 'PQF Level 6'),
        ('aqrf_level_6', 'AQRF Level 6'),
        ('sdg', 'SDG'),
    ], validators=[DataRequired()])
    department = SelectField('Department', choices=[], validators=[DataRequired()])
    code = StringField('Code', validators=[Optional(), Length(max=50)], render_kw={"placeholder": "e.g. SDG 4, PQF1, AQRF1"})
    title = StringField('Title / Label', validators=[DataRequired(), Length(max=200)], render_kw={"placeholder": "e.g. Quality Education"})
    description = TextAreaField('Meaning / Guidance', validators=[Optional()], render_kw={"rows": 4, "placeholder": "Explain what this item means and when it should be aligned by the AI..."})
    submit = SubmitField('Save Copilot Reference')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            from app.utils import get_department_choices as get_cached_department_choices
            choices = get_cached_department_choices(use_ids=True)
            self.department.choices = [(value, label) for value, label in choices if value]
        except Exception:
            self.department.choices = [('', 'Error Loading Departments')]


class TemplateProfileUploadForm(FlaskForm):
    name = StringField('Profile Name', validators=[DataRequired(), Length(max=120)], render_kw={"placeholder": "e.g. IT Department CLP Template 2025-2026"})
    file = FileField('Department Template (.docx)', validators=[DataRequired(), FileAllowed(['docx'], 'Only .docx files allowed!')])
    submit = SubmitField('Upload & Profile Template')
