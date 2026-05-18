import json
import time
from io import BytesIO

import jwt
import requests
from flask import current_app


class TiptapConversionError(RuntimeError):
    pass


def is_tiptap_conversion_configured():
    return bool(
        current_app.config.get("TIPTAP_CONVERT_APP_ID")
        and current_app.config.get("TIPTAP_CONVERT_SECRET")
    )


def _base_url():
    return current_app.config.get("TIPTAP_CONVERT_BASE_URL", "https://api.tiptap.dev/v2/convert").rstrip("/")


def _token():
    now = int(time.time())
    payload = {
        "iat": now,
        "exp": now + 300,
        "appId": current_app.config["TIPTAP_CONVERT_APP_ID"],
    }
    return jwt.encode(payload, current_app.config["TIPTAP_CONVERT_SECRET"], algorithm="HS256")


def _headers(content_type=None):
    headers = {
        "Authorization": f"Bearer {_token()}",
        "X-App-Id": current_app.config["TIPTAP_CONVERT_APP_ID"],
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _raise_for_conversion_error(response):
    if response.ok:
        return
    detail = response.text[:800]
    try:
        payload = response.json()
        detail = payload.get("message") or payload.get("error") or json.dumps(payload)
    except ValueError:
        pass
    raise TiptapConversionError(f"TipTap conversion failed ({response.status_code}): {detail}")


def _collect_logs(payload):
    logs = payload.get("logs") if isinstance(payload, dict) else None
    warnings = []
    if isinstance(logs, dict):
        for level in ("warn", "error"):
            for item in logs.get(level) or []:
                if isinstance(item, dict):
                    message = item.get("message") or json.dumps(item)
                else:
                    message = str(item)
                message = message.strip()
                if not message or message == "No text content found in run":
                    continue
                if message not in warnings:
                    warnings.append(message)
    return warnings[:20]


SUPPORTED_NODES = {
    "doc",
    "text",
    "paragraph",
    "heading",
    "blockquote",
    "bulletList",
    "orderedList",
    "listItem",
    "codeBlock",
    "hardBreak",
    "horizontalRule",
    "table",
    "tableRow",
    "tableCell",
    "tableHeader",
    "image",
}

SUPPORTED_MARKS = {"bold", "italic", "underline", "strike", "code", "link", "highlight"}


def _editor_safe_node(node):
    if not isinstance(node, dict):
        return None

    node_type = node.get("type")
    content = [
        child
        for child in (_editor_safe_node(child) for child in node.get("content", []) or [])
        if child
    ]

    if node_type == "text":
        safe = {"type": "text", "text": str(node.get("text", ""))}
        marks = [
            mark
            for mark in node.get("marks", []) or []
            if isinstance(mark, dict) and mark.get("type") in SUPPORTED_MARKS
        ]
        if marks:
            safe["marks"] = marks
        return safe

    if node_type not in SUPPORTED_NODES:
        if content:
            return {"type": "paragraph", "content": content}
        text = node.get("text")
        if text:
            return {"type": "paragraph", "content": [{"type": "text", "text": str(text)}]}
        return None

    safe = {"type": node_type}
    attrs = node.get("attrs")
    if isinstance(attrs, dict) and attrs:
        safe["attrs"] = attrs
    if content:
        safe["content"] = content
    return safe


def _editor_safe_doc(doc):
    safe = _editor_safe_node(doc)
    if not safe or safe.get("type") != "doc":
        return {"type": "doc", "content": [{"type": "paragraph"}]}
    safe.setdefault("content", [{"type": "paragraph"}])
    return safe


def import_docx_to_tiptap(upload_stream, filename="document.docx"):
    response = requests.post(
        f"{_base_url()}/import/docx",
        headers=_headers(),
        files={"file": (filename, upload_stream, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        data={"verbose": "6"},
        timeout=120,
    )
    _raise_for_conversion_error(response)

    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    content = data.get("content") if isinstance(data, dict) else None
    if not isinstance(content, dict) or content.get("type") != "doc":
        raise TiptapConversionError("TipTap import did not return a valid document.")

    return {
        "doc": _editor_safe_doc(content),
        "warnings": _collect_logs(payload),
        "extras": {
            key: value
            for key, value in (data or {}).items()
            if key != "content" and value is not None
        },
    }


def export_tiptap_to_docx_bytes(tiptap_doc):
    response = requests.post(
        f"{_base_url()}/export/docx",
        headers=_headers("application/json"),
        json={"doc": json.dumps(tiptap_doc), "exportType": "blob"},
        timeout=120,
    )
    _raise_for_conversion_error(response)
    buffer = BytesIO(response.content)
    buffer.seek(0)
    return buffer


def export_tiptap_to_pdf_bytes(tiptap_doc):
    response = requests.post(
        f"{_base_url()}/export/pdf",
        headers=_headers("application/json"),
        json={"doc": json.dumps(tiptap_doc)},
        timeout=120,
    )
    _raise_for_conversion_error(response)
    buffer = BytesIO(response.content)
    buffer.seek(0)
    return buffer
