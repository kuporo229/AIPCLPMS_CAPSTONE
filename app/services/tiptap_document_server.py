from urllib.parse import quote

import requests
from flask import current_app


class TiptapDocumentServerError(RuntimeError):
    pass


def is_tiptap_document_server_configured():
    return bool(
        current_app.config.get("TIPTAP_DOCUMENT_SERVER_ID")
        and current_app.config.get("TIPTAP_DOCUMENT_SERVER_API_SECRET")
        and current_app.config.get("TIPTAP_DOCUMENT_SERVER_BASE_URL")
    )


def _base_url():
    return current_app.config["TIPTAP_DOCUMENT_SERVER_BASE_URL"].rstrip("/")


def _headers(content_type=None):
    headers = {"Authorization": current_app.config["TIPTAP_DOCUMENT_SERVER_API_SECRET"]}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def document_identifier(document_id):
    return f"clp-document-{document_id}"


def _document_url(identifier, *, format_json=False):
    url = f"{_base_url()}/api/documents/{quote(identifier, safe='')}"
    if format_json:
        url = f"{url}?format=json"
    return url


def _raise_for_document_error(response, allowed_statuses=()):
    if response.ok or response.status_code in allowed_statuses:
        return
    raise TiptapDocumentServerError(
        f"TipTap document server failed ({response.status_code}): {response.text[:800]}"
    )


def upsert_tiptap_document(identifier, tiptap_json):
    if not is_tiptap_document_server_configured():
        return {"ok": False, "skipped": True}

    create_response = requests.post(
        _document_url(identifier, format_json=True),
        headers=_headers("application/json"),
        json=tiptap_json,
        timeout=60,
    )
    if create_response.status_code == 409:
        delete_response = requests.delete(
            _document_url(identifier),
            headers=_headers(),
            timeout=60,
        )
        _raise_for_document_error(delete_response, allowed_statuses=(404,))
        create_response = requests.post(
            _document_url(identifier, format_json=True),
            headers=_headers("application/json"),
            json=tiptap_json,
            timeout=60,
        )

    _raise_for_document_error(create_response, allowed_statuses=(204,))
    return {"ok": True, "identifier": identifier}


def fetch_tiptap_document(identifier):
    response = requests.get(
        _document_url(identifier, format_json=True),
        headers=_headers(),
        timeout=60,
    )
    _raise_for_document_error(response)
    return response.json()
