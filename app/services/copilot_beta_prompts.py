"""
Prompt templates and constants for the AI Copilot Beta workflow.

Extracted from copilot_beta_service.py for separation of concerns.
All prompt strings, option lists, and configuration constants live here.
"""

from __future__ import annotations


SGA_OPTIONS = [
    "Transformative Leaders",
    "Reconcilers",
    "Industry Competent",
    "Research-Oriented",
    "Information and Communication Technology Proficient",
    "Critical Thinkers",
    "Holistic Persons",
]

CORE_VALUE_OPTIONS = [
    "Faith",
    "Reconciliation",
    "Integrity",
    "Excellence",
    "Solidarity",
]

PQF_LEVEL_6_OPTIONS = [
    "PQF1", "PQF2", "PQF3", "PQF4", "PQF5", "PQF6",
    "PQF7", "PQF8", "PQF9", "PQF10", "PQF11", "PQF12", "PQF13",
]

AQRF_LEVEL_6_OPTIONS = [
    "AQRF1", "AQRF2", "AQRF3", "AQRF4", "AQRF5",
    "AQRF6", "AQRF7", "AQRF8", "AQRF9",
]

SDG_OPTIONS = [
    "SDG 4", "SDG 8", "SDG 9", "SDG 16", "SDG 17",
]

WEEKLY_PROGRESS_STOPWORDS = {
    "about", "aligned", "analysis", "applied", "apply", "assessment", "based", "building",
    "case", "check", "class", "clms", "competencies", "competency", "concept", "concepts",
    "content", "course", "coverage", "demonstrate", "development", "discussion", "drill",
    "during", "education", "example", "examples", "exercise", "feedback", "focus", "focused",
    "foundation", "guided", "guide", "identify", "improvement", "instructor", "integrative",
    "introduction", "knowledge", "laboratory", "lecture", "lesson", "materials", "method",
    "methods", "module", "modules", "mock", "notes", "other", "output", "outputs", "overview",
    "packet", "pages", "performance", "practice", "preparation", "problem", "problems",
    "professional", "progression", "quiz", "readiness", "recitation", "reference", "references",
    "reflection", "resource", "resources", "review", "reviewer", "session", "sessions",
    "short", "slides", "specific", "students", "study", "submission", "support", "synthesis",
    "task", "tasks", "teaching", "technology", "test", "tests", "theme", "topic", "topics",
    "training", "understand", "week", "weekly", "worksheet", "workshop",
}

PRE_EXAM_REVIEW_TOKENS = {
    "review", "reviewer", "synthesis", "coverage", "consultation", "mock", "readiness",
    "drill", "recap", "practice", "preparation", "integrative", "integration",
    "exam", "quiz", "test", "worksheet", "guide", "packet", "checklist", "recall",
    "consolidation", "comprehensive", "remediation", "benchmark", "simulation",
    "oral", "practical", "mastery", "recitation", "debrief",
}

SDG_CONTEXT = {
    "SDG 4": {
        "title": "Quality Education",
        "guidance": "Use when the course strengthens education quality, learning access, digital literacy, inclusive learning, or teaching improvement.",
    },
    "SDG 8": {
        "title": "Decent Work and Economic Growth",
        "guidance": "Use when the course supports employability, industry readiness, productivity, entrepreneurship, workplace skills, or ethical professional practice.",
    },
    "SDG 9": {
        "title": "Industry, Innovation and Infrastructure",
        "guidance": "Use when the course supports technology innovation, digital systems, engineering practice, infrastructure, prototyping, or modern industry solutions.",
    },
    "SDG 16": {
        "title": "Peace, Justice and Strong Institutions",
        "guidance": "Use when the course emphasizes ethics, accountability, governance, security, public information systems, fairness, or institutional trust.",
    },
    "SDG 17": {
        "title": "Partnerships for the Goals",
        "guidance": "Use when the course emphasizes collaboration, community partnership, interdisciplinary coordination, or shared development work.",
    },
}

