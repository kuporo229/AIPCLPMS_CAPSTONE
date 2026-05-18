import json
from flask import current_app
from app import supabase
from app.services.ai_client import AIClient

class ValidatorService:
    @staticmethod
    def validate_clp_outcomes(clp_content):
        """
        Analyzes the Learning Outcomes in the CLP JSON against Bloom's Taxonomy.
        Returns a JSON report with flagged items and suggested corrections.
        """
        try:
            model_instance = AIClient.get_model()
            
            # Extract relevant sections (Weekly LOs, Course Outcomes)
            # We assume clp_content is a dict
            
            prompt = f"""
            You are an expert in Educational Pedagogy and Bloom's Taxonomy.
            Review the following Course Learning Plan (CLP) content for alignment with Bloom's Taxonomy.

            FOCUS AREAS:
            1. **Course Outcomes (CO)**: Are the verbs appropriate for the course level? (e.g., 100-level should be foundational, 400-level should be analytical/creative).
            2. **Weekly Learning Outcomes (LO)**: Do they align with the Topic and Assessment?
            3. **Action Verbs**: Flag vague verbs like "Understand", "Know", "Learn". Suggest measurable alternatives like "Define", "Analyze", "Demonstrate".

            CONTENT TO ANALYZE:
            {json.dumps(clp_content)}

            OUTPUT FORMAT (JSON ONLY):
            {{
                "score": 0,
                "issues": [
                    {{
                        "location": "Week 1 LO",
                        "original_text": "Students will understand the basics...",
                        "issue": "Vague verb 'understand'",
                        "suggestion": "Students will define and describe the basics...",
                        "severity": "high"
                    }}
                ],
                "summary": "Brief summary of the pedagogical quality."
            }}
            """

            validation_schema = {
                "type": "OBJECT",
                "properties": {
                    "score": {"type": "NUMBER"},
                    "issues": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "location": {"type": "STRING"},
                                "original_text": {"type": "STRING"},
                                "issue": {"type": "STRING"},
                                "suggestion": {"type": "STRING"},
                                "severity": {"type": "STRING", "enum": ["high", "medium", "low"]}
                            },
                            "required": ["location", "issue", "suggestion", "severity"]
                        }
                    },
                    "summary": {"type": "STRING"}
                },
                "required": ["score", "issues", "summary"]
            }

            response = AIClient.generate_with_retry(
                model_instance,
                [prompt],
                {
                    "response_mime_type": "application/json",
                    "response_schema": validation_schema
                },
                task_type="validation"
            )
            
            try:
                # Use centralized cleaning logic
                cleaned_json = AIClient.clean_ai_json(response.text)
                return json.loads(cleaned_json)
            except Exception as e:
                current_app.logger.error(f"Validator JSON Parse Error: {e}")
                return {"score": 0, "issues": [], "summary": "Failed to parse validation report."}

        except Exception as e:
            current_app.logger.error(f"Validator Service Error: {e}")
            return {"score": 0, "issues": [], "summary": "Validation service unavailable."}
