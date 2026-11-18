"""A deliberately restricted Canvas REST API client.

This client is the ONLY code that talks to Canvas. It enforces, in one place:
  * HTTPS only, to a single Canvas host chosen at connect time.
  * Three separate modes, each with its own allow-list:
      send    - GET + POST to create an announcement, an assignment, or a file
                (file uploads must ask Canvas to rename, never overwrite)
      grading - GET + exactly four weighting edits (see GRADING_RULES)
      publish - GET + exactly two edits: an assignment's or a file's
                published/unpublished state (see PUBLISH_RULES)
    PATCH and DELETE can never be sent in any mode.
  * File bytes go to the upload server Canvas names, but only if it is a
    known Canvas storage host, and never with the API token.
  * Redirects are never followed (a redirected POST could land somewhere else).
  * Pagination links are only followed if they point at the same host, so the
    API token is never sent to another server.
  * The token is never placed in a URL and never shown in repr()/logs.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterable

USER_AGENT = "CanvasCommandCenter/1.0 (local teacher tool)"

# ---- "send" mode (default): read + create announcements/assignments only.
ALLOWED_METHODS = frozenset({"GET", "POST"})
ALLOWED_POST_PATHS = (
    re.compile(r"^/api/v1/courses/(\d+)/discussion_topics$"),
    re.compile(r"^/api/v1/courses/(\d+)/assignments$"),
)

# ---- "grading" mode: read + exactly four weighting edits, nothing else.
# Each rule pins the method, the path AND the exact form fields allowed, and
# validates every value. Anything that could touch scores, submissions,
# points, dates or content is impossible to send in this mode.
_WEIGHT = re.compile(r"^(?:\d{1,4})(?:\.\d{1,4})?$")
_ID = re.compile(r"^\d{1,15}$")
GRADING_RULES = (
    # turn weighted grading on/off for a course
    ("PUT", re.compile(r"^/api/v1/courses/(\d+)$"),
     {"course[apply_assignment_group_weights]": lambda v: v in ("true", "false")}),
    # create an assignment group (name + weight)
    ("POST", re.compile(r"^/api/v1/courses/(\d+)/assignment_groups$"),
     {"name": lambda v: 0 < len(v.strip()) <= 255 and v.isprintable(),
      "group_weight": lambda v: bool(_WEIGHT.match(v)) and float(v) <= 1000}),
    # change an assignment group's weight
    ("PUT", re.compile(r"^/api/v1/courses/(\d+)/assignment_groups/(\d+)$"),
     {"group_weight": lambda v: bool(_WEIGHT.match(v)) and float(v) <= 1000}),
    # move an assignment into another group
    ("PUT", re.compile(r"^/api/v1/courses/(\d+)/assignments/(\d+)$"),
     {"assignment[assignment_group_id]": lambda v: bool(_ID.match(v))}),
)


_BOOL = lambda v: v in ("true", "false")

# ---- "publish" mode: read + flip published state of ONE assignment or file.
PUBLISH_RULES = (
    ("PUT", re.compile(r"^/api/v1/courses/(\d+)/assignments/(\d+)$"), {"assignment[published]": _BOOL}),
    ("PUT", re.compile(r"^/api/v1/files/(\d+)$"), {"locked": _BOOL}),
)

# ---- file upload "preflight" (send mode): asks Canvas where to upload.
MAX_UPLOAD_BYTES = int(float(os.environ.get("CCC_MAX_UPLOAD_MB", "250")) * 1024 * 1024)
UPLOAD_PREFLIGHT_PATH = re.compile(r"^/api/v1/courses/(\d+)/files$")
_CONTENT_TYPE = re.compile(r"^[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+$")


def valid_file_name(v: str) -> bool:
    return (0 < len(v) <= 255 and v.isprintable() and v.strip() == v
            and "/" not in v and "\\" not in v and v not in (".", ".."))


def validate_folder_path(path: str) -> str:
    """'Week 12 / Handouts/' -> 'Week 12/Handouts'.  '' means the top folder."""
    parts = [p.strip() for p in (path or "").replace("\\", "/").split("/")]
    parts = [p for p in parts if p]
    if len(parts) > 10:
        raise ValueError("Folder path is too deep (max 10 levels).")
    for p in parts:
        if p in (".", "..") or not p.isprintable() or len(p) > 100:
            raise ValueError(f"Invalid folder name: {p!r}")
    result = "/".join(parts)
    if len(result) > 255:
        raise ValueError("Folder path is too long.")
    return result


UPLOAD_FIELDS = {
    "name": valid_file_name,
    "size": lambda v: v.isdigit() and 0 <= int(v) <= MAX_UPLOAD_BYTES,
    "content_type": lambda v: bool(_CONTENT_TYPE.match(v)),
    "on_duplicate": lambda v: v == "rename",          # never "overwrite"
}
def _clean_folder(v: str) -> bool:
    try:
        return validate_folder_path(v) == v and v != ""
    except ValueError:
        return False


UPLOAD_OPTIONAL = {"parent_folder_path": _clean_folder}


def check_rules(rules, method: str, path: str, form: list, label: str) -> None:
    """Raise SafetyViolation unless (method, path, form) matches one rule exactly."""
    for m, pattern, fields in rules:
        if m == method and pattern.match(path):
            keys = [k for k, _ in form]
            if sorted(keys) != sorted(fields) or len(set(keys)) != len(keys):
                raise SafetyViolation(f"{method} {path}: fields {keys} are not exactly {sorted(fields)}")
            for k, v in form:
                if not isinstance(v, str) or not fields[k](v):
                    raise SafetyViolation(f"{method} {path}: invalid value for {k}: {v!r}")
            return
    raise SafetyViolation(f"{method} {path} is not on the {label} allow-list")


def check_grading_request(method: str, path: str, form: list) -> None:
    check_rules(GRADING_RULES, method, path, form, "grading")


def check_publish_request(method: str, path: str, form: list) -> None:
    check_rules(PUBLISH_RULES, method, path, form, "publish")


def check_upload_preflight(form: list) -> None:
    keys = [k for k, _ in form]
    if len(set(keys)) != len(keys):
        raise SafetyViolation("Upload request has a repeated field")
    missing = set(UPLOAD_FIELDS) - set(keys)
    extra = set(keys) - set(UPLOAD_FIELDS) - set(UPLOAD_OPTIONAL)
    if missing or extra:
        raise SafetyViolation(f"Upload request fields wrong (missing {sorted(missing)}, extra {sorted(extra)})")
    for k, v in form:
        check = UPLOAD_FIELDS.get(k) or UPLOAD_OPTIONAL[k]
        if not isinstance(v, str) or not check(v):
            raise SafetyViolation(f"Upload request: invalid value for {k}: {v!r}")


_S3_HOST = re.compile(r"^instructure-uploads(?:-[a-z0-9]+)*\.s3(?:[.-][a-z0-9-]+)*\.amazonaws\.com$")


def upload_host_allowed(url: str, canvas_base: str) -> bool:
    """Canvas stores files on its own storage service. Only known hosts are allowed."""
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    if port not in (None, 443):
        return f"https://{parsed.hostname}:{port}" == canvas_base
    host = parsed.hostname.lower()
    extra = {h.strip().lower() for h in os.environ.get("CCC_EXTRA_UPLOAD_HOSTS", "").split(",") if h.strip()}
    return (f"https://{host}" == canvas_base or host in extra
            or host.endswith(".instructure.com") or host.endswith(".inscloudgate.net")
            or bool(_S3_HOST.match(host)))


def multipart_body(params: list, filename: str, content_type: str, data: bytes) -> tuple[bytes, str]:
    """multipart/form-data with the file LAST (Canvas requires this)."""
    boundary = "----ccc" + secrets.token_hex(16)
    chunks = []
    for k, v in params:
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{_q(k)}"\r\n\r\n{v}\r\n'.encode())
    chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{_q(filename)}"\r\n'
                  f"Content-Type: {content_type}\r\n\r\n".encode())
    chunks.append(data)
    chunks.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _q(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ")
_SAFE_PATH = re.compile(r"^/api/v1/[A-Za-z0-9_\-/]*$")


class CanvasError(Exception):
    """A request failed.

    ``ambiguous`` is True when a POST may or may not have been applied by Canvas
    (timeouts, dropped connections, 5xx). Ambiguous failures are never retried.
    """

    def __init__(self, message: str, status: int | None = None, ambiguous: bool = False):
        super().__init__(message)
        self.status = status
        self.ambiguous = ambiguous


class SafetyViolation(Exception):
    """The code attempted something the safety rules forbid. Nothing was sent."""


@dataclass
class Response:
    status: int
    headers: dict = field(default_factory=dict)
    body: bytes = b""


Transport = Callable[[str, str, dict, "bytes | None"], Response]


def validate_base_url(url: str) -> str:
    """Return a normalised ``https://host[:port]`` or raise ValueError."""
    url = (url or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise ValueError("Canvas URL must start with https://")
    if not parsed.hostname:
        raise ValueError("Canvas URL is missing a host name")
    if parsed.username or parsed.password:
        raise ValueError("Canvas URL must not contain a username or password")
    if parsed.path not in ("",) or parsed.query or parsed.fragment:
        raise ValueError("Enter only the Canvas address, e.g. https://school.instructure.com")
    host = parsed.hostname.lower()
    return f"https://{host}" + (f":{parsed.port}" if parsed.port else "")


def validate_token(token: str) -> str:
    token = (token or "").strip()
    if not token:
        raise ValueError("API token is empty")
    if any(c.isspace() for c in token) or not token.isprintable():
        raise ValueError("API token contains invalid characters")
    return token


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # urllib then raises HTTPError with the 3xx status


class UrllibTransport:
    """Real network transport (standard library only, TLS verified)."""

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        ctx = ssl.create_default_context()
        self._opener = urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPSHandler(context=ctx)
        )

    def __call__(self, method, url, headers, body):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return Response(resp.status, dict(resp.headers.items()), resp.read())
        except urllib.error.HTTPError as e:
            return Response(e.code, dict(e.headers.items()) if e.headers else {}, e.read() or b"")