COPILOT_PROGRAM_OUTCOMES_KEY = "copilot_program_outcomes"
COPILOT_GRADUATE_ATTRIBUTES_KEY = "copilot_graduate_attributes"
COPILOT_CORE_VALUES_KEY = "copilot_core_values"
COPILOT_PQF_OPTIONS_KEY = "copilot_pqf_level_6_options"
COPILOT_AQRF_OPTIONS_KEY = "copilot_aqrf_level_6_options"
COPILOT_SDG_OPTIONS_KEY = "copilot_sdg_options"
COPILOT_SDG_CONTEXT_KEY = "copilot_sdg_context"
COPILOT_DEFAULT_TEMPLATE_NAME_KEY = "copilot_default_template_name"
COPILOT_DEFAULT_TEMPLATE_FILENAME_KEY = "copilot_default_template_filename"

WEEK_ROW_LABELS = [
    "Week 1", "Week 2", "Week 3", "Week 4", "Week 5", "Week 6", "Week 7",
    "Week 8 & 9", "Week 10 & 11", "Week 12", "Week 13", "Week 14 & 15",
    "Week 16-17", "Week 18",
]

DEFAULT_CLO_ROWS = [
    ("CLO 1", "cognitive"),
    ("CLO 2", "cognitive"),
    ("CLO 3", "cognitive"),
    ("CLO 4", "affective"),
    ("CLO 5", "affective"),
    ("CLO 6", "psychomotor"),
    ("CLO 7", "psychomotor"),
    ("CLO 8", "psychomotor"),
]

ALIGNMENT_MIN_ITEMS = 1
ALIGNMENT_PREFERRED_MIN_ITEMS = 2
ALIGNMENT_MAX_ITEMS = 3

_STAGE_ORDER = {
    "metadata": 0,
    "clo_generated": 1,
    "alignment_generated": 2,
    "weekly_generated": 3,
    "beta_ready": 4,
    "beta_inserted": 5,
}


def advance_stage(content, target_stage):
    """Set review_stage to *target_stage* only if it advances; never regress."""
    current = content.get("review_stage", "metadata")
    if _STAGE_ORDER.get(current, 0) < _STAGE_ORDER.get(target_stage, 0):
        content["review_stage"] = target_stage


BETA_CLO_DEFAULT_PROMPT = """
You are generating Course Learning Outcomes for a beta CLP workflow.
Return JSON only. Do not include markdown fences, prose, notes, or explanations.

Use the dynamic CLO row plan supplied later in the prompt. That plan is authoritative for the exact row count, CLO codes, ordering, domains, and any template-specific grouping.

Rules:
- Each row must contain `clo_code`, `domain`, and `clo_statement`
- `domain` must be exactly one of: `cognitive`, `affective`, `psychomotor`
- Write measurable, faculty-ready statements aligned with the course title, course description, service learning component, and optional source context
- Do not invent extra CLO codes
- Do not omit any CLO row
- Do not return extra top-level keys

Return this exact JSON shape:
{
  "clo_alignment_table": [
    { "clo_code": "CLO 1", "domain": "cognitive", "clo_statement": "" }
  ]
}
""".strip()

BETA_ALIGNMENT_DEFAULT_PROMPT = """
You are generating CLO alignment data for a beta CLP workflow.
Return JSON only. Do not include markdown fences, prose, notes, or explanations.

Map each existing CLO to the approved lists only.

Rules:
- Return exactly one alignment row per CLO already provided by the dynamic template/profile shape
- Never invent codes or labels outside the approved lists
- For every CLO row, each array should usually contain 2 to 3 items, and may contain exactly 1 item only when there is truly just one academically defensible approved match for that CLO:
  - `aligned_plos`
  - `graduate_attributes`
  - `core_values`
  - `pqf_level_6_alignment`
  - `aqrf_level_6_alignment`
  - `relevant_sdgs`
- Choose the academically strongest matches only
- Do not force a second or third item if it would be weak, generic, or misleading; however, when two or more approved matches are genuinely supported by the CLO statement, include at least 2
- Determine `target_sdgs_display` from the course title, course description, service learning component, and approved SDG guidance
- `target_sdgs_display` must contain 1 to 3 SDG codes
- Return arrays for all list fields
- Do not return strings where arrays are required
- Do not assign the exact same `relevant_sdgs` array to every CLO unless the CLO statements are truly identical
- Do not reuse an identical `pqf_level_6_alignment` array across all cognitive CLOs, all affective CLOs, or all psychomotor CLOs
- Do not reuse an identical `aqrf_level_6_alignment` array across all cognitive CLOs, all affective CLOs, or all psychomotor CLOs
- Within each domain, vary PQF/AQRF selections according to the CLO's actual progression, complexity, and performance level
- Use the CLO statement itself, not only the domain label, when choosing SDGs, PQF, and AQRF alignments
- Do not return extra top-level keys

Return this exact JSON shape:
{
  "target_sdgs_display": ["SDG 4", "SDG 9"],
  "clo_alignment_table": [
    {
      "clo_code": "CLO 1",
      "aligned_plos": [],
      "graduate_attributes": [],
      "core_values": [],
      "pqf_level_6_alignment": [],
      "aqrf_level_6_alignment": [],
      "relevant_sdgs": []
    }
  ]
}
""".strip()

