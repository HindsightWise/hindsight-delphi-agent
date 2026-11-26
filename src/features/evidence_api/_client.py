"""Strict OpenAPI 3.1.0 client for https://delphi.fyi/v1 Evidence API."""

from __future__ import annotations

import json
import mimetypes
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://delphi.fyi/v1"
DEFAULT_USER_AGENT = "Hindsight-Delphi/1.0 (DoraHacks-2479)"
MAX_TOTAL_ITEMS = 3
MAX_NOTE_CHARS = 8000
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_IDEMPOTENCY_KEY_LEN = 128
_MARKET_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_UNSAFE_FILENAME_RE = re.compile(r'["\\\r\n]')


class EvidenceApiError(RuntimeError):
    """Structured error surfaced by Delphi Evidence API v1."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        code: str = "client_error",
        request_id: str | None = None,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.request_id = request_id
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    agent_id: str = "hindsight-delphi"
    agent_harness: str = "antigravity-vertical-slice"
    agent_model: str = "gemini-4-neon-medium"

    def __post_init__(self) -> None:
        validate_ascii_header("X-Agent-Id", self.agent_id)
        validate_ascii_header("X-Agent-Harness", self.agent_harness)
        validate_ascii_header("X-Agent-Model", self.agent_model)


@dataclass(frozen=True, slots=True)
class MultipartEvidenceRequest:
    market_address: str
    note: str | None = None
    links: tuple[str, ...] = ()
    files: tuple[Path, ...] = ()
    idempotency_key: str = ""


def validate_ascii_header(header_name: str, value: str, *, max_len: int = 64) -> str:
    cleaned = str(value).strip()
    if not (1 <= len(cleaned) <= max_len):
        raise ValueError(f"{header_name} must be 1..{max_len} printable ASCII characters, got len={len(cleaned)}")
    if any(ord(ch) < 32 or ord(ch) > 126 for ch in cleaned):
        raise ValueError(f"{header_name} contains non-printable ASCII characters")
    return cleaned


def build_evidence_headers(
    api_key: str,
    identity: AgentIdentity,
    *,
    idempotency_key: str | None = None,
    content_type: str | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
) -> dict[str, str]:
    """Build exact Delphi Evidence API v1 HTTP headers."""
    key = str(api_key).strip()
    if not key:
        raise ValueError("DELPHI_API_ACCESS_KEY (Mainnet) must be non-empty")
    ua = str(user_agent).strip()
    if not ua or "python-urllib" in ua.lower():
        raise ValueError("User-Agent must be non-empty and not Python-urllib (blocked by Cloudflare 1010)")

    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": ua,
        "X-Agent-Id": validate_ascii_header("X-Agent-Id", identity.agent_id),
        "X-Agent-Harness": validate_ascii_header("X-Agent-Harness", identity.agent_harness),
        "X-Agent-Model": validate_ascii_header("X-Agent-Model", identity.agent_model),
    }
    if idempotency_key is not None:
        headers["Idempotency-Key"] = validate_ascii_header(
            "Idempotency-Key", idempotency_key, max_len=MAX_IDEMPOTENCY_KEY_LEN
        )
    if content_type is not None:
        headers["Content-Type"] = content_type
    return headers


def validate_submission_items(
    *,
    market_address: str,
    note: str | None,
    links: tuple[str, ...],
    files: tuple[Path, ...],
) -> int:
    """Enforce Delphi v1 intake limits: <=3 total items across note + links[] + files."""
    if not _MARKET_ADDR_RE.fullmatch(market_address.strip()):
        raise ValueError(f"Invalid EVM marketAddress: {market_address!r}")

    has_note = bool(note and note.strip())
    if has_note and len(note or "") > MAX_NOTE_CHARS:
        raise ValueError(f"Note length {len(note or '')} exceeds {MAX_NOTE_CHARS} characters")

    for url in links:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(f"Evidence links must be valid https:// URLs, got {url!r}")

    total_bytes = 0
    for fpath in files:
        if not fpath.is_file():
            raise ValueError(f"Attachment file does not exist: {fpath}")
        total_bytes += fpath.stat().st_size
    if total_bytes > MAX_FILE_BYTES:
        raise ValueError(f"Total file bytes {total_bytes} exceeds {MAX_FILE_BYTES}")

    total_items = (1 if has_note else 0) + len(links) + len(files)
    if total_items < 1:
        raise ValueError("Submission must contain at least 1 item (note, links[], or files)")
    if total_items > MAX_TOTAL_ITEMS:
        raise ValueError(
            f"Submission contains {total_items} items (note={int(has_note)}, links={len(links)}, files={len(files)}); "
            f"Delphi Evidence API v1 enforces at most {MAX_TOTAL_ITEMS} items total."
        )
    return total_items


def encode_multipart_submission(
    req: MultipartEvidenceRequest,
    *,
    boundary: str | None = None,
) -> tuple[bytes, str]:
    """Encode a validated submission into RFC 7578 multipart/form-data matching OpenAPI 3.1.0."""
    validate_submission_items(
        market_address=req.market_address,
        note=req.note,
        links=req.links,
        files=req.files,
    )
    bound = boundary or f"----HindsightDelphi{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    def _add_text_field(name: str, value: str) -> None:
        chunks.append(f"--{bound}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        chunks.append(value.encode("utf-8"))
        chunks.append(b"\r\n")

    _add_text_field("marketAddress", req.market_address.strip())
    if req.note and req.note.strip():
        _add_text_field("note", req.note)
    for url in req.links:
        _add_text_field("links[]", url)
    for fpath in req.files:
        ctype = mimetypes.guess_type(fpath.name)[0] or "application/octet-stream"
        safe_name = _UNSAFE_FILENAME_RE.sub("_", fpath.name)
        chunks.append(f"--{bound}\r\n".encode("utf-8"))
        chunks.append(
            f'Content-Disposition: form-data; name="files"; filename="{safe_name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n".encode("utf-8")
        )
        chunks.append(fpath.read_bytes())
        chunks.append(b"\r\n")

    chunks.append(f"--{bound}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={bound}"


class EvidenceApiClient:
    """HTTP client for https://delphi.fyi/v1 endpoints."""

    def __init__(
        self,
        api_key: str,
        *,
        identity: AgentIdentity | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 20.0,
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError(f"Only https:// base_url is allowed: {base_url!r}")
        self._api_key = api_key.strip()
        if not self._api_key:
            raise ValueError("api_key must be non-empty")
        self._identity = identity or AgentIdentity()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        data: bytes | None = None,
        content_type: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        headers = build_evidence_headers(
            self._api_key,
            self._identity,
            idempotency_key=idempotency_key,
            content_type=content_type,
        )
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw_bytes = resp.read(MAX_RESPONSE_BYTES + 1)
                if len(raw_bytes) > MAX_RESPONSE_BYTES:
                    raise EvidenceApiError(
                        f"Response from {method} {path} exceeded {MAX_RESPONSE_BYTES} byte safety cap",
                        status_code=resp.status,
                        code="response_too_large",
                    )
                raw_body = raw_bytes.decode("utf-8")
                parsed = json.loads(raw_body) if raw_body.strip() else {}
                return {
                    "status_code": resp.status,
                    "data": parsed,
                    "warnings": resp.headers.get("X-Delphi-Warnings"),
                    "rate_limit_remaining": resp.headers.get("RateLimit-Remaining"),
                    "rate_limit_reset": resp.headers.get("RateLimit-Reset"),
                    "location": resp.headers.get("Location"),
                }
        except urllib.error.HTTPError as exc:
            err_raw = exc.read().decode("utf-8", errors="replace")
            err_code = "http_error"
            req_id = None
            retryable = exc.code in (429, 500, 502, 503, 504)
            retry_after = None
            try:
                err_json = json.loads(err_raw)
                err_obj = err_json.get("error") if isinstance(err_json, dict) else None
                if isinstance(err_obj, dict):
                    err_code = str(err_obj.get("code") or err_code)
                    retryable = bool(err_obj.get("retryable", retryable))
                    retry_after = err_obj.get("retryAfterSeconds")
                req_id = err_json.get("requestId") if isinstance(err_json, dict) else None
            except json.JSONDecodeError:
                pass
            raise EvidenceApiError(
                f"HTTP {exc.code} {err_code} on {method} {path}: {err_raw[:300]}",
                status_code=exc.code,
                code=err_code,
                request_id=req_id,
                retryable=retryable,
                retry_after_seconds=retry_after,
            ) from exc

    def get_limits(self) -> dict[str, Any]:
        return self._request_json("GET", "/limits")

    def get_terms(self) -> dict[str, Any]:
        return self._request_json("GET", "/terms")

    def accept_terms(self, terms_version: str) -> dict[str, Any]:
        body = json.dumps({"termsVersion": terms_version}).encode("utf-8")
        return self._request_json("POST", "/terms/acceptance", data=body, content_type="application/json")

    def get_entry_state(self, market_address: str) -> dict[str, Any]:
        if not _MARKET_ADDR_RE.fullmatch(market_address.strip()):
            raise ValueError(f"Invalid market_address: {market_address!r}")
        return self._request_json("GET", f"/markets/{market_address.strip()}/entry-state")

    def list_submissions(self) -> dict[str, Any]:
        return self._request_json("GET", "/submissions")

    def get_submission(self, submission_id: str) -> dict[str, Any]:
        safe_id = urllib.parse.quote(submission_id.strip(), safe="")
        return self._request_json("GET", f"/submissions/{safe_id}")

    def retry_submission(self, submission_id: str) -> dict[str, Any]:
        safe_id = urllib.parse.quote(submission_id.strip(), safe="")
        return self._request_json("POST", f"/submissions/{safe_id}/retry")

    def submit_evidence(self, req: MultipartEvidenceRequest) -> dict[str, Any]:
        body, content_type = encode_multipart_submission(req)
        idem = req.idempotency_key.strip() or f"delphi-ev-{uuid.uuid4().hex[:24]}"
        return self._request_json(
            "POST",
            "/submissions",
            data=body,
            content_type=content_type,
            idempotency_key=idem,
        )
