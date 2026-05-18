import json
import re
from dataclasses import dataclass
from typing import Any

from flask import current_app, session, url_for

from app import supabase
from app.services.ai_client import AIClient
from app.utils import (
    check_ai_quota,
    get_current_user_department,
    get_current_user_profile,
    get_department_outcomes_bundle,
    get_department_signatory_settings,
    get_system_prompt,
    get_system_settings_map,
    get_unread_notifications_count,
    invalidate_notifications_cache,
    log_audit,
    log_system_event,
    user_can_access_clp,
)


MAX_HISTORY_ITEMS = 10
MAX_HISTORY_CONTENT = 400


def _client():
    return current_app.config.get("SUPABASE_SERVICE") or current_app.config.get("SUPABASE_CLIENT") or supabase


def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _trim_text(value, limit=160):
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _normalize_message(message):
    return re.sub(r"\s+", " ", (message or "")).strip()


def _normalize_history_item(role, content):
    return {
        "role": role,
        "content": _trim_text(content, MAX_HISTORY_CONTENT),
    }


def _lowered(message):
    return _normalize_message(message).lower()


def _contains_any(text, keywords):
    return any(keyword in text for keyword in keywords)


def _extract_plan_id(message):
    match = re.search(r"\bplan\s+#?(\d+)\b", _lowered(message))
    if match:
        return _safe_int(match.group(1), None)
    match = re.search(r"\bclp\s+#?(\d+)\b", _lowered(message))
    if match:
        return _safe_int(match.group(1), None)
    return None


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _coerce_json_object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _looks_incomplete(text):
    cleaned = (text or "").strip()
    if not cleaned:
        return True
    if len(cleaned) < 40:
        return True
    if cleaned.endswith((",", ":", ";", "-", "(", "[", "{")):
        return True
    return False


def _role_docs(role):
    if role == "teacher":
        return [
            {
                "label": "Teacher Guide",
                "url": url_for("main.docs_teacher"),
                "snippet": "Use My Plans to manage drafts, resubmissions, AI generation, and dean submissions.",
            }
        ]
    if role == "dean":
        return [
            {
                "label": "Dean Guide",
                "url": url_for("main.docs_dean"),
                "snippet": "Use Courses for pending reviews, Faculty for department instructors, and Analytics for approval oversight.",
            }
        ]
    return [
        {
            "label": "Admin Guide",
            "url": url_for("main.docs_admin"),
            "snippet": "Use Dashboard, Operations, Analytics, Templates, Audit Logs, and Settings for system administration.",
        }
    ]


def _current_model_name():
    return get_system_prompt(_client(), "gemini_model", "gemini-1.5-flash")


def _institution_context():
    """Snapshot of institution-level settings the buddy can talk about."""
    try:
        settings = get_system_settings_map(local_supabase=_client()) or {}
    except Exception:
        settings = {}
    return {
        "institution_name": (settings.get("institution_name") or "").strip() or "University of La Salette",
        "institution_logo": (settings.get("institution_logo") or "").strip(),
        "active_semester": (settings.get("active_semester") or "").strip(),
        "active_academic_year": (settings.get("active_academic_year") or "").strip(),
        "vision": (settings.get("institution_vision") or "").strip(),
        "mission": (settings.get("institution_mission") or "").strip(),
    }


def _group_plans_by_subject(plans):
    """Return [{course_code, course_title, count, statuses, plans:[{id,subject,status}]}]."""
    buckets = {}
    for plan in plans or []:
        subject = (plan.get("subject") or "Untitled").strip()
        # Heuristic: course code is the first token of the subject when uppercase/alnum.
        first_token = subject.split(" ", 1)[0] if subject else ""
        code = first_token if first_token and any(ch.isdigit() for ch in first_token) else subject
        key = code.upper()
        bucket = buckets.setdefault(key, {
            "course_code": code,
            "course_title": subject,
            "count": 0,
            "statuses": {},
            "plans": [],
        })
        bucket["count"] += 1
        status = plan.get("status") or "unknown"
        bucket["statuses"][status] = bucket["statuses"].get(status, 0) + 1
        bucket["plans"].append({
            "id": plan.get("id"),
            "subject": subject,
            "status": status,
        })
    return sorted(buckets.values(), key=lambda b: (-b["count"], b["course_code"]))


def _teacher_subjects(user_id, active_semester, active_academic_year):
    """Read teacher_subjects for the active term (best-effort)."""
    if not user_id:
        return []
    try:
        query = (
            _client().table("teacher_subjects")
            .select("id, course_code, course_title, department, semester, academic_year, units, class_schedule, room_assignment")
            .eq("user_id", user_id)
        )
        if active_semester:
            query = query.eq("semester", active_semester)
        if active_academic_year:
            query = query.eq("academic_year", active_academic_year)
        data = query.limit(20).execute().data or []
        return data
    except Exception:
        return []


def _base_navigation_actions(role):
    common = [
        {
            "action_id": "open_dashboard",
            "label": "Open Dashboard",
            "description": "Go to your role dashboard.",
            "kind": "navigate",
            "requires_confirmation": False,
            "payload": {},
        },
        {
            "action_id": "open_notifications",
            "label": "Open Notifications",
            "description": "Review your latest notifications.",
            "kind": "navigate",
            "requires_confirmation": False,
            "payload": {},
        },
        {
            "action_id": "open_docs",
            "label": "Open Role Guide",
            "description": "Read the built-in workflow guide for your role.",
            "kind": "navigate",
            "requires_confirmation": False,
            "payload": {},
        },
    ]
    if role == "teacher":
        common.extend(
            [
                {
                    "action_id": "open_teacher_my_plans",
                    "label": "Open My Plans",
                    "description": "Go to your CLP management page.",
                    "kind": "navigate",
                    "requires_confirmation": False,
                    "payload": {},
                },
                {
                    "action_id": "open_teacher_department_plans",
                    "label": "Open Department Plans",
                    "description": "See approved plans shared within your department.",
                    "kind": "navigate",
                    "requires_confirmation": False,
                    "payload": {},
                },
                {
                    "action_id": "open_teacher_subjects",
                    "label": "Open My Subjects",
                    "description": "Manage your subjects for the active semester.",
                    "kind": "navigate",
                    "requires_confirmation": False,
                    "payload": {},
                },
            ]
        )
    elif role == "dean":
        common.extend(
            [
                {
                    "action_id": "open_dean_courses",
                    "label": "Open Dean Courses",
                    "description": "Review pending and approved department plans.",
                    "kind": "navigate",
                    "requires_confirmation": False,
                    "payload": {},
                },
                {
                    "action_id": "open_dean_analytics",
                    "label": "Open Dean Analytics",
                    "description": "See department approval and faculty progress.",
                    "kind": "navigate",
                    "requires_confirmation": False,
                    "payload": {},
                },
            ]
        )
    elif role == "admin":
        common.extend(
            [
                {
                    "action_id": "open_admin_operations",
                    "label": "Open Operations",
                    "description": "Review runtime warnings, failed tasks, and system health.",
                    "kind": "navigate",
                    "requires_confirmation": False,
                    "payload": {},
                },
                {
                    "action_id": "open_admin_analytics",
                    "label": "Open Analytics",
                    "description": "Review platform-wide approval and activity metrics.",
                    "kind": "navigate",
                    "requires_confirmation": False,
                    "payload": {},
                },
                {
                    "action_id": "open_admin_templates",
                    "label": "Open Templates",
                    "description": "Manage template assets and defaults.",
                    "kind": "navigate",
                    "requires_confirmation": False,
                    "payload": {},
                },
            ]
        )
    return common


@dataclass
class BuddyResponse:
    reply: str
    citations: list[dict[str, Any]]
    context_summary: dict[str, Any]
    suggested_actions: list[dict[str, Any]]
    requires_confirmation: bool
    session_state: dict[str, Any]
    intent: str

    def to_dict(self):
        return {
            "reply": self.reply,
            "citations": self.citations,
            "context_summary": self.context_summary,
            "suggested_actions": self.suggested_actions,
            "requires_confirmation": self.requires_confirmation,
            "session_state": self.session_state,
            "intent": self.intent,
        }