BETA_ALIGNMENT_CHECKMARK_PROMPT = """
You are generating CLO alignment data for a beta CLP workflow using a CHECKMARK MATRIX.
Return JSON only. Do not include markdown fences, prose, notes, or explanations.

For each CLO, you must decide which Program Outcomes (POs) are supported.

Rules:
- Return exactly one alignment row per CLO already provided by the dynamic template/profile shape
- Use ONLY the PO codes provided in the 'AVAILABLE PO CODES' list
- Check every PO that the CLO genuinely supports, even if the connection is partial or indirect
- Do not artificially limit the number of checked POs per CLO to a small fixed number
- Determine `target_sdgs_display` from the course title, course description, service learning component, and approved SDG guidance
- `target_sdgs_display` must contain 1 to 3 SDG codes
- Do not return extra top-level keys

Return this exact JSON shape:
{
  "target_sdgs_display": ["SDG 4", "SDG 9"],
  "alignment_matrix": [
    {
      "clo_code": "CLO 1",
      "checked_pos": ["BPED 1", "BPED 3", "BPED 5"]
    }
  ]
}
""".strip()

BETA_ALIGNMENT_REPAIR_PROMPT = """
You are repairing CLO alignment data for a beta CLP workflow.
Return JSON only. Do not include markdown fences, prose, notes, or explanations.

Your job:
- revise the existing CLO alignment rows so they remain academically plausible and specific to each CLO statement
- keep all selections inside the approved lists only
- remove repetitive patterning where different CLOs were given the same alignment set without real justification
- vary aligned PLOs, graduate attributes, core values, PQF, AQRF, and SDGs when the CLO intent, complexity, or performance differs
- do not invent extra CLO rows
- do not omit any CLO row
- keep alignment arrays academically selective, but use at least 2 items whenever the CLO clearly supports two or more approved matches
- keep exactly 1 item only when adding a second approved match would be unjustified by the CLO statement

Return this exact JSON shape:
{
  "target_sdgs_display": ["SDG 4", "SDG 9"],
  "clo_alignment_table": [
    {
      "clo_code": "CLO 1",
      "aligned_plos": [],
      "graduate_attributes": [],
      "core_values": [],
      "pqf_level_6_alignment": [],
      "aqrf_level_6_alignment": [],
      "relevant_sdgs": []
    }
  ]
}
""".strip()

