"""Uploading files from your computer to one or more Canvas courses.

Same safety model as sending announcements (copier.py):
  * preview first (read-only), dry run, typed confirmation in the GUI
  * the ledger records (file contents, target course) BEFORE uploading, so the
    same file can never be uploaded to the same course twice
  * a course that already has a file with that name in that folder is skipped
  * Canvas is always told on_duplicate=rename: nothing is ever overwritten
  * the upload server must be a known Canvas storage host and never gets your token
  * uploads are rate limited, never retried, and the run stops on an unclear result
  * after each upload the file is read back from Canvas and checked (size, visibility)

A Canvas upload has three steps:
  1. "preflight": ask Canvas where to put the file   (nothing created yet)
  2. send the bytes to the storage server Canvas named
  3. confirm, which gives the new file's record
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .canvas_client import (MAX_UPLOAD_BYTES, CanvasError, SafeCanvasClient, SafetyViolation,
                            valid_file_name, validate_folder_path)
from .config import UPLOAD_DEFAULTS, Limits
from .copier import PlanError, Target
from .ledger import AlreadyRecorded, Ledger, SendKey
from .rate_limiter import RateLimiter, RateLimitExceeded


@dataclass(frozen=True)
class LocalFile:
    path: str
    name: str
    size: int
    sha256: str
    content_type: str

    @property
    def ledger_id(self) -> int:
        """60-bit number derived from the file's contents (fits SQLite INTEGER)."""
        return int(self.sha256[:15], 16)


def inspect_local_file(path: str) -> LocalFile:
    p = Path(path)
    if not p.is_file():
        raise PlanError(f"Not a file: {path}")
    size = p.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        raise PlanError(f"{p.name} is {size / 1e6:.0f} MB; the limit is {MAX_UPLOAD_BYTES / 1e6:.0f} MB "
                        "(change with CCC_MAX_UPLOAD_MB).")
    if not valid_file_name(p.name):
        raise PlanError(f"Unsupported file name: {p.name!r}")
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return LocalFile(str(p), p.name, size, h.hexdigest(), ctype)


@dataclass
class UploadRow:
    file: LocalFile
    target: Target
    action: str              # send | skip
    reason: str = ""


@dataclass
class UploadPlan:
    files: list
    folder: str
    publish: bool
    rows: list
    warnings: list = field(default_factory=list)

    @property
    def to_send(self):
        return [r for r in self.rows if r.action == "send"]


@dataclass
class UploadResult:
    file: LocalFile
    target: Target
    outcome: str             # sent | dry-run | skipped | failed | unknown | not-sent | verify-failed
    message: str = ""
    file_id: int | None = None
    stop: bool = False       # True = halt the rest of the run (unclear or systemic problem)


def folder_label(folder: str) -> str:
    return f"'{folder}'" if folder else "the top Files folder"