class AIBuddyService:
    @classmethod
    def build_context(cls, user_id, role, route_hint=None):
        profile = get_current_user_profile() or {}
        context = {
            "user_id": user_id,
            "role": role,
            "route_hint": route_hint,
            "assistant_meta": {
                "model_name": _current_model_name(),
                "name": "AI Buddy",
            },
            "profile": {
                "username": profile.get("username"),
                "first_name": profile.get("first_name"),
                "last_name": profile.get("last_name"),
                "email": profile.get("email"),
                "department": profile.get("assigned_department"),
                "title": profile.get("title"),
                "approved": profile.get("approved"),
                "active": profile.get("active"),
                "consultation_hours": profile.get("consultation_hours"),
                "signature_url": profile.get("signature_url"),
            },
            "docs": _role_docs(role),
            "allowed_actions": _base_navigation_actions(role),
        }

        institution = _institution_context()
        context["institution"] = institution

        department_name = profile.get("assigned_department")
        signatories = {}
        if department_name:
            try:
                signatories = get_department_signatory_settings(department_name=department_name, local_supabase=_client()) or {}
            except Exception:
                signatories = {}
        context["department_signatories"] = signatories

        if role == "teacher":
            context.update(cls._build_teacher_context(user_id, institution))
        elif role == "dean":
            context.update(cls._build_dean_context(user_id))
        else:
            context.update(cls._build_admin_context(user_id))

        # Mark context freshness — Buddy always has LIVE data from DB
        import datetime
        context["memory_refreshed_at"] = datetime.datetime.now().strftime("%b %d, %Y %I:%M %p")
        context["context_summary"] = cls._make_context_summary(context)
        return context

        context["context_summary"] = cls._make_context_summary(context)
        return context

    @classmethod
    def respond(cls, message, context, session_history):
        intent = cls.classify_intent(message)
        if intent == "unsupported_request":
            reply = cls._unsupported_reply(context)
            citations = context.get("docs", [])
            actions = [action for action in context.get("allowed_actions", []) if action["action_id"] in {"open_docs", "open_dashboard"}]
        elif intent == "action_request":
            reply, actions, citations = cls._handle_action_request(message, context)
        elif intent == "general_conversation":
            reply = cls._general_chat_reply(message, context)
            citations = []
            actions = []
        elif intent == "workflow_help":
            reply = cls._workflow_help_reply(message, context)
            citations = context.get("docs", [])
            actions = cls._workflow_actions(context)
        elif intent == "next_step_guidance":
            reply = cls._next_steps_reply(context)
            citations = context.get("docs", [])
            actions = cls._workflow_actions(context)
        else:
            reply, citations, actions = cls._data_reply(message, context)

        refined_reply = cls._maybe_refine_with_ai(intent, message, context, session_history, reply, actions, citations)
        requires_confirmation = any(action.get("requires_confirmation") for action in actions)
        return BuddyResponse(
            reply=refined_reply,
            citations=citations,
            context_summary=context.get("context_summary", {}),
            suggested_actions=actions,
            requires_confirmation=requires_confirmation,
            session_state={
                "history_count": len(session_history),
                "role": context.get("role"),
                "route_hint": context.get("route_hint"),
            },
            intent=intent,
        )

    @classmethod
    def execute_action(cls, action_id, payload, user_id, role):
        payload = payload or {}

        if action_id == "open_dashboard":
            return cls._ok_redirect("Opened your dashboard.", url_for("main.dashboard"))
        if action_id == "open_notifications":
            return cls._ok_redirect("Opened your notifications.", url_for("main.list_notifications"))
        if action_id == "open_docs":
            endpoint = "main.docs_teacher"
            if role == "dean":
                endpoint = "main.docs_dean"
            elif role == "admin":
                endpoint = "main.docs_admin"
            return cls._ok_redirect("Opened the role guide.", url_for(endpoint))
        if action_id == "open_teacher_my_plans" and role == "teacher":
            return cls._ok_redirect("Opened your plans.", url_for("teacher.teacher_my_clps"))
        if action_id == "open_teacher_department_plans" and role == "teacher":
            return cls._ok_redirect("Opened department plans.", url_for("teacher.teacher_all_clps"))
        if action_id == "open_teacher_subjects" and role == "teacher":
            return cls._ok_redirect("Opened your subjects.", url_for("teacher.manage_subjects"))
        if action_id == "open_teacher_copilot" and role == "teacher":
            return cls._ok_redirect("Opened AI Copilot.", url_for("teacher.create_clp_ai_beta"))
        if action_id == "open_teacher_template_profiles" and role == "teacher":
            return cls._ok_redirect("Opened template profiles.", url_for("teacher.list_template_profiles"))
        if action_id == "open_teacher_profile" and role == "teacher":
            return cls._ok_redirect("Opened your profile.", url_for("teacher.teacher_profile"))
        if action_id == "open_dean_courses" and role == "dean":
            return cls._ok_redirect("Opened dean courses.", url_for("dean.dean_courses"))
        if action_id == "open_dean_analytics" and role == "dean":
            return cls._ok_redirect("Opened dean analytics.", url_for("dean.dean_analytics"))
        if action_id == "open_admin_operations" and role == "admin":
            return cls._ok_redirect("Opened operations.", url_for("admin.admin_operations"))
        if action_id == "open_admin_analytics" and role == "admin":
            return cls._ok_redirect("Opened analytics.", url_for("admin.admin_analytics"))
        if action_id == "open_admin_templates" and role == "admin":
            return cls._ok_redirect("Opened templates.", url_for("admin.manage_templates"))
        if action_id == "mark_notifications_read_all":
            return cls._mark_notifications_read(user_id)
        if action_id == "open_plan":
            return cls._open_plan(payload, role, user_id)
        if action_id == "dean_open_review":
            return cls._dean_open_review(payload, user_id)
        if action_id == "teacher_submit_to_dean" and role == "teacher":
            return cls._teacher_submit_to_dean(payload, user_id)
        raise PermissionError("This buddy action is not allowed in the current beta.")

    @classmethod
    def classify_intent(cls, message):
        text = _lowered(message)
        if not text:
            return "workflow_help"

        unsupported_keywords = [
            "approve all",
            "delete",
            "remove user",
            "deactivate",
            "return plan",
            "reject user",
            "set default template",
            "delete template",
            "delete department",
        ]
        if _contains_any(text, unsupported_keywords):
            return "unsupported_request"

        if _contains_any(text, ["open ", "go to", "navigate", "show me", "take me", "mark ", "submit plan", "review plan"]):
            return "action_request"
        if _contains_any(text, ["what should i do", "what do i need", "next step", "blocked", "what next", "recommendation", "suggest", "advice", "prioritize", "what should i focus", "tips"]):
            return "next_step_guidance"
        if _contains_any(text, ["how do i", "how to", "where do i", "what is the process", "guide", "help me", "clone", "copy plan", "export", "download", "create a plan", "new plan", "change password", "reset password", "ai copilot", "what is clp", "what is lpms", "what does lpms", "learning plan", "manual", "documentation", "user guide", "upload", "edit document", "paano", "gumawa", "bagong", "plano", "course learning plan"]):
            return "workflow_help"
        if _contains_any(
            text,
            [
                "how many",
                "howmany",
                "count",
                "status",
                "which",
                "what plans",
                "what tasks",
                "notifications",
                "feedback",
                "drafted",
                "draft",
                "approved",
                "pending",
                "returned",
                "department",
                "role",
                "email",
                "username",
                "name",
                "title",
                "account",
                "profile",
                "institution",
                "school",
                "university",
                "campus",
                "semester",
                "term",
                "academic year",
                "school year",
                "vice president",
                " vp",
                "dean",
                "coordinator",
                "signatory",
                "signatories",
                "subject",
                "subjects",
                "course code",
                "by subject",
                "consultation",
                "office hours",
                "signature",
                "vision",
                "mission",
                "outcome",
                "program outcome",
                "course outcome",
                "institutional outcome",
                " po ",
                " co ",
                " io ",
                "colleague",
                "faculty",
                "instructor",
                "other teacher",
                "quota",
                "ai limit",
                "generation limit",
                "remaining",
                "class schedule",
                "my schedule",
                "room assignment",
                "classroom",
                "latest plan",
                "most recent",
                "newest plan",
                "last plan",
                "ready to submit",
                "submittable",
                "can i submit",
                "need attention",
                "action needed",
                "returned plan",
                "what can you",
                "capabilities",
                "what can i ask",
                "our vice",
                "the vice",
                "vice ",
                "program coo",
                "coop",
                "coor",
                "unit",
                "total unit",
                "credit",
                "date today",
                "today",
                "when was",
                "created",
                "submitted",
                "specific plan",
                "plan #",
                "plan id",
                "tell me about",
                "details",
                "template",
                "progress",
                "completion",
                "how am i doing",
                "my performance",
                "failed",
                "error",
                "who is the admin",
                "contact admin",
                "deadline",
                "due date",
                "when is it due",
                "announcement",
                "department stat",
                "department progress",
                "how is our department",
            ],
        ) or ("plan" in text or "clp" in text):
            return "data_question"
        return "general_conversation"

    @classmethod
    def append_session_history(cls, role, content):
        history = session.get("buddy_history", [])
        history.append(_normalize_history_item(role, content))
        session["buddy_history"] = history[-MAX_HISTORY_ITEMS:]
        session.modified = True

    @classmethod
    def get_session_history(cls):
        return session.get("buddy_history", [])

    @classmethod
    def reset_session_history(cls):
        session["buddy_history"] = []
        session.modified = True

    @classmethod
    def _build_teacher_context(cls, user_id, institution=None):
        institution = institution or {}

        # Load all system settings for comprehensive knowledge
        all_settings = {}
        try:
            all_settings = get_system_settings_map(local_supabase=_client()) or {}
        except Exception:
            pass
        
        plans = (
            _client().table("course_learning_plans")
            .select("id, subject, status, department, date_posted, dean_comments")
            .eq("user_id", user_id)
            .order("date_posted", desc=True)
            .execute()
            .data
            or []
        )
        tasks = (
            _client().table("background_tasks")
            .select("id, plan_id, status, progress_percent, progress_label, error_message, created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(8)
            .execute()
            .data
            or []
        )
        notifications = (
            _client().table("notifications")
            .select("id, message, is_read, timestamp")
            .eq("user_id", user_id)
            .order("timestamp", desc=True)
            .limit(8)
            .execute()
            .data
            or []
        )

        stats = {"total": len(plans), "approved": 0, "pending": 0, "returned": 0, "draft": 0}
        submittable = []
        feedback_items = []
        for plan in plans:
            status = plan.get("status")
            if status == "approved":
                stats["approved"] += 1
            elif status == "pending":
                stats["pending"] += 1
            elif status == "returned_for_revision":
                stats["returned"] += 1
            else:
                stats["draft"] += 1
            if status in {"draft", "draft_review", "returned_for_revision"}:
                submittable.append({"id": plan["id"], "subject": plan.get("subject"), "status": status})
            if plan.get("dean_comments"):
                feedback_items.append({"id": plan["id"], "subject": plan.get("subject"), "comments": plan.get("dean_comments")})

        subjects = _teacher_subjects(
            user_id,
            institution.get("active_semester"),
            institution.get("active_academic_year"),
        )
        plans_by_subject = _group_plans_by_subject(plans)

        department = get_current_user_department()

        # Faculty peers in the same department (best-effort).
        faculty_peers = 0
        try:
            if department:
                fp_res = (
                    _client().table("users")
                    .select("id", count="exact")
                    .eq("role", "teacher")
                    .eq("assigned_department", department)
                    .eq("approved", True)
                    .execute()
                )
                faculty_peers = fp_res.count or 0
        except Exception:
            pass

        # Outcomes summary.
        outcomes_summary = {}
        try:
            bundle = get_department_outcomes_bundle(department)
            outcomes_summary = {
                "program_outcomes": [
                    {"code": po.get("code"), "description": _trim_text(po.get("description"), 100)}
                    for po in (bundle.get("program_outcomes") or [])[:12]
                ],
                "course_outcomes": [
                    {"code": co.get("code"), "description": _trim_text(co.get("description"), 100)}
                    for co in (bundle.get("course_outcomes") or [])[:12]
                ],
                "institutional_outcomes": [
                    {"code": io.get("code"), "description": _trim_text(io.get("description"), 100)}
                    for io in (bundle.get("institutional_outcomes") or [])[:12]
                ],
            }
        except Exception:
            pass

        # AI generation quota.
        ai_quota = {}
        try:
            allowed, used, limit = check_ai_quota(user_id)
            ai_quota = {"allowed": allowed, "used_today": used, "daily_limit": limit}
        except Exception:
            pass

        # ── Peer teachers with names ──
        peer_teachers = []
        try:
            if department:
                pt_query = _client().table("users").select("id,username,first_name,last_name,email,title,role").eq("role","teacher").eq("assigned_department",department).eq("approved",True)
                pt_data = pt_query.execute().data or []
                peer_teachers = [
                    {"name": f"{p.get('first_name','')} {p.get('last_name','')}".strip() or p.get('username','?'),
                     "title": p.get('title',''), "email": p.get('email','')}
                    for p in pt_data if p.get("id") != user_id
                ]
        except Exception:
            pass

        # ── System knowledge base ──
        sys_base = {
            "school_name": all_settings.get("institution_name",""),
            "core_values": (all_settings.get("copilot_core_values","") or "").strip(),
            "graduate_attributes": (all_settings.get("copilot_graduate_attributes","") or "").strip(),
            "announcement": all_settings.get("announcement_text",""),
            "deadline": all_settings.get("submission_deadline",""),
            "daily_ai_limit": all_settings.get("daily_ai_limit","5"),
            "allow_signups": all_settings.get("allow_signups","yes"),
            "session_lifetime": all_settings.get("session_lifetime_minutes","60"),
        }

        return {
            "stats": stats,
            "plans": plans[:15],
            "tasks": tasks,
            "notifications": notifications,
            "unread_notifications": get_unread_notifications_count(user_id),
            "submittable_plans": submittable,
            "feedback_items": feedback_items[:5],
            "department": department,
            "subjects": subjects,
            "plans_by_subject": plans_by_subject,
            "faculty_peers": faculty_peers,
            "peer_teachers": peer_teachers,
            "system_base": sys_base,
            "outcomes_summary": outcomes_summary,
            "ai_quota": ai_quota,
        }

    @classmethod
    def _build_dean_context(cls, user_id):
        department = get_current_user_department()
        plan_query = _client().table("course_learning_plans").select("id, subject, status, department, user_id, date_posted").order("date_posted", desc=True)
        faculty_query = _client().table("users").select("id, username, first_name, last_name, assigned_department, approved").eq("role", "teacher")
        if department:
            plan_query = plan_query.eq("department", department)
            faculty_query = faculty_query.eq("assigned_department", department)
        plans = plan_query.limit(20).execute().data or []
        faculty = faculty_query.execute().data or []
        notifications = (
            _client().table("notifications")
            .select("id, message, is_read, timestamp")
            .eq("user_id", user_id)
            .order("timestamp", desc=True)
            .limit(8)
            .execute()
            .data
            or []
        )

        stats = {"total": len(plans), "approved": 0, "pending": 0, "returned": 0}
        pending_plans = []
        for plan in plans:
            status = plan.get("status")
            if status == "approved":
                stats["approved"] += 1
            elif status == "pending":
                stats["pending"] += 1
                pending_plans.append({"id": plan["id"], "subject": plan.get("subject")})
            elif status == "returned_for_revision":
                stats["returned"] += 1

        return {
            "department": department,
            "stats": stats,
            "plans": plans,
            "pending_plans": pending_plans[:8],
            "faculty_count": len(faculty),
            "notifications": notifications,
            "unread_notifications": get_unread_notifications_count(user_id),
            "plans_by_subject": _group_plans_by_subject(plans),
        }

    @classmethod
    def _build_admin_context(cls, user_id):
        users = _client().table("users").select("id, role, approved, active").execute().data or []
        plans = _client().table("course_learning_plans").select("id, status, department, subject, user_id").execute().data or []
        tasks = (
            _client().table("background_tasks")
            .select("id, status, task_name, plan_id, created_at, error_message")
            .order("created_at", desc=True)
            .limit(10)
            .execute()
            .data
            or []
        )
        events = (
            _client().table("system_events")
            .select("id, category, level, message, created_at")
            .order("created_at", desc=True)
            .limit(10)
            .execute()
            .data
            or []
        )
        notifications = (
            _client().table("notifications")
            .select("id, message, is_read, timestamp")
            .eq("user_id", user_id)
            .order("timestamp", desc=True)
            .limit(8)
            .execute()
            .data
            or []
        )
        stats = {
            "users_total": len(users),
            "pending_users": sum(1 for user in users if not user.get("approved")),
            "inactive_users": sum(1 for user in users if user.get("active") is False),
            "plans_total": len(plans),
            "plans_pending": sum(1 for plan in plans if plan.get("status") == "pending"),
            "failed_tasks": sum(1 for task in tasks if task.get("status") == "failed"),
            "warning_events": sum(1 for event in events if event.get("level") in {"warning", "error"}),
        }
        return {
            "stats": stats,
            "plans": plans[:20],
            "tasks": tasks,
            "events": events,
            "notifications": notifications,
            "unread_notifications": get_unread_notifications_count(user_id),
        }

    @classmethod
    def _make_context_summary(cls, context):
        role = context.get("role")
        stats = context.get("stats", {})
        if role == "teacher":
            return {
                "role": role,
                "plan_counts": stats,
                "unread_notifications": context.get("unread_notifications", 0),
                "active_tasks": sum(1 for task in context.get("tasks", []) if task.get("status") in {"queued", "processing"}),
                "returned_plans": stats.get("returned", 0),
            }
        if role == "dean":
            return {
                "role": role,
                "department": context.get("department"),
                "plan_counts": stats,
                "faculty_count": context.get("faculty_count", 0),
                "unread_notifications": context.get("unread_notifications", 0),
            }
        return {
            "role": role,
            "platform": stats,
            "unread_notifications": context.get("unread_notifications", 0),
        }

    @classmethod
    def _data_reply(cls, message, context):
        text = _lowered(message)
        role = context.get("role")
        summary = context.get("context_summary", {})
        profile = context.get("profile", {})
        docs = context.get("docs", [])
        actions = []
        citations = []

        institution = context.get("institution", {}) or {}
        signatories = context.get("department_signatories", {}) or {}

        if _contains_any(text, ["institution", "school", "university", "campus"]) and "department" not in text:
            inst_name = institution.get("institution_name") or "the institution"
            sem = institution.get("active_semester") or "an unspecified semester"
            ay = institution.get("active_academic_year") or "an unspecified academic year"
            reply = f"You are using the LPMS deployment for {inst_name}. The active term is {sem}, AY {ay}."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["semester", "term", "academic year", "school year", "current ay"]):
            sem = institution.get("active_semester") or "not set"
            ay = institution.get("active_academic_year") or "not set"
            reply = f"The currently active term is {sem} of AY {ay}."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["vice president", " vp", "vp name", "our vice", "the vice", "vice "]):
            vp = signatories.get("vice_president_name") or ""
            vp_title = signatories.get("vice_president_title") or "Vice President"
            dept = profile.get("department")
            if vp:
                reply = f"The {vp_title} on file for the {dept or 'your'} department is {vp}."
            else:
                reply = f"I don't see a Vice President recorded for the {dept or 'your'} department yet. An admin can set this under Departments."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["coordinator", "program coordinator", "program coo", "coop", "coor"]):
            pc = signatories.get("program_coordinator_name") or ""
            pc_title = signatories.get("program_coordinator_title") or "Program Coordinator"
            dept = profile.get("department")
            if pc:
                reply = f"The {pc_title} on file for the {dept or 'your'} department is {pc}."
            else:
                reply = f"I don't see a program coordinator recorded for the {dept or 'your'} department yet."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["who is our dean", "who is out dean", "who's our dean", "our dean", "my dean", "dean name"]) or ("dean" in text and _contains_any(text, ["who", "name"])):
            dean = signatories.get("dean_name") or ""
            dean_title = signatories.get("dean_title") or "Dean"
            dept = profile.get("department")
            if dean:
                reply = f"The {dean_title} on file for the {dept or 'your'} department is {dean}."
            else:
                reply = f"I don't see a dean recorded for the {dept or 'your'} department yet. An admin can set this under Departments."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["signatory", "signatories"]):
            lines = []
            if signatories.get("dean_name"):
                lines.append(f"{signatories.get('dean_title') or 'Dean'}: {signatories['dean_name']}")
            if signatories.get("program_coordinator_name"):
                lines.append(f"{signatories.get('program_coordinator_title') or 'Program Coordinator'}: {signatories['program_coordinator_name']}")
            if signatories.get("vice_president_name"):
                lines.append(f"{signatories.get('vice_president_title') or 'Vice President'}: {signatories['vice_president_name']}")
            reply = ("Department signatories on file - " + "; ".join(lines) + ".") if lines else "No department signatories are recorded yet."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["vision", "mission"]):
            vision = institution.get("vision") or ""
            mission = institution.get("mission") or ""
            parts = []
            if "vision" in text and vision:
                parts.append(f"Vision: {_trim_text(vision, 200)}")
            if "mission" in text and mission:
                parts.append(f"Mission: {_trim_text(mission, 200)}")
            if not parts:
                if vision:
                    parts.append(f"Vision: {_trim_text(vision, 200)}")
                if mission:
                    parts.append(f"Mission: {_trim_text(mission, 200)}")
            reply = (" | ".join(parts)) if parts else "The institution vision and mission are not configured in the system settings yet."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["consultation", "office hours"]):
            ch = profile.get("consultation_hours")
            if isinstance(ch, dict) and ch:
                lines = []
                for day in ("monday", "tuesday", "wednesday", "thursday", "friday"):
                    slot = ch.get(day, {})
                    t = (slot.get("time") or "").strip()
                    r = (slot.get("room") or "").strip()
                    if t:
                        lines.append(f"{day.capitalize()}: {t}" + (f" (Room {r})" if r else ""))
                if lines:
                    reply = "Your consultation hours: " + "; ".join(lines) + "."
                else:
                    reply = "You have consultation hours saved but no specific days/times are filled in. Update them in your profile."
            else:
                reply = "No consultation hours are set in your profile yet. Go to your Profile page to set them."
            citations = [{"label": "Profile", "url": url_for("teacher.teacher_profile") if role == "teacher" else url_for("main.dashboard")}]
            return reply, citations, actions

        if "signature" in text and _contains_any(text, ["my signature", "signature", "upload", "have a"]):
            sig = profile.get("signature_url")
            if sig:
                reply = "You have a signature uploaded in your profile."
            else:
                reply = "You have not uploaded a signature yet. Upload one in your Profile page - it's required for finalized CLPs."
            citations = [{"label": "Profile", "url": url_for("teacher.teacher_profile") if role == "teacher" else url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["latest plan", "most recent", "newest plan", "last plan"]):
            plans = context.get("plans", [])
            if plans:
                top = plans[0]
                reply = f"Your most recent plan is \"{top.get('subject', '?')}\" (ID {top.get('id')}) with status: {top.get('status', '?')}."
            else:
                reply = "You have no plans yet. Create one using the AI Copilot."
            citations = [{"label": "My Plans", "url": url_for("teacher.teacher_my_clps")}] if role == "teacher" else [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["need attention", "action needed", "what should i fix", "returned plan", "needs work"]):
            returned = [p for p in context.get("plans", []) if p.get("status") == "returned_for_revision"]
            fb = context.get("feedback_items", [])
            parts = []
            if returned:
                names = ", ".join(f"\"{p.get('subject', '?')}\" (#{p.get('id')})" for p in returned[:3])
                parts.append(f"{len(returned)} plan(s) returned for revision: {names}")
            if fb:
                parts.append(f"{len(fb)} plan(s) with dean feedback you should review")
            if parts:
                reply = "Plans needing your attention - " + "; ".join(parts) + "."
            else:
                reply = "Nothing flagged right now. All your plans are either in draft, pending review, or already approved."
            citations = [{"label": "My Plans", "url": url_for("teacher.teacher_my_clps")}]
            actions = [a for a in context.get("allowed_actions", []) if a["action_id"] == "open_teacher_my_plans"]
            return reply, citations, actions

        if _contains_any(text, ["ready to submit", "submittable", "can i submit", "eligible to submit"]):
            submittable = context.get("submittable_plans", [])
            if submittable:
                names = "; ".join(f"\"{s.get('subject', '?')}\" (#{s.get('id')}, {s.get('status')})" for s in submittable[:4])
                reply = f"You have {len(submittable)} plan(s) that can be submitted to the dean: {names}."
            else:
                reply = "None of your current plans are eligible for submission right now. They may already be pending or approved."
            citations = [{"label": "My Plans", "url": url_for("teacher.teacher_my_clps")}]
            actions = [a for a in context.get("allowed_actions", []) if a["action_id"] == "open_teacher_my_plans"]
            return reply, citations, actions

        if _contains_any(text, ["colleague", "faculty", "other teacher", "instructor", "how many teacher", "fellow teacher", "peers"]):
            fp = context.get("faculty_peers", 0)
            dept = profile.get("department") or "your department"
            if fp:
                reply = f"There are {fp} approved teacher(s) in {dept} (including you)."
            else:
                reply = f"I couldn't determine the faculty count for {dept}."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["program outcome", "course outcome", "institutional outcome", "outcome", " po ", " co ", " io "]):
            outcomes = context.get("outcomes_summary", {})
            pos = outcomes.get("program_outcomes", [])
            cos = outcomes.get("course_outcomes", [])
            ios = outcomes.get("institutional_outcomes", [])
            parts = []
            if _contains_any(text, ["program outcome", " po ", "all outcome", "outcome"]) and pos:
                po_list = ", ".join(f"{po.get('code')}: {po.get('description', '')}" for po in pos[:6])
                more = f" (+{len(pos) - 6} more)" if len(pos) > 6 else ""
                parts.append(f"Program Outcomes ({len(pos)}): {po_list}{more}")
            if _contains_any(text, ["course outcome", " co ", "all outcome"]) and cos:
                co_list = ", ".join(f"{co.get('code')}: {co.get('description', '')}" for co in cos[:6])
                more = f" (+{len(cos) - 6} more)" if len(cos) > 6 else ""
                parts.append(f"Course Outcomes ({len(cos)}): {co_list}{more}")
            if _contains_any(text, ["institutional outcome", " io ", "all outcome"]) and ios:
                io_list = ", ".join(f"{io.get('code')}: {io.get('description', '')}" for io in ios[:6])
                more = f" (+{len(ios) - 6} more)" if len(ios) > 6 else ""
                parts.append(f"Institutional Outcomes ({len(ios)}): {io_list}{more}")
            if not parts:
                counts = []
                if pos:
                    counts.append(f"{len(pos)} PO(s)")
                if cos:
                    counts.append(f"{len(cos)} CO(s)")
                if ios:
                    counts.append(f"{len(ios)} IO(s)")
                parts.append(f"Your department has {', '.join(counts) if counts else 'no outcomes configured yet'}.")
            reply = " | ".join(parts)
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["class schedule", "my schedule", "when is my class", "what time"]):
            subjects = context.get("subjects", []) or []
            lines = [
                f"{s.get('course_code', '?')}: {s.get('class_schedule', 'no schedule set')}"
                for s in subjects[:8] if s.get("class_schedule")
            ]
            if lines:
                reply = "Your class schedules this term: " + "; ".join(lines) + "."
            else:
                reply = "No class schedules found in your registered subjects. Set them under My Subjects."
            citations = [{"label": "My Subjects", "url": url_for("teacher.manage_subjects")}]
            return reply, citations, actions

        if _contains_any(text, ["room assignment", "my room", "classroom", "where is my class"]):
            subjects = context.get("subjects", []) or []
            lines = [
                f"{s.get('course_code', '?')}: Room {s.get('room_assignment')}"
                for s in subjects[:8] if s.get("room_assignment")
            ]
            if lines:
                reply = "Your room assignments this term: " + "; ".join(lines) + "."
            else:
                reply = "No room assignments found in your registered subjects. Set them under My Subjects."
            citations = [{"label": "My Subjects", "url": url_for("teacher.manage_subjects")}]
            return reply, citations, actions

        if _contains_any(text, ["quota", "ai limit", "generation limit", "remaining generation", "how many generation"]):
            quota = context.get("ai_quota", {})
            if quota:
                reply = f"You have used {quota.get('used_today', 0)} of your {quota.get('daily_limit', '?')} daily AI generation(s). {'You can still generate.' if quota.get('allowed') else 'You have reached the daily limit - try again tomorrow.'}"
            else:
                reply = "I couldn't retrieve your AI quota right now."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["what can you", "capabilities", "what can i ask", "what do you know", "help me with"]):
            reply = (
                "I can answer questions about: your profile (name, email, title, consultation hours, signature), "
                "your department (dean, VP, coordinator, signatories, faculty count), "
                "institution (name, semester, academic year, vision, mission), "
                "your subjects (course codes, schedules, rooms), "
                "your plans (count, status, latest, returned, submittable, grouped by subject), "
                "outcomes (PO, CO, IO), AI quota, notifications, background tasks, and dean feedback. "
                "I can also navigate you to pages or submit eligible plans to the dean."
            )
            citations = context.get("docs", [])
            return reply, citations, actions

        if role == "teacher" and _contains_any(text, ["my subjects", "subjects", "course code", "my courses this"]):
            subjects = context.get("subjects", []) or []
            if subjects:
                preview = "; ".join(f"{s.get('course_code','?')} - {s.get('course_title','?')}" for s in subjects[:5])
                more = f" (+{len(subjects) - 5} more)" if len(subjects) > 5 else ""
                reply = f"You have {len(subjects)} subject(s) registered for {institution.get('active_semester') or 'the active term'} AY {institution.get('active_academic_year') or ''}: {preview}{more}."
            else:
                reply = "You have no subjects registered for the active term yet. Add them under My Subjects so the AI Copilot can use them."
            citations = [{"label": "My Subjects", "url": url_for("teacher.manage_subjects")}]
            actions = [action for action in context.get("allowed_actions", []) if action["action_id"] == "open_teacher_subjects"]
            return reply, citations, actions

        if _contains_any(text, ["by subject", "per subject", "group", "enumerate", "list plans", "plans for"]):
            grouped = context.get("plans_by_subject", []) or []
            if grouped:
                lines = []
                for bucket in grouped[:6]:
                    statuses = ", ".join(f"{count} {stat}" for stat, count in bucket.get("statuses", {}).items())
                    lines.append(f"{bucket.get('course_code','?')} - {bucket.get('count',0)} plan(s) ({statuses})")
                reply = "Plans grouped by subject - " + "; ".join(lines) + "."
            else:
                reply = "There are no plans to group by subject yet."
            citations = [{"label": "My Plans", "url": url_for("teacher.teacher_my_clps")}] if role == "teacher" else [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if "department" in text:
            department = profile.get("department")
            if department:
                reply = f"You are currently assigned to the {department} department."
            else:
                reply = "You do not have a department assigned in your current account profile."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if "role" in text and "workflow" not in text:
            reply = f"Your current LPMS role is {role}."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if "email" in text:
            email = profile.get("email")
            if email:
                reply = f"Your account email is {email}."
            else:
                reply = "I do not see an email address stored in your current profile."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if "username" in text:
            username = profile.get("username")
            if username:
                reply = f"Your username is {username}."
            else:
                reply = "I do not see a username stored in your current profile."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if ("name" in text and "what" in text) or "who am i" in text:
            full_name = " ".join(part for part in [profile.get("first_name"), profile.get("last_name")] if part).strip()
            if full_name:
                reply = f"Your name in LPMS is {full_name}."
            elif profile.get("username"):
                reply = f"I only see your username right now, which is {profile.get('username')}."
            else:
                reply = "I do not see a stored display name in your current profile."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if "title" in text:
            title = profile.get("title")
            if title:
                reply = f"Your recorded title is {title}."
            else:
                reply = "I do not see a title saved in your current profile."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if "account" in text or "approved" in text or "active" in text:
            approved = profile.get("approved")
            active = profile.get("active")
            approved_text = "approved" if approved else "pending approval"
            active_text = "active" if active is not False else "inactive"
            reply = f"Your account is currently {active_text} and {approved_text}."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if "notification" in text:
            unread = context.get("unread_notifications", 0)
            recent = context.get("notifications", [])[:3]
            if recent:
                recent_lines = "; ".join(_trim_text(item.get("message"), 80) for item in recent)
                reply = f"You currently have {unread} unread notification(s). Recent items include: {recent_lines}."
            else:
                reply = f"You currently have {unread} unread notification(s)."
            citations = [{"label": "Notifications", "url": url_for("main.list_notifications")}]
            actions = [
                {
                    "action_id": "open_notifications",
                    "label": "Open Notifications",
                    "description": "Review the latest notification feed.",
                    "kind": "navigate",
                    "requires_confirmation": False,
                    "payload": {},
                },
                {
                    "action_id": "mark_notifications_read_all",
                    "label": "Mark All Notifications Read",
                    "description": "Mark all your notifications as read.",
                    "kind": "execute",
                    "requires_confirmation": True,
                    "payload": {},
                },
            ]
            return reply, citations, actions

        if "task" in text or "generation" in text or "loading" in text:
            tasks = context.get("tasks", [])
            active = [task for task in tasks if task.get("status") in {"queued", "processing"}]
            failed = [task for task in tasks if task.get("status") == "failed"]
            if active:
                task_lines = "; ".join(
                    f"plan {task.get('plan_id')} is {task.get('status')} at {task.get('progress_percent') or 0}%"
                    for task in active[:3]
                )
                reply = f"You currently have {len(active)} active background task(s). {task_lines}."
            elif failed:
                reply = f"You have no active tasks right now, but {len(failed)} recent task(s) failed. Check the related plan pages for recovery steps."
            else:
                reply = "You do not have any active background tasks right now."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            actions = [action for action in context.get("allowed_actions", []) if action["action_id"] in {"open_dashboard", "open_teacher_my_plans"}]
            return reply, citations, actions

        if "feedback" in text or "comment" in text:
            feedback_items = context.get("feedback_items", [])
            if feedback_items:
                top = feedback_items[0]
                reply = f"You have {len(feedback_items)} plan(s) with dean feedback. The most recent visible item is {top.get('subject')} with feedback: {_trim_text(top.get('comments'), 120)}"
            else:
                reply = "I do not see any recent dean feedback attached to your visible plans."
            citations = [{"label": "My Plans", "url": url_for("teacher.teacher_my_clps")}]
            actions = [action for action in context.get("allowed_actions", []) if action["action_id"] == "open_teacher_my_plans"]
            return reply, citations, actions

        if _contains_any(text, ["specific plan", "plan #", "plan id", "tell me about plan", "details of plan", "status of plan"]):
            plan_id = _extract_plan_id(text)
            plans = context.get("plans", [])
            if plan_id:
                match = next((p for p in plans if str(p.get("id")) == str(plan_id)), None)
                if match:
                    reply = f"Plan #{match['id']} - Subject: {match.get('subject', '?')}, Status: {match.get('status', '?')}, Department: {match.get('department', '?')}, Posted: {match.get('date_posted', '?')}."
                    if match.get("dean_comments"):
                        reply += f" Dean comments: {_trim_text(match['dean_comments'], 100)}."
                else:
                    reply = f"I don't see plan #{plan_id} in your visible plans list."
            else:
                reply = "Please include the plan ID or number (e.g., 'tell me about plan #238')."
            citations = [{"label": "My Plans", "url": url_for("teacher.teacher_my_clps")}] if role == "teacher" else [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["unit", "total unit", "credit", "how many unit"]):
            subjects = context.get("subjects", []) or []
            if subjects:
                subj_units = []
                total = 0
                for s in subjects[:10]:
                    u = _safe_int(s.get("units"), 0)
                    total += u
                    subj_units.append(f"{s.get('course_code', '?')}: {u} unit(s)")
                reply = f"You have {len(subjects)} subject(s) totaling {total} unit(s) this term. Breakdown: {'; '.join(subj_units)}."
            else:
                reply = "No subjects registered for the active term, so no unit count is available."
            citations = [{"label": "My Subjects", "url": url_for("teacher.manage_subjects")}]
            return reply, citations, actions

        if _contains_any(text, ["date today", "today's date", "current date", "what day", "what is today"]):
            from datetime import datetime
            now = datetime.now()
            reply = f"Today is {now.strftime('%A, %B %d, %Y')} ({now.strftime('%I:%M %p')})."
            return reply, citations, actions

        if _contains_any(text, ["when was", "date posted", "date created", "when did i", "when submitted"]):
            plan_id = _extract_plan_id(text)
            plans = context.get("plans", [])
            if plan_id:
                match = next((p for p in plans if str(p.get("id")) == str(plan_id)), None)
                if match:
                    reply = f"Plan #{match['id']} ({match.get('subject', '?')}) was posted on {match.get('date_posted', 'unknown date')}."
                else:
                    reply = f"I don't see plan #{plan_id} in your visible plans."
            elif plans:
                top = plans[0]
                reply = f"Your most recent plan (#{top.get('id')}, {top.get('subject', '?')}) was posted on {top.get('date_posted', 'unknown date')}. Specify a plan ID for others."
            else:
                reply = "You have no plans yet."
            citations = [{"label": "My Plans", "url": url_for("teacher.teacher_my_clps")}] if role == "teacher" else [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["template", "what template", "available template", "department template"]):
            dept = profile.get("department")
            try:
                tpl_res = _client().table("templates").select("id, name, department_id").limit(5).execute()
                tpls = tpl_res.data or []
                if tpls:
                    tpl_names = "; ".join(f"{t.get('name', '?')}" for t in tpls[:5])
                    reply = f"Available templates: {tpl_names}. Your admin sets the default template for each department."
                else:
                    reply = "No templates have been uploaded yet. An admin needs to upload one under Templates."
            except Exception:
                reply = "I couldn't fetch template information right now."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["progress", "completion", "how am i doing", "my performance", "how far"]):
            stats = context.get("stats", {})
            total = stats.get("total", 0)
            approved = stats.get("approved", 0)
            if total > 0:
                pct = round((approved / total) * 100)
                reply = f"Your CLP completion rate: {approved}/{total} plans approved ({pct}%). {stats.get('pending', 0)} pending, {stats.get('returned', 0)} returned, {stats.get('draft', 0)} in draft."
            else:
                reply = "You have no plans yet, so there's no progress to report. Start by creating a plan with the AI Copilot."
            citations = [{"label": "My Plans", "url": url_for("teacher.teacher_my_clps")}] if role == "teacher" else [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["failed", "error", "why did", "generation fail"]):
            tasks = context.get("tasks", [])
            failed = [t for t in tasks if t.get("status") == "failed"]
            if failed:
                t = failed[0]
                err = _trim_text(t.get("error_message") or "no details", 150)
                reply = f"Your most recent failed task (plan {t.get('plan_id')}): {err}. Try re-generating from the plan page."
            else:
                reply = "No failed tasks found in your recent activity."
            citations = [{"label": "My Plans", "url": url_for("teacher.teacher_my_clps")}] if role == "teacher" else [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["department stat", "department progress", "how is our department", "department doing"]):
            dept = profile.get("department") or "your department"
            stats = context.get("stats", {})
            fp = context.get("faculty_peers", 0)
            reply = (
                f"{dept} snapshot: {fp} teacher(s), {stats.get('total', 0)} plan(s) "
                f"({stats.get('approved', 0)} approved, {stats.get('pending', 0)} pending, "
                f"{stats.get('returned', 0)} returned, {stats.get('draft', 0)} draft)."
            )
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["deadline", "due date", "when is it due", "submission deadline"]):
            reply = "LPMS does not enforce hard deadlines automatically - submission timelines are set by your dean or admin. Check your notifications or ask your dean directly for any specific due dates."
            citations = [{"label": "Notifications", "url": url_for("main.list_notifications")}]
            return reply, citations, actions

        if _contains_any(text, ["who is the admin", "contact admin", "reach admin", "admin email"]):
            try:
                admin_res = _client().table("users").select("first_name, last_name, email").eq("role", "admin").eq("approved", True).limit(1).execute()
                admin_data = (admin_res.data or [{}])[0] if admin_res.data else {}
                if admin_data:
                    admin_name = f"{admin_data.get('first_name', '')} {admin_data.get('last_name', '')}".strip()
                    admin_email = admin_data.get("email", "")
                    reply = f"Your system admin is {admin_name or 'not named'}" + (f" ({admin_email})" if admin_email else "") + "."
                else:
                    reply = "I couldn't find an admin account on record."
            except Exception:
                reply = "I couldn't fetch admin info right now."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["announcement", "system event", "recent event"]):
            try:
                ev_res = _client().table("system_events").select("event_type, message, created_at").order("created_at", desc=True).limit(3).execute()
                events = ev_res.data or []
                if events:
                    ev_lines = "; ".join(f"{e.get('event_type', '?')}: {_trim_text(e.get('message', ''), 80)} ({e.get('created_at', '?')})" for e in events)
                    reply = f"Recent system events: {ev_lines}."
                else:
                    reply = "No recent system events or announcements."
            except Exception:
                reply = "I couldn't fetch system events right now."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if "plan" in text or "clp" in text or "how many" in text or "count" in text or "status" in text:
            if role == "teacher":
                stats = summary.get("plan_counts", {})
                reply = (
                    f"You have created {stats.get('total', 0)} plan(s): "
                    f"{stats.get('approved', 0)} approved, "
                    f"{stats.get('pending', 0)} pending, "
                    f"{stats.get('returned', 0)} returned for revision, "
                    f"and {stats.get('draft', 0)} still in draft."
                )
                citations = [{"label": "My Plans", "url": url_for("teacher.teacher_my_clps")}]
                actions = [action for action in context.get("allowed_actions", []) if action["action_id"] in {"open_teacher_my_plans", "open_teacher_department_plans"}]
                return reply, citations, actions
            if role == "dean":
                stats = summary.get("plan_counts", {})
                reply = (
                    f"Your department currently has {stats.get('total', 0)} visible plan(s): "
                    f"{stats.get('pending', 0)} pending review, "
                    f"{stats.get('approved', 0)} approved, "
                    f"and {stats.get('returned', 0)} returned for revision."
                )
                citations = [{"label": "Dean Courses", "url": url_for("dean.dean_courses")}]
                actions = [action for action in context.get("allowed_actions", []) if action["action_id"] in {"open_dean_courses", "open_dean_analytics"}]
                return reply, citations, actions
            platform = summary.get("platform", {})
            reply = (
                f"Platform snapshot: {platform.get('plans_total', 0)} total plans, "
                f"{platform.get('plans_pending', 0)} pending plans, "
                f"{platform.get('pending_users', 0)} pending user approvals, "
                f"{platform.get('failed_tasks', 0)} failed tasks, "
                f"and {platform.get('warning_events', 0)} recent warning/error events."
            )
            citations = [{"label": "Admin Analytics", "url": url_for("admin.admin_analytics")}]
            actions = [action for action in context.get("allowed_actions", []) if action["action_id"] in {"open_admin_analytics", "open_admin_operations"}]
            return reply, citations, actions

        # ── Comprehensive teacher/dean Q&A ──
        sys_base = context.get("system_base", {}) or {}
        signatories = context.get("department_signatories", {}) or {}
        profile = context.get("profile", {})
        subjects = context.get("subjects", []) or []
        peers = context.get("peer_teachers", []) or []
        institution = context.get("institution", {}) or {}
        dept = profile.get("department", "")

        if _contains_any(text, ["core values", "graduate attribute", "core competencies", "institutional values"]):
            cv = sys_base.get("core_values", "")
            ga = sys_base.get("graduate_attributes", "")
            parts = []
            if cv and "core value" in text: parts.append(f"Core Values: {cv}")
            if ga and ("graduate" in text or "attribute" in text): parts.append(f"Graduate Attributes: {ga}")
            if not parts:
                parts.append(f"Core Values: {cv}") if cv else parts.append("Core Values are not configured yet.")
                parts.append(f"Graduate Attributes: {ga}") if ga else parts.append("Graduate Attributes are not configured yet.")
            reply = " | ".join(parts)
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["deadline", "due date", "when is it due", "submission date", "last day"]):
            dl = sys_base.get("deadline", "")
            reply = f"The submission deadline on file is: {dl}." if dl else "No submission deadline has been set by the admin yet."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["announcement", "system announcement", "what's new", "any news"]):
            ann = sys_base.get("announcement", "")
            reply = f"System announcement: \"{ann}\"" if ann else "There is no active system announcement right now."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["session", "how long", "timeout", "logged out", "auto logout", "session timeout", "inactive"]):
            sl = sys_base.get("session_lifetime", "60")
            reply = f"Your session times out after {sl} minutes of inactivity."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["other teacher", "who else", "who teaches", "colleague", "fellow", "coworker", "co-teacher", "department teacher", "who is in my department", "show me the teachers", "list teacher"]):
            if peers:
                names = "; ".join(f"{p['name']}" + (f" ({p['title']})" if p.get('title') else "") for p in peers[:10])
                more = f" (+{len(peers)-10} more)" if len(peers) > 10 else ""
                reply = f"Teachers in the {dept or 'your'} department ({len(peers)} colleague(s)): {names}{more}."
            else:
                reply = f"No other teachers found in the {dept or 'your'} department currently."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["where is our school", "school address", "school location", "where is", "located", "campus address", "address of"]):
            reply = f"The institution name on file is \"{institution.get('institution_name','AIPCLPMS')}\". A physical address/location is not stored in the system yet. An admin can add it under Settings."
            citations = [{"label": "Settings", "url": url_for("admin.admin_settings")}] if role == "admin" else [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["school website", "institution website", "university website", "official site", "school site", "web address"]):
            pub_url = all_settings.get("PUBLIC_APP_URL", "") or "https://aipclpms.otakunity.com"
            reply = f"The institution's official LPMS portal is at {pub_url}."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["ai limit", "generation limit", "daily ai", "ai quota", "how many ai", "generations per day", "remaining generation", "quota left"]):
            quota = context.get("ai_quota", {})
            dl = sys_base.get("daily_ai_limit", "5")
            used = quota.get("used_today", 0)
            remaining = max(0, int(dl) - int(used)) if dl and used else "?"
            reply = f"Your daily AI generation limit is {dl}. You've used {used} today, with {remaining} remaining."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["my teaching load", "my units", "total units", "how many units", "teaching load", "how many subjects", "how many courses"]):
            total_units = sum(float(s.get("units", "").split()[0]) for s in subjects if s.get("units","").split()[0].replace('.','').isdigit())
            total_hours = sum(float(s.get("contact_hours", "").split()[0]) for s in subjects if s.get("contact_hours","").split()[0].replace('.','').isdigit())
            reply = f"Your teaching load: {len(subjects)} subject(s), {total_units:.0f} units, {total_hours:.0f} contact hours per week."
            citations = [{"label": "My Subjects", "url": url_for("teacher.manage_subjects")}]
            return reply, citations, actions

        if _contains_any(text, ["what school", "what institution", "what university", "school name", "my school", "my institution"]):
            inst_name = institution.get("institution_name", "") or sys_base.get("school_name", "AIPCLPMS")
            sem = institution.get("active_semester", "")
            ay = institution.get("active_academic_year", "")
            reply = f"You are using LPMS at {inst_name}. Active term: {sem}, AY {ay}."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["allow signup", "can people join", "registration open", "is signup open", "can new users"]):
            signups = sys_base.get("allow_signups", "yes")
            reply = f"New user signups are currently {'open' if signups == 'yes' else 'closed'}." if signups else "Signup status is not configured."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["who is our dean", "who is the dean", "our dean name", "dean of", "dean for"]):
            dname = signatories.get("dean_name", "")
            dtitle = signatories.get("dean_title", "Dean")
            if dname:
                reply = f"The {dtitle} for the {dept or 'your'} department is {dname}."
            else:
                reply = f"No dean is recorded for the {dept or 'your'} department yet."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["who is our vp", "vice president name", "vp name", "the vice president", "who is vp"]):
            vp = signatories.get("vice_president_name", "")
            vp_title = signatories.get("vice_president_title", "Vice President")
            if vp:
                reply = f"The {vp_title} on file for the {dept or 'your'} department is {vp}."
            else:
                reply = f"No Vice President is recorded for the {dept or 'your'} department yet."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["who is our coordinator", "program coordinator name", "coordinator name", "who is coordinator", "program head", "department head", "chairperson"]):
            pc = signatories.get("program_coordinator_name", "")
            pc_title = signatories.get("program_coordinator_title", "Program Coordinator")
            if pc:
                reply = f"The {pc_title} on file for the {dept or 'your'} department is {pc}."
            else:
                reply = f"No program coordinator is recorded for the {dept or 'your'} department yet."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["who is the admin", "contact admin", "system admin", "admin email", "who manages"]):
            admin_email = sys_base.get("admin_alert_email", "") or (all_settings or {}).get("admin_alert_email", "")
            reply = "The system administrator can be reached through the contacts listed in the admin section." if not admin_email else f"The admin alert email on file is {admin_email}."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["ai model", "what model", "which model", "gemini model", "what llm", "what ai model", "ai engine"]):
            model = sys_base.get("gemini_model", "") or (all_settings or {}).get("gemini_model", "gemini-3-flash-preview")
            reply = f"The system is using Google's {model} for AI generation."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["max upload", "file size", "upload limit", "how big", "max file", "file limit"]):
            mb = sys_base.get("max_upload_mb", "") or (all_settings or {}).get("max_upload_mb", "10")
            reply = f"The maximum file upload size is {mb} MB."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["maintenance", "maintenance mode", "is the system down", "system offline", "system maintenance"]):
            mm = (all_settings or {}).get("maintenance_mode", "off")
            reply = f"Maintenance mode is currently {'ON - only admins can log in' if mm == 'on' else 'OFF - the system is fully operational'}."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["what stage", "clp stages", "stages of", "review stages", "plan stages", "beta stages", "workflow stages", "steps to finalize"]):
            reply = "A CLP goes through these stages: 1) Create draft, 2) Generate CLOs, 3) Generate alignment, 4) Generate weekly outline, 5) Validate and mark ready, 6) Insert final data into the DOCX template, 7) Submit to dean for review. The dean can approve or return it for revision."
            citations = [{"label": "My Plans", "url": url_for("teacher.teacher_my_clps")}]
            return reply, citations, actions

        if _contains_any(text, ["my schedule", "class time", "what time", "teaching schedule", "my timetable", "my classes"]):
            if subjects:
                scheds = [f"{s.get('course_code','?')}: {s.get('class_schedule','no schedule')} (Room {s.get('room_assignment','TBA')})" for s in subjects[:8]]
                reply = "Your class schedule: " + "; ".join(scheds) + "."
            else:
                reply = "No class schedules found. Add subjects with schedules under My Subjects."
            citations = [{"label": "My Subjects", "url": url_for("teacher.manage_subjects")}]
            return reply, citations, actions

        if _contains_any(text, ["custom editor", "custom_editor", "can i use the", "editor enabled", "onlyoffice editor", "document editor", "beta editor"]):
            ce = (all_settings or {}).get("custom_editor_enabled", "false")
            reply = f"The custom document editor is currently {'enabled' if ce == 'true' else 'not enabled'}."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["what are sdg", "sdg", "sustainable development", "un sdg", "global goals"]):
            sdgs = (all_settings or {}).get("copilot_sdg_options", "") or "SDG 4, SDG 8, SDG 9, SDG 16, SDG 17"
            reply = f"The system tracks these UN Sustainable Development Goals (SDGs): {sdgs}. These are used in CLP alignment to show how your course contributes to global goals."
            citations = [{"label": "Dashboard", "url": url_for("main.dashboard")}]
            return reply, citations, actions

        if _contains_any(text, ["how do i submit", "submit to dean", "send to dean", "finalize and submit", "ready for submission"]):
            reply = "To submit a plan to the dean: 1) Complete all generation steps (CLOs, alignment, weekly), 2) Click Validate & Mark Ready, 3) Click Submit to Dean. The dean will then receive it for review."
            citations = [{"label": "My Plans", "url": url_for("teacher.teacher_my_clps")}]
            return reply, citations, actions

        reply = f"I'm looking at your current LPMS context for the {role} role. Right now the key summary is: {context.get('context_summary')}."
        citations = docs
        actions = context.get("allowed_actions", [])[:2]
        return reply, citations, actions

    @classmethod
    def _workflow_help_reply(cls, message, context):
        text = _lowered(message)
        role = context.get("role")

        if _contains_any(text, ["clone", "copy plan", "duplicate"]):
            return "To clone a plan: go to All CLPs, find the plan you want, and click Clone. If the plan uses old data, only metadata is copied - you'll need to re-generate content with the AI Copilot."
        if _contains_any(text, ["create a plan", "new plan", "start a plan", "make a plan", "paano", "gumawa", "how do i create", "how do i make", "learning plan", "course plan"]):
            return (
                "Para gumawa ng bagong Course Learning Plan (CLP), sundan ang tatlong hakbang na ito:\n"
                "1. Mag-upload at mag-confirm ng Template Profile — ito ang magiging layout ng iyong CLP. Pumunta sa Template Profiles at i-upload ang iyong DOCX template.\n"
                "2. Magdagdag ng Subject — ikonekta ito sa iyong Template Profile para alam ng AI ang gagamiting format. Pumunta sa My Subjects at pindutin ang Add Subject.\n"
                "3. Pumunta sa AI Copilot — piliin ang subject mo at pindutin ang Generate. Ang AI ang bubuo ng CLOs, alignment matrix, at weekly outline gamit ang template mo.\n"
                "Pagkatapos, i-review mo ang content, i-validate, at i-submit sa Dean.\n\n"
                "To create a new CLP:\n"
                "1. Upload & confirm a Template Profile (your DOCX layout)\n"
                "2. Add a Subject and link it to your template profile\n"
                "3. Go to AI Copilot, pick your subject, and generate"
            )
        if _contains_any(text, ["ai copilot", "ai generation", "how does ai", "copilot work"]):
            return "The AI Copilot uses Gemini to generate CLP content. You pick a subject, provide course metadata (description, outcomes, schedule), and the AI drafts the weekly plan, CLOs, assessments, and references. You can then review, edit, and finalize."
        if _contains_any(text, ["change password", "reset password", "forgot password"]):
            return "LPMS uses your login credentials from the system. To change your password, contact your system admin. There is no self-service password reset in this version."
        if _contains_any(text, ["export", "lms export"]):
            return "To export a plan for LMS, open the plan from My Plans, and use the Export/Download option. The finalized DOCX document can be shared or uploaded to your LMS."
        if _contains_any(text, ["download", "get document", "get my plan file"]):
            return "To download your plan as a DOCX file: go to My Plans, open the plan, and click the Download button. The document uses your department's template with all content filled in."
        if _contains_any(text, ["edit document", "edit docx", "edit the file"]):
            return "To edit your plan document: open the plan from My Plans, click 'Edit Document'. If OnlyOffice is available, you can edit in-browser. Otherwise, download, edit locally, and re-upload."
        if _contains_any(text, ["upload", "upload document", "upload file"]):
            return "To upload a document: go to My Plans, open the relevant plan, and use the upload option to attach or replace the DOCX file."
        if _contains_any(text, ["what is clp", "what is a clp", "clp mean", "clp stand"]):
            return "CLP stands for Course Learning Plan. It's a structured document that outlines weekly topics, learning outcomes, assessments, references, and teaching strategies for a course."
        if _contains_any(text, ["what is lpms", "what does lpms", "lpms stand", "lpms mean"]):
            return "LPMS stands for Learning Plan Management System. It helps teachers create, manage, and submit Course Learning Plans (CLPs), with AI-assisted generation, dean review workflows, and document management."
        if _contains_any(text, ["learning plan", "what is a learning plan"]):
            return "A learning plan (CLP) defines what students will learn each week, the intended outcomes (CLOs), assessments, teaching methods, and references. Teachers create them, deans review and approve them."
        if _contains_any(text, ["manual", "documentation", "user guide", "where is the help", "where is the doc"]):
            return "The built-in guides are available under the Docs section in the sidebar. Click the '?' or 'Docs' link to see role-specific workflow documentation."
        if _contains_any(text, ["submit", "how to submit", "submit to dean"]):
            return "To submit a plan to the dean: go to My Plans, make sure the plan is in draft/draft_review/returned status, then click 'Submit to Dean'. The plan moves to pending review."
        if _contains_any(text, ["version", "plan version", "history", "plan history"]):
            return "Plan versions are saved automatically when key actions occur (generation, submission, approval). View version history from the plan detail page."

        if role == "teacher":
            return (
                "For teacher workflow, the usual path is: register subjects under My Subjects, create a CLP with the AI Copilot, review and edit the generated content, finalize the document, and submit to the dean for approval."
            )
        if role == "dean":
            return (
                "For dean workflow, start from Dean Courses, review pending department plans, open the CLP review page, check the supporting document if needed, then approve or return the plan through the existing review flow."
            )
        return (
            "For admin workflow, use Dashboard for approvals, Operations for incidents and failed tasks, Analytics for overall trends, Templates for document standards, and Audit Logs for activity tracing."
        )

    @classmethod
    def _next_steps_reply(cls, context):
        role = context.get("role")
        if role == "teacher":
            stats = context.get("stats", {})
            profile = context.get("profile", {})
            recommendations = []

            returned = [p for p in context.get("plans", []) if p.get("status") == "returned_for_revision"]
            if returned:
                recommendations.append(f"Fix {len(returned)} returned plan(s) - these have dean feedback that needs addressing.")

            if context.get("feedback_items"):
                recommendations.append(f"Review dean feedback on {len(context['feedback_items'])} plan(s).")

            if context.get("submittable_plans"):
                top = context["submittable_plans"][0]
                recommendations.append(f"Submit \"{top.get('subject')}\" (#{top.get('id')}) to the dean - you have {len(context['submittable_plans'])} plan(s) ready.")

            if not context.get("subjects"):
                recommendations.append("Register your subjects under My Subjects - the AI Copilot needs them to create CLPs.")

            if not profile.get("signature_url"):
                recommendations.append("Upload your e-signature in your Profile - it's required for finalized CLP documents.")

            if not profile.get("consultation_hours"):
                recommendations.append("Set your consultation hours in your Profile page.")

            tasks = context.get("tasks", [])
            failed = [t for t in tasks if t.get("status") == "failed"]
            if failed:
                recommendations.append(f"Check {len(failed)} failed AI generation task(s) and retry if needed.")

            if stats.get("pending", 0) and not recommendations:
                recommendations.append(f"Monitor your {stats['pending']} pending plan(s) - watch for dean approval or returned feedback.")

            if recommendations:
                numbered = " ".join(f"({i+1}) {r}" for i, r in enumerate(recommendations[:5]))
                return f"Here are my recommendations: {numbered}"

            return "Your LPMS workload looks clear right now. All plans are on track and your profile is complete. If you need to create or refine a plan, start from My Plans or the AI CLP creation tools."
        if role == "dean":
            pending = context.get("stats", {}).get("pending", 0)
            if pending:
                first = context.get("pending_plans", [{}])[0]
                return f"Your next priority is reviewing pending department plans. You currently have {pending} pending review(s), starting with {first.get('subject', 'the next visible plan')}."
            return "You have no pending department reviews right now. The next best step is to check analytics or faculty progress for follow-up."
        platform = context.get("stats", {})
        if platform.get("failed_tasks", 0) or platform.get("warning_events", 0):
            return "Your next step should be opening Operations to review failed tasks or warning events before making broader admin changes."
        if platform.get("pending_users", 0):
            return f"Your next step is likely user onboarding: there are {platform.get('pending_users', 0)} pending user approval(s)."
        return "The system looks relatively stable right now. Analytics and templates are the best next places to review."

    @classmethod
    def _workflow_actions(cls, context):
        """Return the 3-step flow actions for creating a CLP."""
        role = context.get("role")
        if role == "teacher":
            return [
                {"action_id": "open_teacher_template_profiles", "label": "Step 1: Template Profiles", "description": "Upload & confirm your DOCX template", "kind": "navigate", "requires_confirmation": False, "payload": {}},
                {"action_id": "open_teacher_subjects", "label": "Step 2: My Subjects", "description": "Add subjects & link template", "kind": "navigate", "requires_confirmation": False, "payload": {}},
                {"action_id": "open_teacher_copilot", "label": "Step 3: AI Copilot", "description": "Generate from linked template", "kind": "navigate", "requires_confirmation": False, "payload": {}},
            ]
        return [action for action in context.get("allowed_actions", []) if action["action_id"] in {"open_dashboard", "open_docs"}]

    @classmethod
    def _general_chat_reply(cls, message, context):
        text = _lowered(message)
        name = context.get("profile", {}).get("first_name") or context.get("profile", {}).get("username") or "there"
        model_name = context.get("assistant_meta", {}).get("model_name") or "your configured Gemini model"
        if _contains_any(text, ["what model", "which model", "who are you", "what are you"]):
            return f"I'm your LPMS AI Buddy, and I'm currently using {model_name}."
        if _contains_any(text, ["what can you do", "what can you help", "your capabilities", "what do you know"]):
            return (
                "I know about your profile, department, institution, subjects, plans, outcomes (PO/CO/IO), "
                "signatories (dean, VP, coordinator), consultation hours, class schedules, room assignments, "
                "AI generation quota, notifications, background tasks, and dean feedback. "
                "I can also navigate to pages or submit eligible plans. Just ask naturally!"
            )
        if _contains_any(text, ["hello", "hi", "hey", "good morning", "good afternoon", "good evening", "kumusta", "magandang umaga", "magandang hapon", "magandang gabi", "musta"]):
            return f"Hi {name}! I'm here and ready to help with LPMS questions, your workflow, or just a quick check on your plans. Ask me anything!"
        if _contains_any(text, ["how are you", "what's up", "hows it going", "how's it going", "kamusta ka"]):
            return f"I'm doing well, thanks! I'm ready to help with anything in LPMS - plans, subjects, signatories, outcomes, schedules, you name it."
        if _contains_any(text, ["thank", "salamat", "thanks"]):
            return f"You're welcome, {name}! Let me know if there's anything else."
        if _contains_any(text, ["bye", "goodbye", "paalam", "see you"]):
            return f"Goodbye, {name}! I'll be here whenever you need help with LPMS."
        return "I'm here with your full LPMS context. Try asking about your plans, subjects, department, signatories, outcomes, schedule, or say 'what can you do' for the full list."

    @classmethod
    def _unsupported_reply(cls, context):
        role = context.get("role")
        return (
            f"I can help with LPMS status questions, workflow guidance, and a small set of low-risk actions for the {role} role, but this beta does not execute destructive or high-risk commands."
        )

    @classmethod
    def _handle_action_request(cls, message, context):
        text = _lowered(message)
        role = context.get("role")
        citations = []
        actions = []

        if "notification" in text and _contains_any(text, ["mark", "read all", "clear"]):
            actions.append(
                {
                    "action_id": "mark_notifications_read_all",
                    "label": "Mark All Notifications Read",
                    "description": "Mark all current notifications as read.",
                    "kind": "execute",
                    "requires_confirmation": True,
                    "payload": {},
                }
            )
            citations.append({"label": "Notifications", "url": url_for("main.list_notifications")})
            return "I can mark all of your notifications as read. Confirm the action below if you want me to do that.", actions, citations

        if role == "teacher":
            if _contains_any(text, ["my plans", "my clps", "drafts"]):
                actions.append(next(action for action in context["allowed_actions"] if action["action_id"] == "open_teacher_my_plans"))
                citations.append({"label": "My Plans", "url": url_for("teacher.teacher_my_clps")})
                return "I can open your My Plans page.", actions, citations
            if _contains_any(text, ["my subjects", "subjects page", "manage subjects"]):
                actions.append(next((a for a in context["allowed_actions"] if a["action_id"] == "open_teacher_subjects"), None) or {
                    "action_id": "open_teacher_subjects", "label": "Open My Subjects",
                    "description": "Manage your subjects for the active semester.",
                    "kind": "navigate", "requires_confirmation": False, "payload": {},
                })
                citations.append({"label": "My Subjects", "url": url_for("teacher.manage_subjects")})
                return "I can open your My Subjects page.", actions, citations
            if _contains_any(text, ["my profile", "profile page", "edit profile"]):
                actions.append({
                    "action_id": "open_teacher_profile", "label": "Open My Profile",
                    "description": "View and edit your profile settings.",
                    "kind": "navigate", "requires_confirmation": False, "payload": {},
                })
                citations.append({"label": "Profile", "url": url_for("teacher.teacher_profile")})
                return "I can open your profile page.", actions, citations
            if _contains_any(text, ["department plans", "all plans", "shared plans"]):
                actions.append(next(action for action in context["allowed_actions"] if action["action_id"] == "open_teacher_department_plans"))
                citations.append({"label": "Department Plans", "url": url_for("teacher.teacher_all_clps")})
                return "I can open the department plans view.", actions, citations
            if "submit" in text and "dean" in text:
                plan_id = _extract_plan_id(text)
                if plan_id is None and len(context.get("submittable_plans", [])) == 1:
                    plan_id = context["submittable_plans"][0]["id"]
                if plan_id is not None:
                    actions.append(
                        {
                            "action_id": "teacher_submit_to_dean",
                            "label": f"Submit Plan {plan_id} To Dean",
                            "description": "Change the plan status to pending dean review.",
                            "kind": "execute",
                            "requires_confirmation": True,
                            "payload": {"plan_id": plan_id},
                        }
                    )
                    citations.append({"label": "My Plans", "url": url_for("teacher.teacher_my_clps")})
                    return f"I can submit plan {plan_id} to the dean if it is still eligible for submission. Confirm below to proceed.", actions, citations
            plan_id = _extract_plan_id(text)
            if plan_id is not None:
                actions.append(
                    {
                        "action_id": "open_plan",
                        "label": f"Open Plan {plan_id}",
                        "description": "Open the plan details page if you are allowed to view it.",
                        "kind": "navigate",
                        "requires_confirmation": False,
                        "payload": {"plan_id": plan_id},
                    }
                )
                citations.append({"label": "Plan View", "url": url_for("teacher.view_clp", plan_id=plan_id)})
                return f"I can try to open plan {plan_id}.", actions, citations

        if role == "dean":
            if _contains_any(text, ["pending", "reviews", "courses"]):
                actions.append(next(action for action in context["allowed_actions"] if action["action_id"] == "open_dean_courses"))
                citations.append({"label": "Dean Courses", "url": url_for("dean.dean_courses")})
                return "I can open the dean courses page for pending and approved department plans.", actions, citations
            if _contains_any(text, ["analytics", "stats"]):
                actions.append(next(action for action in context["allowed_actions"] if action["action_id"] == "open_dean_analytics"))
                citations.append({"label": "Dean Analytics", "url": url_for("dean.dean_analytics")})
                return "I can open dean analytics.", actions, citations
            plan_id = _extract_plan_id(text)
            if plan_id is not None and _contains_any(text, ["review", "open"]):
                actions.append(
                    {
                        "action_id": "dean_open_review",
                        "label": f"Open Review For Plan {plan_id}",
                        "description": "Open the dean review page if the plan belongs to your department.",
                        "kind": "navigate",
                        "requires_confirmation": False,
                        "payload": {"plan_id": plan_id},
                    }
                )
                citations.append({"label": "Dean Review", "url": url_for("dean.dean_courses")})
                return f"I can try to open the review page for plan {plan_id}.", actions, citations

        if role == "admin":
            if _contains_any(text, ["operations", "errors", "incidents"]):
                actions.append(next(action for action in context["allowed_actions"] if action["action_id"] == "open_admin_operations"))
                citations.append({"label": "Operations", "url": url_for("admin.admin_operations")})
                return "I can open the admin operations page.", actions, citations
            if _contains_any(text, ["analytics", "metrics"]):
                actions.append(next(action for action in context["allowed_actions"] if action["action_id"] == "open_admin_analytics"))
                citations.append({"label": "Admin Analytics", "url": url_for("admin.admin_analytics")})
                return "I can open admin analytics.", actions, citations
            if "template" in text:
                actions.append(next(action for action in context["allowed_actions"] if action["action_id"] == "open_admin_templates"))
                citations.append({"label": "Templates", "url": url_for("admin.manage_templates")})
                return "I can open template management.", actions, citations

        actions = [action for action in context.get("allowed_actions", []) if action["action_id"] in {"open_dashboard", "open_docs"}]
        citations = context.get("docs", [])
        return "I can help open relevant LPMS pages or perform a few safe actions, but I need a more specific request.", actions, citations

    @classmethod
    def _build_ai_context_payload(cls, context, fallback_reply, suggested_actions, citations):
        role = context.get("role")
        payload = {
            "role": role,
            "route_hint": context.get("route_hint"),
            "assistant_meta": context.get("assistant_meta", {}),
            "profile": context.get("profile", {}),
            "institution": context.get("institution", {}),
            "department_signatories": context.get("department_signatories", {}),
            "context_summary": context.get("context_summary", {}),
            "docs": context.get("docs", []),
            "citations": citations,
            "fallback_reply": fallback_reply,
            "allowed_actions": [
                {
                    "action_id": action.get("action_id"),
                    "label": action.get("label"),
                    "description": action.get("description"),
                    "kind": action.get("kind"),
                    "requires_confirmation": bool(action.get("requires_confirmation")),
                    "payload": action.get("payload", {}),
                }
                for action in context.get("allowed_actions", [])
            ],
            "suggested_actions": [
                {
                    "action_id": action.get("action_id"),
                    "label": action.get("label"),
                    "description": action.get("description"),
                    "kind": action.get("kind"),
                    "requires_confirmation": bool(action.get("requires_confirmation")),
                    "payload": action.get("payload", {}),
                }
                for action in suggested_actions
            ],
        }

        if role == "teacher":
            payload.update(
                {
                    "stats": context.get("stats", {}),
                    "plans": (context.get("plans") or [])[:12],
                    "tasks": (context.get("tasks") or [])[:8],
                    "notifications": (context.get("notifications") or [])[:8],
                    "feedback_items": (context.get("feedback_items") or [])[:6],
                    "submittable_plans": (context.get("submittable_plans") or [])[:6],
                    "subjects": (context.get("subjects") or [])[:12],
                    "plans_by_subject": (context.get("plans_by_subject") or [])[:12],
                    "faculty_peers": context.get("faculty_peers", 0),
                    "outcomes_summary": context.get("outcomes_summary", {}),
                    "ai_quota": context.get("ai_quota", {}),
                }
            )
        elif role == "dean":
            payload.update(
                {
                    "department": context.get("department"),
                    "stats": context.get("stats", {}),
                    "plans": (context.get("plans") or [])[:12],
                    "pending_plans": (context.get("pending_plans") or [])[:8],
                    "faculty_count": context.get("faculty_count", 0),
                    "notifications": (context.get("notifications") or [])[:8],
                    "plans_by_subject": (context.get("plans_by_subject") or [])[:12],
                }
            )
        else:
            payload.update(
                {
                    "stats": context.get("stats", {}),
                    "plans": (context.get("plans") or [])[:12],
                    "tasks": (context.get("tasks") or [])[:10],
                    "events": (context.get("events") or [])[:10],
                    "notifications": (context.get("notifications") or [])[:8],
                }
            )

        return _json_safe(payload)

    @classmethod
    def _maybe_refine_with_ai(cls, intent, message, context, session_history, fallback_reply, suggested_actions, citations):
        if current_app.config.get("TESTING"):
            return fallback_reply
        if intent == "data_question":
            return fallback_reply
        try:
            model = AIClient.get_model()
            ai_context = cls._build_ai_context_payload(context, fallback_reply, suggested_actions, citations)
            prompt = f"""
You are the AIPCLPMS AI Buddy for a course learning plan system.

Your role:
- answer as a warm, professional LPMS assistant the user can talk to
- use the supplied LPMS context bundle as your source of truth
- help with status, workflow, next steps, and allowed low-risk actions
- talk normally when the user is just chatting, asking who you are, or asking what model you use

Hard rules:
- stay strictly within LPMS scope
- do not invent counts, statuses, records, permissions, or completed actions
- do not expose data outside the provided context bundle
- do not say you executed an action unless it already happened in backend logic
- if the user asks for something unsupported, say so clearly and redirect them to what you can help with
- if data is missing or uncertain, say that directly
- keep the reply under 180 words
- do not output JSON
- do not mention "JSON", "context bundle", or "fallback answer" in the reply
- if the user asks what model you use, answer directly from assistant_meta.model_name
- do not force workflow guidance into a simple greeting or casual question

Intent: {intent}
Role: {context.get('role')}
Route hint: {context.get('route_hint')}
User message: {message}
Recent history: {json.dumps(_json_safe(session_history[-6:]), ensure_ascii=True)}
LPMS context: {json.dumps(ai_context, ensure_ascii=True)}

Write the assistant reply now.
"""
            resp = AIClient.generate_with_retry(
                model,
                [prompt],
                {"temperature": 0.35, "max_output_tokens": 320},
                retries=1,
                delay=1,
                task_type="ai_buddy",
                user_id=context.get("user_id"),
            )
            candidate = (resp.text or "").strip()
            if _looks_incomplete(candidate):
                return fallback_reply
            if intent == "data_question":
                fallback_digits = re.findall(r"\d+", fallback_reply or "")
                candidate_digits = re.findall(r"\d+", candidate or "")
                if fallback_digits and candidate_digits != fallback_digits:
                    return fallback_reply
            return _trim_text(candidate, 800) or fallback_reply
        except Exception:
            return fallback_reply

    @classmethod
    def _ok_redirect(cls, message, redirect_url):
        return {"status": "ok", "message": message, "redirect_url": redirect_url}

    @classmethod
    def _mark_notifications_read(cls, user_id):
        _client().table("notifications").update({"is_read": True}).eq("user_id", user_id).execute()
        invalidate_notifications_cache(user_id)
        log_audit(user_id, "Buddy Marked Notifications Read", {"event_type": "buddy_action", "resource_id": "notifications"})
        log_system_event("buddy", "info", "Buddy executed mark notifications read", user_id=user_id)
        return cls._ok_redirect("Marked all notifications as read.", url_for("main.list_notifications"))

    @classmethod
    def _open_plan(cls, payload, role, user_id):
        plan_id = _safe_int(payload.get("plan_id"), None)
        if plan_id is None:
            raise ValueError("A plan id is required.")
        plan = _client().table("course_learning_plans").select("id, user_id, department, status").eq("id", plan_id).single().execute().data
        if not user_can_access_clp(plan, role=role, user_id=user_id, department=get_current_user_department()):
            raise PermissionError("You are not allowed to open that plan.")
        log_audit(user_id, "Buddy Opened Plan", {"event_type": "buddy_action", "resource_id": str(plan_id)})
        log_system_event("buddy", "info", "Buddy opened plan view", user_id=user_id, plan_id=plan_id)
        return cls._ok_redirect(f"Opened plan {plan_id}.", url_for("teacher.view_clp", plan_id=plan_id))

    @classmethod
    def _dean_open_review(cls, payload, user_id):
        plan_id = _safe_int(payload.get("plan_id"), None)
        if plan_id is None:
            raise ValueError("A plan id is required.")
        plan = _client().table("course_learning_plans").select("id, department, user_id, status").eq("id", plan_id).single().execute().data
        if not user_can_access_clp(plan, role="dean", user_id=user_id, department=get_current_user_department()):
            raise PermissionError("You are not allowed to review that plan.")
        log_audit(user_id, "Buddy Opened Dean Review", {"event_type": "buddy_action", "resource_id": str(plan_id)})
        log_system_event("buddy", "info", "Buddy opened dean review", user_id=user_id, plan_id=plan_id)
        return cls._ok_redirect(f"Opened review for plan {plan_id}.", url_for("dean.dean_review_clp", plan_id=plan_id))

    @classmethod
    def _teacher_submit_to_dean(cls, payload, user_id):
        plan_id = _safe_int(payload.get("plan_id"), None)
        if plan_id is None:
            raise ValueError("A plan id is required.")
        plan = (
            _client().table("course_learning_plans")
            .select("id, user_id, status, subject, upload_type, content, filename")
            .eq("id", plan_id)
            .single()
            .execute()
            .data
        )
        if not plan or plan.get("user_id") != user_id:
            raise PermissionError("You are not allowed to submit that plan.")
        allowed_statuses = {"draft", "draft_review", "returned_for_revision", "beta_ready"}
        can_submit = plan.get("status") in allowed_statuses
        if not can_submit and plan.get("upload_type") == "ai_copilot_beta":
            plan_content = _coerce_json_object(plan.get("content"))
            review_stage = str(plan_content.get("review_stage") or "").strip().lower()
            beta_ready_for_template = bool(plan_content.get("beta_ready_for_template"))
            has_beta_document = bool(plan.get("filename")) and str(plan.get("filename")).lower().endswith(".docx")
            can_submit = review_stage in {"beta_ready", "beta_inserted"} or (beta_ready_for_template and has_beta_document)
        if not can_submit:
            raise ValueError("This plan cannot be submitted right now.")
        _client().table("course_learning_plans").update({"status": "pending"}).eq("id", plan_id).execute()
        from app.utils import create_notification
        create_notification(user_id, f'Buddy submitted "{plan.get("subject")}" to the dean.', reference_type='clp', reference_id=plan_id)
        log_audit(user_id, "Buddy Submitted Plan To Dean", {"event_type": "buddy_action", "resource_id": str(plan_id)})
        log_system_event("buddy", "info", "Buddy submitted plan to dean", user_id=user_id, plan_id=plan_id)
        return cls._ok_redirect(f'Submitted "{plan.get("subject")}" to the dean.', url_for("teacher.teacher_my_clps"))