BETA_WEEKLY_DEFAULT_PROMPT = """
You are generating the weekly course outline for a beta CLP workflow.
Return JSON only with one top-level key: `weekly_course_outline`. No markdown, prose, or extra keys.

STRUCTURE: Use the dynamic weekly row plan supplied later in the prompt. That plan is authoritative for the exact row count, labels, ordering, and writable template rows.
Never omit, invent, or collapse labels. Self-check: array length and labels match the supplied dynamic weekly row plan exactly.

REQUIRED FIELDS PER ROW:
- `time_frame_label`, `mapped_clos` (array of exact CLO codes), `topics` (3-5 items), `assessment` (2-4 items)
- `intended_learning_outcomes`: { `lead_in`, `cognitive` (>=2), `affective` (>=2), `psychomotor` (>=2) }
- `teaching_learning_activities`: { `lecture` (2-4), `practical_session` (1-3), `other` }
- `learning_resources`: { `clms`, `textbook`, `website`, `journal`, `other` }

CONTENT QUALITY RULES:
- Every week fully populated — no empty arrays, no placeholder/generic/repeated filler
- All wording must be faculty-ready, measurable, course-specific, and aligned with mapped CLOs
- Build each week: pick concrete resources first -> derive topics -> TLAs -> assessments from those resources
- Topics, activities, and assessments must be traceable to the week's listed resources
- Consecutive weeks must not reuse the same topic/resource cluster unless clearly deepening the material
- Do not mention week labels/numbers inside content fields; temporal info belongs only in `time_frame_label`
- Psychomotor outcomes must describe specific applied performances, not generic placeholders
- Do not reference CLO codes in narrative fields unless they match the row's `mapped_clos`

RESOURCE RULES:
- `clms`: specific CLMS materials, modules, handouts, tasks, or submission points
- `textbook`: real citations (author, year, title, publisher) differentiated by chapter/section when reused
- `website`: real URLs from official/professional sources
- `journal`: real scholarly references with citation details
- `other`: software, tools, labs, videos, datasets as appropriate
- Resources must be week-specific; never copy-paste boilerplate across rows

TIME-FRAME RULES:
- Use the supplied dynamic labels exactly
- ALL weeks except strictly orientation ones MUST have at least 1-2 mapped_clos

NON-NEGOTIABLE COUNT RULES — (no-template-profile fallback) violation invalidates the output:
- Every weekly row MUST contain at least 2 items in `cognitive`, `affective`, AND `psychomotor`
- Every weekly row MUST contain at least 3 `topics`
- Every weekly row MUST contain at least 2 `lecture` items and at least 1 `practical_session`
- Do not skip any requirement above — partial arrays are treated as failures

Return this exact JSON shape:
{"weekly_course_outline":[{"time_frame_label":"Week 1","mapped_clos":["CLO 1"],"intended_learning_outcomes":{"lead_in":"At the end of the week, students should have the ability to:","cognitive":["Define X","Explain Y"],"affective":["Value Z","Appreciate W"],"psychomotor":["Execute A","Perform B"]},"topics":["Introduction to X","Basic Concepts of Y"],"teaching_learning_activities":{"lecture":["Interactive Discussion","PowerPoint Presentation"],"practical_session":["Laboratory Exercise 1"],"other":["Online Quiz"]},"assessment":["Formative Assessment 1","Laboratory Report"],"learning_resources":{"clms":["Module 1"],"textbook":["Author, 2024, Title, Publisher"],"website":["https://example.com/resource"],"journal":["Author, Title, Journal Name"],"other":["Software Tool"]}}]}
""".strip()

BETA_FINAL_REVIEW_PROMPT = """
You are the final academic QA reviewer for a beta CLP workflow before template rendering.
Return JSON only. Do not include markdown fences, prose, notes, or explanations outside the JSON object.

Check whether the draft is internally aligned and ready to be inserted into the official CLP template.

Audit requirements:
- Metadata must be academically consistent with the CLOs, alignment matrix, and weekly outline
- CLO statements must align with the course title, description, and service-learning component
- Alignment rows must be plausible and consistent with each CLO statement
- Weekly rows must progress logically and remain aligned with mapped CLOs
- Weekly rows after the first/orientation row must show a real course progression
- Topics, outcomes, activities, assessments, and resources must support each other
- Flag vague, contradictory, duplicated, or obviously mismatched content
- Flag incorrect or suspicious alignment choices
- Approve only if the draft is strong enough to render into the final template without major correction

Return this exact JSON shape:
{
  "approved": true,
  "issues": [],
  "warnings": [],
  "summary": ""
}
""".strip()

BETA_FINAL_FIX_PROMPT = """
You are repairing a beta CLP draft after a final academic QA review flagged alignment and content issues.
Return JSON only. Do not include markdown fences, notes, or explanations outside the JSON object.

Your job:
- Fix the draft so it is internally coherent and closer to final-template quality
- Replace obvious placeholder metadata with academically plausible values
- Correct CLO domain mismatches
- Correct alignment rows so they fit the CLO statements
- Rewrite weekly rows so `mapped_clos`, learning outcomes, topics, activities, assessments, and resources all align
- Remove repetitive or contradictory weekly content
- Keep the course intent, subject, and overall direction intact

Return this exact JSON shape:
{
  "metadata": {},
  "clo_alignment_table": [],
  "weekly_course_outline": [],
  "notes": []
}
""".strip()
