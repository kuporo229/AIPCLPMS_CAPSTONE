import json
import os
import httpx

try:
    from google import genai
    from google.genai import types
except ImportError:  # pragma: no cover
    genai = None

    class _FallbackThinkingLevel:
        MINIMAL = "minimal"
        LOW = "low"
        MEDIUM = "medium"
        HIGH = "high"

    class _FallbackThinkingConfig:
        def __init__(self, thinking_level=None):
            self.thinking_level = thinking_level

    class _FallbackGenerateContentConfig(dict):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

    class _FallbackTypes:
        ThinkingLevel = _FallbackThinkingLevel
        ThinkingConfig = _FallbackThinkingConfig
        GenerateContentConfig = _FallbackGenerateContentConfig

    types = _FallbackTypes()
from flask import current_app
import time

from app.services.observability import StructuredLogger, log_ai_usage


_INVALID_GEMINI_KEYS = {
    "test",
    "test-gemini",
    "server-managed",
    "changeme",
    "change-me",
    "placeholder",
    "none",
    "null",
}


def is_valid_gemini_api_key(value):
    key = str(value or "").strip()
    if not key:
        return False
    lowered = key.lower()
    if lowered in _INVALID_GEMINI_KEYS:
        return False
    if lowered.startswith("test-") or lowered.endswith("-placeholder"):
        return False
    return True


def _is_gemini_auth_error(error_str):
    lowered = str(error_str or "").lower()
    auth_markers = (
        "api key not valid",
        "invalid api key",
        "invalid api_key",
        "permission_denied",
        "unauthenticated",
        "authentication",
        "unauthorized",
        "401",
        "403",
    )
    return any(marker in lowered for marker in auth_markers)


def _is_gemini_model_fallback_error(error_str):
    lowered = str(error_str or "").lower()
    return (
        ("not found" in lowered and ("model" in lowered or "models/" in lowered))
        or ("unsupported" in lowered and "model" in lowered)
        or ("not supported" in lowered and "model" in lowered)
    )