class FileCenter:
    def __init__(self, send_client: SafeCanvasClient, publish_client: SafeCanvasClient, ledger: Ledger,
                 limits: Limits | None = None, clock=time.time, sleep=time.sleep):
        if send_client.mode != "send" or publish_client.mode != "publish":
            raise ValueError("FileCenter needs a send-mode and a publish-mode client")
        self.client = send_client
        self.publisher = publish_client
        self.ledger = ledger
        self.limits = limits or UPLOAD_DEFAULTS
        self.limiter = RateLimiter(ledger, self.limits, clock=clock, sleep=sleep, channel="upload")

    # ------------------------------------------------------------ helpers
    def _key(self, f: LocalFile, t: Target) -> SendKey:
        return SendKey("file", 0, f.ledger_id, t.course_id)

    def _ledger_block(self, key) -> str | None:
        row = self.ledger.find(key)
        if row is None or row.status == "failed":
            return None
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row.updated_at))
        if row.status == "sent":
            return f"This file was already uploaded here by this tool on {when}"
        return (f"An upload on {when} did not confirm. Check Files in Canvas; "
                f"see docs for 'release' (ledger #{row.id})")

    def existing_names(self, course_id: int, folder: str) -> set:
        """Lower-cased names of files already in that folder of the course."""
        wanted = ("course files/" + folder if folder else "course files").casefold()
        folders = self.client.list_folders(course_id)
        match = [f for f in folders if str(f.get("full_name", "")).casefold() == wanted]
        if not match and not folder:
            match = [f for f in folders if f.get("parent_folder_id") is None]
        names = set()
        for fo in match:
            for f in self.client.list_folder_files(fo["id"]):
                for k in ("display_name", "filename"):
                    if f.get(k):
                        names.add(str(f[k]).casefold())
        return names

    # ------------------------------------------------------------ preview
    def build_plan(self, paths: list, folder: str, targets: list, publish: bool = True) -> UploadPlan:
        if not paths:
            raise PlanError("Add at least one file.")
        if not targets:
            raise PlanError("Choose at least one course.")
        try:
            folder = validate_folder_path(folder)
        except ValueError as e:
            raise PlanError(str(e)) from None
        files, by_hash, by_name, warnings = [], set(), {}, []
        for p in paths:
            f = inspect_local_file(p)
            if f.sha256 in by_hash:
                warnings.append(f"{f.name} was added twice; it will be uploaded once.")
                continue
            if f.name.casefold() in by_name:
                raise PlanError(f"Two different files are both named '{f.name}'. Rename one first.")
            by_hash.add(f.sha256)
            by_name[f.name.casefold()] = f
            files.append(f)
        rows, seen = [], set()
        for t in targets:
            if t.course_id in seen:
                continue
            seen.add(t.course_id)
            try:
                names = self.existing_names(t.course_id, folder)
            except CanvasError as e:
                rows += [UploadRow(f, t, "skip", f"Could not read course files: {e}") for f in files]
                continue
            for f in files:
                reason = self._ledger_block(self._key(f, t))
                if reason:
                    rows.append(UploadRow(f, t, "skip", reason))
                elif f.name.casefold() in names:
                    rows.append(UploadRow(f, t, "skip", f"A file named '{f.name}' is already in {folder_label(folder)}"))
                else:
                    rows.append(UploadRow(f, t, "send", "Ready"))
        plan = UploadPlan(files, folder, publish, rows, warnings)
        if len(plan.to_send) > self.limits.max_posts_per_run:
            raise PlanError(f"This would upload {len(plan.to_send)} file copies; the per-run limit is "
                            f"{self.limits.max_posts_per_run}. Select fewer files or courses.")
        return plan

    # ------------------------------------------------------------ execute
    def execute(self, plan: UploadPlan, dry_run: bool = True,
                progress: Callable[[UploadResult], None] | None = None,
                cancel: threading.Event | None = None) -> list:
        emit = progress or (lambda r: None)
        results, stop = [], None
        self.limiter.start_run()
        names_cache: dict = {}
        for row in plan.to_send:
            f, t = row.file, row.target
            if stop or (cancel and cancel.is_set()):
                r = UploadResult(f, t, "not-sent", stop or "Cancelled before uploading")
            else:
                r = self._one(plan, f, t, dry_run, names_cache)
                if r.stop or r.outcome in ("unknown", "verify-failed"):
                    stop = f"Stopped after a problem with {r.file.name} in {r.target.name}."
            results.append(r)
            emit(r)
        return results

    def _one(self, plan, f: LocalFile, t: Target, dry_run: bool, names_cache: dict) -> UploadResult:
        key = self._key(f, t)
        reason = self._ledger_block(key)
        if reason:
            return UploadResult(f, t, "skipped", reason)
        try:
            if t.course_id not in names_cache:
                names_cache[t.course_id] = self.existing_names(t.course_id, plan.folder)
        except CanvasError as e:
            return UploadResult(f, t, "skipped", f"Could not read course files: {e}")
        if f.name.casefold() in names_cache[t.course_id]:
            return UploadResult(f, t, "skipped", f"A file named '{f.name}' is already in {folder_label(plan.folder)}")
        try:
            data = Path(f.path).read_bytes()
        except OSError as e:
            return UploadResult(f, t, "skipped", f"Could not read the file: {e}")
        if len(data) != f.size or hashlib.sha256(data).hexdigest() != f.sha256:
            return UploadResult(f, t, "skipped", "The file changed on your computer after the preview. Preview again.")
        if dry_run:
            return UploadResult(f, t, "dry-run", "Would be uploaded (dry run - nothing sent)")

        try:
            row_id = self.ledger.reserve(key, f.name)
        except AlreadyRecorded as e:
            return UploadResult(f, t, "skipped", str(e))
        try:
            self.limiter.acquire()
        except RateLimitExceeded as e:
            self.ledger.release(row_id)                      # nothing was sent
            return UploadResult(f, t, "failed", f"Not uploaded: {e}", stop=True)

        # Step 1: preflight. Nothing exists in Canvas yet, so any failure is definite.
        try:
            pre = self.client.upload_preflight(t.course_id, f.name, f.size, f.content_type, plan.folder)
            if not isinstance(pre, dict) or not isinstance(pre.get("upload_url"), str):
                raise CanvasError("Canvas returned an unexpected upload reply")
        except (CanvasError, SafetyViolation) as e:
            self.ledger.mark_failed(row_id, str(e))
            stop = isinstance(e, CanvasError) and e.status in (401, 429)
            return UploadResult(f, t, "failed", f"Canvas did not accept the upload: {e}", stop=stop)
        # Step 2: send the bytes to the storage server.
        try:
            resp = self.client.upload_file_data(pre["upload_url"], pre.get("upload_params") or {},
                                                 f.name, f.content_type, data)
        except SafetyViolation as e:
            self.ledger.mark_failed(row_id, str(e))       # refused before any byte was sent
            return UploadResult(f, t, "failed", str(e), stop=True)
        except CanvasError as e:
            if e.ambiguous:
                self.ledger.note_pending(row_id, str(e))
                return UploadResult(f, t, "unknown", f"Upload may or may not have completed - check Files in "
                                    f"Canvas before doing anything. {e}")
            self.ledger.mark_failed(row_id, str(e))
            return UploadResult(f, t, "failed", f"The storage server refused the file: {e}")
        # Step 3: confirm with Canvas and get the file record.
        try:
            created = self.client.confirm_upload(resp)
            file_id = int(created["id"])
        except (CanvasError, SafetyViolation, KeyError, TypeError, ValueError) as e:
            self.ledger.note_pending(row_id, f"uploaded but not confirmed: {e}")
            return UploadResult(f, t, "unknown", f"The file was sent but Canvas did not confirm it - check Files "
                                f"in Canvas. {e}")
        self.ledger.mark_sent(row_id, file_id)
        names_cache[t.course_id].add(f.name.casefold())

        # Optional: hide it from students.
        if not plan.publish:
            try:
                self.limiter.acquire()
                self.publisher.set_file_locked(file_id, True)
            except (CanvasError, RateLimitExceeded) as e:
                return UploadResult(f, t, "verify-failed", f"Uploaded, but could NOT be unpublished, so students "
                                    f"may see it. Unpublish it in Canvas now. ({e})", file_id)
        # Verify: read it back.
        try:
            back = self.client.get_file(file_id)
        except CanvasError as e:
            return UploadResult(f, t, "verify-failed", f"Uploaded (id {file_id}) but could not be checked: {e}", file_id)
        problems = []
        if int(back.get("size", -1)) != f.size:
            problems.append(f"size in Canvas is {back.get('size')} bytes, expected {f.size}")
        if bool(back.get("locked")) == plan.publish:
            problems.append("visibility is " + ("unpublished" if back.get("locked") else "published") +
                            ", expected " + ("published" if plan.publish else "unpublished"))
        if problems:
            return UploadResult(f, t, "verify-failed", "VERIFY FAILED: " + "; ".join(problems), file_id)
        shown = back.get("display_name") or f.name
        note = f" (Canvas named it '{shown}')" if shown != f.name else ""
        vis = "published" if plan.publish else "unpublished (hidden from students)"
        return UploadResult(f, t, "sent", f"Uploaded to {folder_label(plan.folder)}, {vis}{note}", file_id)