def _parse_next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        m = re.match(r'\s*<([^>]+)>\s*;\s*rel="?([^";]+)"?', part)
        if m and m.group(2) == "next":
            return m.group(1)
    return None


class SafeCanvasClient:
    def __init__(self, base_url: str, token: str, transport: Transport | None = None,
                 mode: str = "send"):
        if mode not in ("send", "grading", "publish"):
            raise ValueError("mode must be 'send', 'grading' or 'publish'")
        self.base_url = validate_base_url(base_url)
        self._token = validate_token(token)
        self._transport = transport or UrllibTransport()
        self.mode = mode
        self.posts_sent = 0
        self.writes_sent = 0

    def __repr__(self):
        return f"SafeCanvasClient(base_url={self.base_url!r}, mode={self.mode!r}, token='***redacted***')"

    # ------------------------------------------------------------------ core
    def _url(self, path_or_url: str, params: Iterable | None = None) -> str:
        if path_or_url.startswith("https://") or path_or_url.startswith("http://"):
            parsed = urllib.parse.urlsplit(path_or_url)
            origin = f"{parsed.scheme}://{parsed.netloc.lower()}"
            if origin != self.base_url:
                raise SafetyViolation(f"Refusing to contact a different host: {origin}")
            path, query = parsed.path, parsed.query
        else:
            path, query = path_or_url, ""
        if ".." in path or not _SAFE_PATH.match(path):
            raise SafetyViolation(f"Refusing unexpected API path: {path!r}")
        if params:
            extra = urllib.parse.urlencode(list(params), doseq=True)
            query = f"{query}&{extra}" if query else extra
        return f"{self.base_url}{path}" + (f"?{query}" if query else "")

    def _request(self, method: str, path_or_url: str, params=None, form=None):
        method = method.upper()
        allowed = ALLOWED_METHODS | ({"PUT"} if self.mode in ("grading", "publish") else set())
        if method not in allowed:
            raise SafetyViolation(f"HTTP {method} is not allowed in {self.mode} mode")
        url = self._url(path_or_url, params)
        body = None
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if method != "GET":
            path = urllib.parse.urlsplit(url).path
            form = list(form or [])
            if self.mode == "send":
                if UPLOAD_PREFLIGHT_PATH.match(path):
                    check_upload_preflight(form)
                elif not any(p.match(path) for p in ALLOWED_POST_PATHS):
                    raise SafetyViolation(f"POST to {path} is not on the allow-list")
            elif self.mode == "grading":
                check_grading_request(method, path, form)
            else:
                check_publish_request(method, path, form)
            if urllib.parse.urlsplit(url).query:
                raise SafetyViolation("Write requests may not carry query parameters")
            body = urllib.parse.urlencode(list(form or []), doseq=True).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            resp = self._transport(method, url, headers, body)
        except Exception as e:  # network error, timeout, TLS error ...
            raise CanvasError(
                f"Network error during {method}: {type(e).__name__}: {e}",
                ambiguous=(method != "GET"),
            ) from None
        if method == "POST":
            self.posts_sent += 1
        if method != "GET":
            self.writes_sent += 1
        if not 200 <= resp.status < 300:
            raise CanvasError(
                _error_message(resp),
                status=resp.status,
                ambiguous=(method != "GET" and (resp.status >= 500 or resp.status == 408)),
            )
        try:
            data = json.loads(resp.body.decode("utf-8") or "null")
        except ValueError:
            raise CanvasError("Canvas returned a non-JSON response", status=resp.status,
                              ambiguous=(method != "GET")) from None
        return data, resp

    def _get(self, path, params=None):
        return self._request("GET", path, params=params)[0]

    def _get_all(self, path, params=None, max_pages: int = 50) -> list:
        params = list(params or []) + [("per_page", "100")]
        items: list = []
        data, resp = self._request("GET", path, params=params)
        for _ in range(max_pages):
            if not isinstance(data, list):
                raise CanvasError("Expected a list from Canvas")
            items.extend(data)
            nxt = _parse_next_link(_header(resp.headers, "Link"))
            if not nxt:
                return items
            data, resp = self._request("GET", nxt)  # _url() checks same host
        raise CanvasError(f"Too many pages (> {max_pages}) for {path}")

    # ----------------------------------------------------------- read (GET)
    def whoami(self) -> dict:
        return self._get("/api/v1/users/self")

    def list_teacher_courses(self) -> list:
        return self._get_all("/api/v1/courses", [
            ("enrollment_type", "teacher"),
            ("state[]", "available"), ("state[]", "unpublished"),
        ])

    def list_announcements(self, course_id: int) -> list:
        return self._get_all(f"/api/v1/courses/{int(course_id)}/discussion_topics",
                             [("only_announcements", "true")])

    def list_assignments(self, course_id: int) -> list:
        return self._get_all(f"/api/v1/courses/{int(course_id)}/assignments")

    def get_announcement(self, course_id: int, topic_id: int) -> dict:
        return self._get(f"/api/v1/courses/{int(course_id)}/discussion_topics/{int(topic_id)}")

    def get_assignment(self, course_id: int, assignment_id: int) -> dict:
        return self._get(f"/api/v1/courses/{int(course_id)}/assignments/{int(assignment_id)}")

    # --------------------------------------------------- grading reads
    def get_course(self, course_id: int) -> dict:
        return self._get(f"/api/v1/courses/{int(course_id)}")

    def list_assignment_groups(self, course_id: int) -> list:
        return self._get_all(f"/api/v1/courses/{int(course_id)}/assignment_groups")

    def list_submissions(self, course_id: int) -> list:
        """Every student's submission for every assignment (read-only)."""
        return self._get_all(f"/api/v1/courses/{int(course_id)}/students/submissions",
                             [("student_ids[]", "all")], max_pages=500)

    # ----------------------------------------------------- file & item reads
    def list_course_files(self, course_id: int) -> list:
        return self._get_all(f"/api/v1/courses/{int(course_id)}/files", max_pages=200)

    def list_folders(self, course_id: int) -> list:
        return self._get_all(f"/api/v1/courses/{int(course_id)}/folders", max_pages=100)

    def list_folder_files(self, folder_id: int) -> list:
        return self._get_all(f"/api/v1/folders/{int(folder_id)}/files", max_pages=100)

    def get_file(self, file_id: int) -> dict:
        return self._get(f"/api/v1/files/{int(file_id)}")

    def list_assignment_submissions(self, course_id: int, assignment_id: int) -> list:
        return self._get_all(f"/api/v1/courses/{int(course_id)}/assignments/{int(assignment_id)}/submissions",
                             max_pages=200)

    # ------------------------------------------- file upload (send mode only)
    def upload_preflight(self, course_id: int, name: str, size: int, content_type: str,
                         folder_path: str = "") -> dict:
        form = [("name", name), ("size", str(int(size))), ("content_type", content_type),
                ("on_duplicate", "rename")]
        if folder_path:
            form.append(("parent_folder_path", folder_path))
        return self._request("POST", f"/api/v1/courses/{int(course_id)}/files", form=form)[0]

    def upload_file_data(self, upload_url: str, upload_params, filename: str, content_type: str,
                         data: bytes) -> Response:
        """Step 2 of a Canvas upload: send the bytes to the storage server Canvas named.
        No API token is sent. Redirects are returned, not followed. Never retried."""
        if self.mode != "send":
            raise SafetyViolation(f"File upload is not allowed in {self.mode} mode")
        if not upload_host_allowed(upload_url, self.base_url):
            host = urllib.parse.urlsplit(upload_url).hostname
            raise SafetyViolation(f"Canvas asked to upload to an unrecognised server ({host}). Refused. "
                                  "If your school hosts its own Canvas storage, add that host to "
                                  "CCC_EXTRA_UPLOAD_HOSTS.")
        if len(data) > MAX_UPLOAD_BYTES:
            raise SafetyViolation("File is larger than the upload limit")
        params = list(upload_params.items()) if isinstance(upload_params, dict) else list(upload_params)
        if not all(isinstance(k, str) and isinstance(v, (str, int)) for k, v in params):
            raise CanvasError("Canvas returned unexpected upload parameters")
        body, ctype = multipart_body([(k, str(v)) for k, v in params], filename, content_type, data)
        headers = {"Content-Type": ctype, "Accept": "application/json", "User-Agent": USER_AGENT}
        try:
            resp = self._transport("POST", upload_url, headers, body)   # deliberately no Authorization
        except Exception as e:
            raise CanvasError(f"Network error during upload: {type(e).__name__}: {e}", ambiguous=True) from None
        self.posts_sent += 1
        self.writes_sent += 1
        if resp.status >= 500 or resp.status == 408:
            raise CanvasError(_error_message(resp), status=resp.status, ambiguous=True)
        if resp.status >= 400:
            raise CanvasError(_error_message(resp), status=resp.status)
        return resp

    def confirm_upload(self, resp: Response) -> dict:
        """Step 3: turn the storage server's reply into Canvas's file record.
        Any follow-up request goes ONLY to the Canvas host (checked by _url)."""
        location = _header(resp.headers, "Location")
        if 300 <= resp.status < 400:
            if not location:
                raise CanvasError("Upload redirect had no Location", ambiguous=True)
            return self._request("GET", location)[0]          # same-host check + auth
        try:
            data = json.loads(resp.body.decode("utf-8") or "null")
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("id") and (data.get("display_name") or data.get("filename")):
            return data
        follow = (data or {}).get("location") if isinstance(data, dict) else None
        follow = follow or location
        if follow:
            return self._request("GET", follow)[0]
        raise CanvasError("Canvas did not confirm the upload", ambiguous=True)

    # ------------------------------------------ publish edits (publish mode only)
    def set_assignment_published(self, course_id: int, assignment_id: int, published: bool) -> dict:
        return self._request("PUT", f"/api/v1/courses/{int(course_id)}/assignments/{int(assignment_id)}",
                             form=[("assignment[published]", "true" if published else "false")])[0]

    def set_file_locked(self, file_id: int, locked: bool) -> dict:
        return self._request("PUT", f"/api/v1/files/{int(file_id)}",
                             form=[("locked", "true" if locked else "false")])[0]

    # ------------------------------------- grading writes (grading mode only)
    def set_weighting(self, course_id: int, on: bool) -> dict:
        return self._request("PUT", f"/api/v1/courses/{int(course_id)}",
                             form=[("course[apply_assignment_group_weights]", "true" if on else "false")])[0]

    def create_assignment_group(self, course_id: int, name: str, weight: float) -> dict:
        return self._request("POST", f"/api/v1/courses/{int(course_id)}/assignment_groups",
                             form=[("name", name), ("group_weight", fmt_weight(weight))])[0]

    def set_group_weight(self, course_id: int, group_id: int, weight: float) -> dict:
        return self._request("PUT", f"/api/v1/courses/{int(course_id)}/assignment_groups/{int(group_id)}",
                             form=[("group_weight", fmt_weight(weight))])[0]

    def move_assignment(self, course_id: int, assignment_id: int, group_id: int) -> dict:
        return self._request("PUT", f"/api/v1/courses/{int(course_id)}/assignments/{int(assignment_id)}",
                             form=[("assignment[assignment_group_id]", str(int(group_id)))])[0]

    # -------------------------------------------- write (POST, create only)
    def create_announcement(self, course_id: int, form: list) -> dict:
        return self._request("POST", f"/api/v1/courses/{int(course_id)}/discussion_topics", form=form)[0]

    def create_assignment(self, course_id: int, form: list) -> dict:
        return self._request("POST", f"/api/v1/courses/{int(course_id)}/assignments", form=form)[0]


def fmt_weight(w) -> str:
    """12.5 -> '12.5', 20.0 -> '20' (at most 4 decimals)."""
    text = f"{float(w):.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _header(headers: dict, name: str):
    for k, v in (headers or {}).items():
        if k.lower() == name.lower():
            return v
    return None


def _error_message(resp: Response) -> str:
    detail = ""
    try:
        payload = json.loads(resp.body.decode("utf-8"))
        if isinstance(payload, dict):
            errs = payload.get("errors") or payload.get("message")
            detail = json.dumps(errs)[:300] if errs else ""
    except Exception:
        pass
    hints = {401: "token is invalid or expired", 403: "Canvas refused: no permission, or the item is locked",
             404: "not found", 429: "Canvas is throttling requests"}
    hint = hints.get(resp.status, "")
    return f"Canvas returned HTTP {resp.status}" + (f" ({hint})" if hint else "") + (f": {detail}" if detail else "")