class AIClient:
    """Centralized wrapper for all Google Gemini interactions."""

    _client = None
    _client_api_key = None
    _deepseek_client = None

    @classmethod
    def _ensure_client(cls):
        """Ensures the Gemini SDK client is configured."""
        if genai is None:
            raise ImportError("google.genai is not installed in this environment.")
        api_key = str(current_app.config.get("GEMINI_API_KEY") or "").strip()
        if not is_valid_gemini_api_key(api_key):
            cls._client = None
            cls._client_api_key = None
            StructuredLogger.error("GEMINI_API_KEY is missing or uses a placeholder value.")
            raise ValueError("GEMINI_API_KEY is missing or uses a placeholder value.")
        if cls._client is None or cls._client_api_key != api_key:
            cls._client = genai.Client(api_key=api_key)
            cls._client_api_key = api_key
        return cls._client

    @classmethod
    def get_provider(cls):
        """Returns the configured AI provider (google or deepseek)."""
        from app.utils import get_system_prompt
        from app import supabase
        return get_system_prompt(supabase, "ai_provider", "google") or "google"

    @classmethod
    def get_model(cls, model_name=None):
        """Returns the configured model name."""
        if cls.get_provider() == "deepseek":
            # DeepSeek doesn't need Gemini client init
            if not model_name:
                from app.utils import get_system_prompt
                from app import supabase
                model_name = get_system_prompt(supabase, "gemini_model", "deepseek-chat")
            return model_name
        cls._ensure_client()
        if not model_name:
            from app.utils import get_system_prompt
            from app import supabase
            model_name = get_system_prompt(supabase, "gemini_model", "gemini-3-flash-preview")
        return model_name

    @classmethod
    def _get_deepseek_api_key(cls):
        """Returns the DeepSeek API key from env or config."""
        key = os.environ.get("DEEPSEEK_API_KEY") or current_app.config.get("DEEPSEEK_API_KEY", "")
        if not key:
            raise ValueError("DEEPSEEK_API_KEY not configured")
        return key

    @classmethod
    def _generate_deepseek(cls, model, contents, config, on_chunk=None):
        """Generate content via DeepSeek API (OpenAI-compatible)."""
        api_key = cls._get_deepseek_api_key()
        prompt = contents[0] if isinstance(contents, list) else contents

        messages = [{"role": "user", "content": prompt}]

        max_tok = 16384
        if isinstance(config, dict):
            for key in ("max_tokens", "max_output_tokens", "maxTokens"):
                val = config.get(key)
                if val and isinstance(val, (int, float)):
                    max_tok = int(val)
                    break
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tok,
            "stream": True,
        }

        if isinstance(config, dict):
            mime = config.get("response_mime_type", "")
            if mime == "application/json":
                body["response_format"] = {"type": "json_object"}

        collected = []
        with httpx.Client(timeout=120) as client:
            with client.stream("POST", "https://api.deepseek.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("data: "):
                        chunk_data = line[6:].strip()
                        if chunk_data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(chunk_data)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                collected.append(content)
                                if callable(on_chunk):
                                    on_chunk(content)
                        except Exception:
                            pass

        full_text = "".join(collected)

        class DeepSeekResponse:
            def __init__(self, text):
                self.text = text
        return DeepSeekResponse(full_text)

    @staticmethod
    def _normalize_model_name(model):
        if isinstance(model, str):
            return model
        return getattr(model, "model_name", None) or str(model)

    @staticmethod
    def _get_thinking_level_enum():
        from app.utils import get_system_prompt
        from app import supabase

        level_value = (get_system_prompt(supabase, "gemini_thinking_level", "minimal") or "minimal").strip().lower()
        mapping = {
            "minimal": types.ThinkingLevel.MINIMAL,
            "low": types.ThinkingLevel.LOW,
            "medium": types.ThinkingLevel.MEDIUM,
            "high": types.ThinkingLevel.HIGH,
        }
        return mapping.get(level_value)

    @classmethod
    def _build_generate_config(cls, model_name, config):
        from app.utils import get_system_prompt
        from app import supabase

        config_dict = dict(config or {})

        if "temperature" not in config_dict:
            try:
                config_dict["temperature"] = float(get_system_prompt(supabase, "ai_temperature", "0.7"))
            except Exception:
                pass

        if model_name.startswith("gemini-3"):
            thinking_level = cls._get_thinking_level_enum()
            if thinking_level is not None:
                config_dict["thinking_config"] = types.ThinkingConfig(thinking_level=thinking_level)

        return types.GenerateContentConfig(**config_dict)

    @staticmethod
    def generate_with_retry(model, contents, config, retries=3, delay=1, task_type="general", plan_id=None, user_id=None, on_chunk=None):
        """Generates content with retry logic, model fallback, and usage logging."""
        current_model = AIClient._normalize_model_name(model)
        start_time = time.time()
        last_error = None

        provider = AIClient.get_provider()

        if provider == "deepseek":
            for attempt in range(retries):
                try:
                    resp = AIClient._generate_deepseek(current_model, contents, config, on_chunk=on_chunk)
                    if not resp or not getattr(resp, "text", None):
                        raise Exception("AI returned an empty response.")
                    duration_ms = int((time.time() - start_time) * 1000)
                    from app import supabase as global_supabase
                    log_ai_usage(global_supabase, plan_id, user_id, task_type, current_model, duration_ms, "success")
                    StructuredLogger.info(f"AI Generation Success: {task_type}", model=current_model, duration_ms=duration_ms, plan_id=plan_id)
                    return resp
                except Exception as e:
                    last_error = str(e)
                    if attempt < retries - 1:
                        time.sleep(delay * (attempt + 1))
                        continue
                    duration_ms = int((time.time() - start_time) * 1000)
                    from app import supabase as global_supabase
                    log_ai_usage(global_supabase, plan_id, user_id, task_type, current_model, duration_ms, "error", str(e))
                    StructuredLogger.error(f"AI Generation Failed: {task_type}", error=str(e))
                    raise Exception(f"AI generation failed after {retries} attempts: {e}")
            return  # never reached

        # ── Gemini path ──
        client = AIClient._ensure_client()
        for attempt in range(retries):
            try:
                generation_config = AIClient._build_generate_config(current_model, config)
                if callable(on_chunk) and hasattr(client.models, "generate_content_stream"):
                    try:
                        collected = []
                        for chunk in client.models.generate_content_stream(
                            model=current_model,
                            contents=contents,
                            config=generation_config,
                        ):
                            text = getattr(chunk, "text", "") or ""
                            if text:
                                collected.append(text)
                                on_chunk(text)

                        class StreamingResponse:
                            def __init__(self, text):
                                self.text = text

                        resp = StreamingResponse("".join(collected))
                    except (AttributeError, TypeError) as stream_exc:
                        StructuredLogger.warning(
                            f"AI streaming unavailable for {task_type}; using standard generation.",
                            error=str(stream_exc),
                        )
                        resp = client.models.generate_content(
                            model=current_model,
                            contents=contents,
                            config=generation_config,
                        )
                        if resp and getattr(resp, "text", None):
                            on_chunk(resp.text)
                else:
                    resp = client.models.generate_content(
                        model=current_model,
                        contents=contents,
                        config=generation_config,
                    )
                if not resp or not getattr(resp, "text", None):
                    raise Exception("AI returned an empty response.")

                duration_ms = int((time.time() - start_time) * 1000)

                from app import supabase as global_supabase

                log_ai_usage(global_supabase, plan_id, user_id, task_type, current_model, duration_ms, "success")

                StructuredLogger.info(
                    f"AI Generation Success: {task_type}",
                    model=current_model,
                    duration_ms=duration_ms,
                    plan_id=plan_id,
                )
                return resp
            except Exception as e:
                last_error = str(e)
                error_str = str(e)

                if _is_gemini_auth_error(error_str):
                    duration_ms = int((time.time() - start_time) * 1000)
                    from app import supabase as global_supabase

                    log_ai_usage(global_supabase, plan_id, user_id, task_type, current_model, duration_ms, "error", "Gemini Configuration Error")
                    StructuredLogger.error(
                        "Gemini API configuration error.",
                        task=task_type,
                        plan_id=plan_id,
                    )
                    raise ValueError("Gemini API key is invalid or unauthorized. Check GEMINI_API_KEY.") from e

                if _is_gemini_model_fallback_error(error_str) and current_model != "gemini-3-flash-preview":
                    fallback_model = "gemini-3-flash-preview"
                    StructuredLogger.warning(f"Model {current_model} not found. Falling back to {fallback_model}.")
                    current_model = fallback_model
                    continue

                if "thinking" in error_str.lower() and current_model.startswith("gemini-3"):
                    try:
                        retry_config = dict(config or {})
                        retry_config.pop("thinking_config", None)
                        generation_config = types.GenerateContentConfig(**retry_config)
                        resp = client.models.generate_content(
                            model=current_model,
                            contents=contents,
                            config=generation_config,
                        )
                        if not resp or not getattr(resp, "text", None):
                            raise Exception("AI returned an empty response.")
                        if callable(on_chunk):
                            on_chunk(resp.text)

                        duration_ms = int((time.time() - start_time) * 1000)
                        from app import supabase as global_supabase

                        log_ai_usage(global_supabase, plan_id, user_id, task_type, current_model, duration_ms, "success")
                        StructuredLogger.warning(
                            "AI Generation succeeded after retrying without thinking configuration.",
                            model=current_model,
                            duration_ms=duration_ms,
                            plan_id=plan_id,
                        )
                        return resp
                    except Exception as retry_exc:
                        last_error = str(retry_exc)
                        error_str = str(retry_exc)

                if "429" in error_str or "quota exceeded" in error_str.lower():
                    duration_ms = int((time.time() - start_time) * 1000)
                    from app import supabase as global_supabase

                    log_ai_usage(global_supabase, plan_id, user_id, task_type, current_model, duration_ms, "error", "Quota Exceeded")
                    raise Exception("Daily AI Quota Exceeded. Please try again later.")

                if attempt == retries - 1:
                    duration_ms = int((time.time() - start_time) * 1000)
                    from app import supabase as global_supabase

                    log_ai_usage(global_supabase, plan_id, user_id, task_type, current_model, duration_ms, "error", last_error)
                    StructuredLogger.error(
                        f"AI Generation failed after {retries} attempts: {last_error}",
                        task=task_type,
                        plan_id=plan_id,
                    )
                    raise

                time.sleep(delay)
                delay *= 1.5

    @staticmethod
    def clean_ai_json(text):
        """Cleans markdown formatting and common malformations from AI JSON responses."""
        if not text:
            return "{}"

        cleaned = text.strip()

        import re

        fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.DOTALL | re.IGNORECASE)
        if fence_match:
            cleaned = fence_match.group(1).strip()

        def _extract_first_json_block(value):
            start = None
            stack = []
            in_string = False
            escape = False
            for idx, ch in enumerate(value):
                if start is None:
                    if ch in "{[":
                        start = idx
                        stack = [ch]
                    continue
                if in_string:
                    if escape:
                        escape = False
                    elif ch == "\\":
                        escape = True
                    elif ch == '"':
                        in_string = False
                    continue
                if ch == '"':
                    in_string = True
                elif ch in "{[":
                    stack.append(ch)
                elif ch in "]}":
                    if not stack:
                        continue
                    opening = stack.pop()
                    if (opening == "{" and ch != "}") or (opening == "[" and ch != "]"):
                        return None
                    if not stack:
                        return value[start : idx + 1]
            return None

        extracted = _extract_first_json_block(cleaned)
        if extracted:
            cleaned = extracted.strip()

        cleaned = re.sub(r",\s*([\]}])", r"\1", cleaned)

        return cleaned.strip() or "{}"

    @classmethod
    def get_embedding(cls, text, task_type="retrieval_document", plan_id=None, user_id=None):
        """Generates embeddings using Gemini's embedding model with logging."""
        start_time = time.time()
        try:
            client = cls._ensure_client()
            result = client.models.embed_content(
                model="gemini-embedding-001",
                contents=text,
            )

            duration_ms = int((time.time() - start_time) * 1000)
            from app import supabase as global_supabase

            log_ai_usage(global_supabase, plan_id, user_id, f"embedding:{task_type}", "gemini-embedding-001", duration_ms, "success")

            embeddings = getattr(result, "embeddings", None) or []
            if embeddings and getattr(embeddings[0], "values", None):
                return embeddings[0].values
            return []
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            from app import supabase as global_supabase

            log_ai_usage(global_supabase, plan_id, user_id, f"embedding:{task_type}", "gemini-embedding-001", duration_ms, "error", str(e))
            StructuredLogger.error(f"Embedding Error: {e}")
            return []
