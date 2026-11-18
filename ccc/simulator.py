"""An in-memory fake Canvas server, used by the tests, the scripted demo
(`python -m ccc simulate`) and the GUI's simulation mode.

It plugs in where the real network transport would, so the exact same client,
ledger, rate limiter and GUI code paths run - only the network is fake.
It also records every request (and every write's exact form fields), so tests
can prove precisely what was - and was not - sent.

The fake deliberately applies ANY field it receives on an edit (like real
Canvas would), so if the app ever sent an extra field - points, dates, names -
the tests would see the change. Fault injection lets tests simulate timeouts,
server errors, locked grading periods and even Canvas-side data corruption.
"""

from __future__ import annotations

import copy
import itertools
import hashlib
import json
import random
import re
import urllib.parse

from .canvas_client import Response

SIM_URL = "https://canvas.simulated.test"
SIM_TOKEN = "SIMULATED-TOKEN-1234"


class FakeCanvas:
    SUBMITTERS_PER_COURSE = 5   # students with submissions per course (kept small for speed)

    def __init__(self, base_url: str = SIM_URL, token: str = SIM_TOKEN, page_size: int = 2):
        self.base_url = base_url
        self.token = token
        self.page_size = page_size
        self.courses: dict[int, dict] = {}
        self.requests: list[tuple[str, str]] = []
        self.writes: list[tuple[str, str, dict]] = []   # (method, path, form) of every write
        self.emails: list[dict] = []          # simulated notification emails
        self.fail_next_post: object = None    # int status, or "timeout"
        # Fault injection for any write (POST/PUT), keyed by 1-based write number:
        #   "timeout"        - Canvas applies the change, but the reply is lost
        #   "timeout_before" - connection drops before Canvas applies anything
        #   int (e.g. 500)   - Canvas returns that status and applies nothing
        self.fail_writes: dict[int, object] = {}
        self.locked_assignments: set[int] = set()   # e.g. in a closed grading period -> 403
        self.corrupt_on_move = False                 # simulate a Canvas-side grade change
        self._ids = itertools.count(1000)
        self._group_ids = itertools.count(500)
        self._sub_ids = itertools.count(50000)
        # ---- file storage server (Canvas's "inst-fs") ----
        self.upload_host = "sim-uploads.inscloudgate.net"   # matches the real allow-list pattern
        self.upload_host_override = None    # tests: pretend Canvas names some other host
        self.upload_mode = "json"           # json | redirect | created_location (all 3 real Canvas styles)
        self.upload_requests: list[dict] = []
        self.fail_uploads: dict[int, object] = {}   # by upload number: "timeout" | "timeout_before" | int
        self.fail_confirm = False
        self._pending_uploads: dict[str, dict] = {}
        self._file_ids = itertools.count(80000)
        self._folder_ids = itertools.count(9000)

    # ------------------------------------------------------------ fixtures
    def add_course(self, course_id, name, role="teacher", published=True, students=20,
                   weighted=False):
        self.courses[course_id] = {
            "id": course_id, "name": name, "course_code": name.split(" - ")[0],
            "workflow_state": "available" if published else "unpublished",
            "role": role, "students": students, "announcements": [], "assignments": [],
            "apply_assignment_group_weights": weighted, "assignment_groups": [], "submissions": [],
            "folders": [], "files": [],
        }
        self.courses[course_id]["folders"].append(
            {"id": next(self._folder_ids), "name": "course files", "full_name": "course files",
             "parent_folder_id": None})
        self.add_group(course_id, "Assignments", 0)    # Canvas always has a default group
        return self.courses[course_id]

    def add_group(self, course_id, name, weight, rules=None):
        c = self.courses[course_id]
        g = {"id": next(self._group_ids), "name": name, "group_weight": float(weight),
             "position": len(c["assignment_groups"]) + 1, "rules": rules or {}}
        c["assignment_groups"].append(g)
        return g

    def folder_for(self, course_id, path, create=False):
        c = self.courses[course_id]
        folder = c["folders"][0]
        for part in [p for p in (path or "").split("/") if p]:
            full = folder["full_name"] + "/" + part
            nxt = next((f for f in c["folders"] if f["full_name"].lower() == full.lower()), None)
            if nxt is None:
                if not create:
                    return None
                nxt = {"id": next(self._folder_ids), "name": part, "full_name": full,
                       "parent_folder_id": folder["id"]}
                c["folders"].append(nxt)
            folder = nxt
        return folder

    def add_file(self, course_id, name, folder="", data=b"%PDF-1.4 sample", locked=False):
        f = self.folder_for(course_id, folder, create=True)
        item = {"id": next(self._file_ids), "display_name": name, "filename": name, "size": len(data),
                "content-type": "application/pdf", "folder_id": f["id"], "locked": locked, "hidden": False,
                "url": f"{self.base_url}/files/download", "_sha256": hashlib.sha256(data).hexdigest()}
        self.courses[course_id]["files"].append(item)
        return item

    def _find_file(self, file_id):
        for c in self.courses.values():
            for f in c["files"]:
                if str(f["id"]) == str(file_id):
                    return c, f
        return None, None

    @staticmethod
    def _public_file(f):
        return {k: v for k, v in f.items() if not k.startswith("_")}

    def group_id(self, course_id, name):
        return next(g["id"] for g in self.courses[course_id]["assignment_groups"] if g["name"] == name)

    def add_announcement(self, course_id, title, message="<p>Hello class</p>", **extra):
        item = {"id": next(self._ids), "title": title, "message": message,
                "is_announcement": True, "published": True,
                "posted_at": "2026-11-10T14:00:00Z", **extra}
        self.courses[course_id]["announcements"].append(item)
        return item

    def add_assignment(self, course_id, name, group=None, submissions=True, **extra):
        c = self.courses[course_id]
        gid = self.group_id(course_id, group) if group else c["assignment_groups"][0]["id"]
        item = {"id": next(self._ids), "name": name, "description": "<p>Do the work.</p>",
                "points_possible": 10, "grading_type": "points",
                "submission_types": ["online_upload"], "allowed_extensions": ["pdf"],
                "due_at": "2026-11-24T04:59:00Z", "unlock_at": None, "lock_at": None,
                "published": True, "assignment_group_id": gid, **extra}
        c["assignments"].append(item)
        if submissions:
            self._make_submissions(c, item)
        return item

    def _make_submissions(self, course, assignment):
        rnd = random.Random(course["id"] * 100003 + assignment["id"])
        for k in range(min(course["students"], self.SUBMITTERS_PER_COURSE)):
            state = rnd.choice(["graded", "graded", "submitted", "unsubmitted"])
            score = round(rnd.uniform(0, assignment["points_possible"] or 10), 2) if state == "graded" else None
            course["submissions"].append({
                "id": next(self._sub_ids), "assignment_id": assignment["id"], "user_id": 7000 + k,
                "score": score, "grade": None if score is None else str(score),
                "entered_score": score, "entered_grade": None if score is None else str(score),
                "excused": rnd.random() < 0.05, "workflow_state": state,
                "submitted_at": None if state == "unsubmitted" else "2026-11-20T15:00:00Z",
                "attempt": None if state == "unsubmitted" else rnd.randint(1, 3),
                "late_policy_status": rnd.choice([None, None, "late", "missing"]),
                "points_deducted": rnd.choice([None, 0.0, 1.5]), "seconds_late": rnd.choice([0, 0, 3600]),
                "submission_type": None if state == "unsubmitted" else "online_upload",
                "graded_at": "2026-11-21T10:00:00Z" if score is not None else None,
                "attachments": [] if state == "unsubmitted" else [{"id": next(self._sub_ids)}],
            })

    @classmethod
    def demo(cls, page_size: int = 2) -> "FakeCanvas":
        fc = cls(page_size=page_size)
        fc.add_course(101, "MA 121 - Section A", students=31, weighted=True)
        fc.add_course(102, "MA 121 - Section B", students=28)
        fc.add_course(103, "MA 121 - Section C", students=25, weighted=True)
        fc.add_course(104, "MA 121 - Section D (unpublished)", published=False, students=0)
        fc.add_course(105, "MA 121 - Section E", students=30, weighted=True)
        fc.add_course(900, "PHY 101 - (you are a student here)", role="student")

        # Source section A: the weighting you set up once in Canvas.
        for name, w in (("Homework", 20), ("Labs", 25), ("Exams", 55)):
            fc.add_group(101, name, w)
        fc.add_announcement(101, "Welcome to Week 12", "<p>Quiz moved to Friday. Office hours 3-5pm.</p>")
        fc.add_announcement(101, "Midterm grades posted")
        fc.add_file(101, "Syllabus.pdf")
        fc.add_file(101, "Lab Safety Rules.pdf", folder="Labs", locked=True)
        fc.add_file(103, "Week 12 Notes.pdf", folder="Week 12")
        fc.add_assignment(101, "Homework 7 - Integration by Parts", group="Homework")
        fc.add_assignment(101, "Lab Report 3", group="Labs",
                          submission_types=["online_text_entry", "online_upload"], points_possible=25)
        fc.add_assignment(101, "Quiz 5", group="Exams", submission_types=["online_quiz"], quiz_id=55)
        fc.add_assignment(101, "Project with linked file", group="Labs",
                          description='<p>See <a href="/courses/101/files/9/download">rubric</a></p>')
        for name, group in (("Homework 1", "Homework"), ("Lab Report 1", "Labs"), ("Midterm Exam", "Exams")):
            fc.add_assignment(101, name, group=group, points_possible=50 if group == "Exams" else 10)
        fc.add_assignment(101, "Final Project (draft)", group="Labs", published=False, submissions=False)
        fc.add_assignment(101, "Bonus Problem Set", group="Homework", submissions=False)

        # Section B: never set up - everything in the default 0% group, weighting off.
        for name in ("Homework 1", "Lab Report 1", "Midterm Exam", "Reading Quiz"):
            fc.add_assignment(102, name)
        # Section C: same groups, different weights. Already has a Homework 7 (made by hand).
        for name, w in (("Homework", 30), ("Labs", 30), ("Exams", 40)):
            fc.add_group(103, name, w)
        fc.add_assignment(103, "Homework 7 - Integration by Parts")
        for name, group in (("Homework 1", "Homework"), ("Lab Report 1", "Labs"), ("Midterm Exam", "Exams")):
            fc.add_assignment(103, name, group=group)
        # Section D: has an extra 10% "Extra Credit" group -> totals would not match.
        fc.add_group(104, "Extra Credit", 10)
        for name in ("Homework 1", "Midterm Exam"):
            fc.add_assignment(104, name)
        # Section E: already identical to section A.
        for name, w in (("Homework", 20), ("Labs", 25), ("Exams", 55)):
            fc.add_group(105, name, w)
        for name, group in (("Homework 1", "Homework"), ("Lab Report 1", "Labs"), ("Midterm Exam", "Exams")):
            fc.add_assignment(105, name, group=group)
        return fc

    def snapshot(self):
        """Deep copy of all course data (for 'nothing changed' tests)."""
        return copy.deepcopy(self.courses)

    # ------------------------------------------------------------- server
    def __call__(self, method, url, headers, body):
        parts = urllib.parse.urlsplit(url)
        self.requests.append((method, parts.path))
        if parts.scheme == "https" and parts.hostname == self.upload_host:
            return self._upload(method, url, headers, body)
        if f"{parts.scheme}://{parts.netloc}" != self.base_url:
            return self._json(404, {"errors": "unknown host"})
        if headers.get("Authorization") != f"Bearer {self.token}":
            return self._json(401, {"errors": [{"message": "Invalid access token."}]})
        query = urllib.parse.parse_qs(parts.query)
        segs = [s for s in parts.path.split("/") if s][2:]   # drop api/v1

        if method == "GET":
            return self._get(segs, query, parts.path)
        if method in ("POST", "PUT"):
            form = urllib.parse.parse_qs(body.decode(), keep_blank_values=True) if body else {}
            self.writes.append((method, parts.path, form))
            fail = self.fail_writes.pop(len(self.writes), None)
            handler = self._post if method == "POST" else self._put
            if fail == "timeout":
                handler(segs, form)                       # applied, but the reply is lost
                raise TimeoutError("The read operation timed out")
            if fail == "timeout_before":
                raise ConnectionResetError("Connection reset by peer")
            if fail is not None:
                return self._json(int(fail), {"errors": [{"message": "simulated failure"}]})
            return handler(segs, form)
        return self._json(405, {"errors": "method not allowed"})

    def _json(self, status, payload, headers=None):
        return Response(status, headers or {}, json.dumps(payload).encode())

    def _paged(self, items, query, path):
        size = int(query.get("per_page", [self.page_size])[0])
        size = min(size, self.page_size)        # emulate Canvas capping page size
        page = int(query.get("page", ["1"])[0])
        chunk = items[(page - 1) * size: page * size]
        headers = {}
        if page * size < len(items):
            q = {k: v for k, v in query.items() if k != "page"}
            q["page"] = [str(page + 1)]
            headers["Link"] = f'<{self.base_url}{path}?{urllib.parse.urlencode(q, doseq=True)}>; rel="next"'
        return self._json(200, chunk, headers)

    def _course(self, cid):
        try:
            c = self.courses.get(int(cid))
        except ValueError:
            return None
        return c if c and c["role"] == "teacher" else None

    # ------------------------------------------------- file storage server
    def _upload(self, method, url, headers, body):
        self.upload_requests.append({"method": method, "url": url, "headers": dict(headers),
                                     "size": len(body or b"")})
        n = len(self.upload_requests)
        fail = self.fail_uploads.pop(n, None)
        if fail == "timeout_before":
            raise ConnectionResetError("Connection reset by peer")
        if fail is not None and fail != "timeout":
            return self._json(int(fail), {"errors": "simulated storage failure"})
        token = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("t", [""])[0]
        pending = self._pending_uploads.pop(token, None)
        if method != "POST" or pending is None:
            return self._json(400, {"errors": "bad or reused upload token"})
        data = _multipart_file(headers.get("Content-Type", ""), body or b"")
        if data is None or len(data) != pending["size"]:
            return self._json(400, {"errors": "size mismatch"})
        if self.upload_mode == "redirect":       # file appears only after the confirm GET
            pending["data"] = data
            self._pending_uploads["confirm:" + token] = pending
            resp = Response(302, {"Location": f"{self.base_url}/api/v1/files/0/create_success?uuid={token}"}, b"")
        else:
            f = self._store_upload(pending, data)
            if self.upload_mode == "created_location":
                resp = Response(201, {"Location": f"{self.base_url}/api/v1/files/{f['id']}"}, b"")
            else:
                resp = self._json(201, {**self._public_file(f), "location": f"{self.base_url}/api/v1/files/{f['id']}"})
        if fail == "timeout":
            raise TimeoutError("The read operation timed out")   # stored, but reply lost
        return resp

    def _store_upload(self, pending, data):
        c = self.courses[pending["course_id"]]
        folder = self.folder_for(c["id"], pending["folder"], create=True)
        name = pending["name"]
        existing = {f["display_name"].lower() for f in c["files"] if f["folder_id"] == folder["id"]}
        if name.lower() in existing:          # on_duplicate=rename, like Canvas
            stem, dot, ext = name.rpartition(".")
            i = 1
            while f"{stem or ext}-{i}{dot}{ext if stem else ''}".lower() in existing:
                i += 1
            name = f"{stem or ext}-{i}{dot}{ext if stem else ''}"
        f = self.add_file(c["id"], name, folder=pending["folder"], data=data)
        f["content-type"] = pending["content_type"]
        return f

    def _get(self, segs, query, path):
        if segs == ["users", "self"]:
            return self._json(200, {"id": 1, "name": "Simulated Teacher"})
        if segs == ["courses"]:
            states = set(query.get("state[]", ["available"]))
            mine = [{k: c[k] for k in ("id", "name", "course_code", "workflow_state")}
                    for c in self.courses.values()
                    if c["role"] == query.get("enrollment_type", ["teacher"])[0]
                    and c["workflow_state"] in states]
            return self._paged(mine, query, path)
        if segs[:1] == ["files"] and len(segs) == 3 and segs[2] == "create_success":
            if self.fail_confirm:
                return self._json(500, {"errors": "simulated confirm failure"})
            pending = self._pending_uploads.pop("confirm:" + query.get("uuid", [""])[0], None)
            if not pending:
                return self._json(404, {"errors": "not found"})
            return self._json(200, self._public_file(self._store_upload(pending, pending["data"])))
        if segs[:1] == ["files"] and len(segs) == 2:
            c, f = self._find_file(segs[1])
            return self._json(200, self._public_file(f)) if f and c["role"] == "teacher" else \
                self._json(404, {"errors": "not found"})
        if segs[:1] == ["folders"] and len(segs) == 3 and segs[2] == "files":
            for c in self.courses.values():
                if c["role"] == "teacher" and any(str(fo["id"]) == segs[1] for fo in c["folders"]):
                    return self._paged([self._public_file(f) for f in c["files"] if str(f["folder_id"]) == segs[1]],
                                       query, path)
            return self._json(404, {"errors": "not found"})
        if len(segs) >= 2 and segs[0] == "courses":
            c = self._course(segs[1])
            if not c:
                return self._json(404, {"errors": "not found"})
            if segs[2:] == ["files"]:
                return self._paged([self._public_file(f) for f in c["files"]], query, path)
            if segs[2:] == ["folders"]:
                return self._paged(copy.deepcopy(c["folders"]), query, path)
            if len(segs) == 5 and segs[2] == "assignments" and segs[4] == "submissions":
                return self._paged([copy.deepcopy(x) for x in c["submissions"] if str(x["assignment_id"]) == segs[3]],
                                   query, path)
            if len(segs) == 2:
                return self._json(200, {k: c[k] for k in ("id", "name", "course_code", "workflow_state",
                                                          "apply_assignment_group_weights")})
            if segs[2:] == ["students", "submissions"]:
                if query.get("student_ids[]") != ["all"]:
                    return self._json(400, {"errors": "student_ids required"})
                return self._paged(copy.deepcopy(c["submissions"]), query, path)
            coll = {"discussion_topics": "announcements", "assignments": "assignments",
                    "assignment_groups": "assignment_groups"}.get(segs[2])
            if not coll:
                return self._json(404, {"errors": "not found"})
            items = [self._enrich(c, coll, i) for i in c[coll]]
            if len(segs) == 3:
                return self._paged(items, query, path)
            if len(segs) == 4:
                for i in items:
                    if str(i["id"]) == segs[3]:
                        return self._json(200, i)
        return self._json(404, {"errors": "not found"})

    def _post(self, segs, form):
        if self.fail_next_post is not None:
            fail, self.fail_next_post = self.fail_next_post, None
            if fail == "timeout":
                # The item IS created, but the reply never arrives - the worst case.
                self._post_inner(segs, form)
                raise TimeoutError("The read operation timed out")
            return self._json(int(fail), {"errors": [{"message": "simulated failure"}]})
        return self._post_inner(segs, form)

    def _post_inner(self, segs, form):
        one = lambda k, d=None: form.get(k, [d])[0]
        if len(segs) != 3 or segs[0] != "courses":
            return self._json(404, {"errors": "not found"})
        c = self._course(segs[1])
        if not c:
            return self._json(403, {"errors": [{"message": "user not authorized"}]})
        if segs[2] == "discussion_topics" and one("is_announcement") == "true":
            item = {"id": next(self._ids), "title": one("title"), "message": one("message", ""),
                    "is_announcement": True, "published": one("published") == "true"}
            c["announcements"].insert(0, item)
            if c["workflow_state"] == "available" and item["published"]:
                self.emails.append({"course_id": c["id"], "title": item["title"], "recipients": c["students"]})
            return self._json(200, item)
        if segs[2] == "assignments":
            item = {"id": next(self._ids), "name": one("assignment[name]"),
                    "description": one("assignment[description]", ""),
                    "points_possible": float(one("assignment[points_possible]", 0)),
                    "grading_type": one("assignment[grading_type]", "points"),
                    "submission_types": form.get("assignment[submission_types][]", ["none"]),
                    "allowed_extensions": form.get("assignment[allowed_extensions][]", []),
                    "due_at": one("assignment[due_at]"), "unlock_at": one("assignment[unlock_at]"),
                    "lock_at": one("assignment[lock_at]"),
                    "published": one("assignment[published]") == "true",
                    "assignment_group_id": c["assignment_groups"][0]["id"]}
            c["assignments"].append(item)
            return self._json(201, item)
        if segs[2] == "files":
            token = f"u{len(self._pending_uploads)}-{next(self._file_ids)}"
            self._pending_uploads[token] = {
                "course_id": c["id"], "name": one("name"), "size": int(one("size", "0")),
                "content_type": one("content_type"), "folder": one("parent_folder_path", "") or ""}
            host = self.upload_host_override or self.upload_host
            return self._json(200, {"upload_url": f"https://{host}/upload?t={token}",
                                    "upload_params": {"filename": one("name"), "content_type": one("content_type")}})
        if segs[2] == "assignment_groups":
            g = self.add_group(c["id"], one("name", "Assignments"), float(one("group_weight", 0) or 0))
            return self._json(200, g)
        return self._json(400, {"errors": "bad request"})

    @staticmethod
    def _enrich(course, coll, item):
        item = copy.deepcopy(item)
        if coll == "assignments":   # computed by Canvas on every read
            item["has_submitted_submissions"] = any(
                s["assignment_id"] == item["id"] and s["workflow_state"] in ("submitted", "graded")
                for s in course["submissions"])
        return item

    def _put(self, segs, form):
        """Like Canvas: applies EVERY field it is given (so stray fields would be visible)."""
        if segs[:1] == ["files"] and len(segs) == 2:
            c, f = self._find_file(segs[1])
            if not f or c["role"] != "teacher":
                return self._json(404, {"errors": "not found"})
            for k, v in form.items():
                f[k] = (v[0] == "true") if k in ("locked", "hidden") else v[0]
            return self._json(200, self._public_file(f))
        if len(segs) < 2 or segs[0] != "courses":
            return self._json(404, {"errors": "not found"})
        c = self._course(segs[1])
        if not c:
            return self._json(403, {"errors": [{"message": "user not authorized"}]})
        if len(segs) == 2:
            for k, v in form.items():
                m = re.match(r"^course\[(\w+)\]$", k)
                if m:
                    c[m.group(1)] = (v[0] == "true") if m.group(1) == "apply_assignment_group_weights" else v[0]
            return self._json(200, {"id": c["id"], "apply_assignment_group_weights": c["apply_assignment_group_weights"]})
        if len(segs) == 4 and segs[2] == "assignment_groups":
            g = next((g for g in c["assignment_groups"] if str(g["id"]) == segs[3]), None)
            if not g:
                return self._json(404, {"errors": "not found"})
            for k, v in form.items():
                g[k] = float(v[0]) if k == "group_weight" else v[0]
            return self._json(200, g)
        if len(segs) == 4 and segs[2] == "assignments":
            a = next((a for a in c["assignments"] if str(a["id"]) == segs[3]), None)
            if not a:
                return self._json(404, {"errors": "not found"})
            if a["id"] in self.locked_assignments:
                return self._json(403, {"errors": [{"message": "Cannot change an assignment in a closed grading period"}]})
            for k, v in form.items():
                m = re.match(r"^assignment\[(\w+)\]$", k)
                if not m:
                    continue
                field = m.group(1)
                if field == "assignment_group_id":
                    gid = int(v[0])
                    if not any(g["id"] == gid for g in c["assignment_groups"]):
                        return self._json(400, {"errors": "invalid assignment group"})
                    if self.corrupt_on_move and gid != a["assignment_group_id"]:
                        for sub in c["submissions"]:          # a (simulated) Canvas-side change
                            if sub["assignment_id"] == a["id"]:
                                sub["score"] = (sub["score"] or 0) + 1
                                break
                    a[field] = gid
                elif field == "published":
                    if v[0] == "false" and any(s["assignment_id"] == a["id"] and s["workflow_state"] in
                                               ("submitted", "graded") for s in c["submissions"]):
                        return self._json(400, {"errors": {"published": [
                            {"message": "Can't unpublish if there are student submissions"}]}})
                    a[field] = v[0] == "true"
                elif field == "points_possible":
                    a[field] = float(v[0])
                else:
                    a[field] = v[0]
            return self._json(200, a)
        return self._json(404, {"errors": "not found"})


def _multipart_file(content_type: str, body: bytes):
    """Extract the bytes of the 'file' part (the storage server's job)."""
    m = re.search(r"boundary=([^;]+)", content_type)
    if not m:
        return None
    boundary = b"--" + m.group(1).strip().encode()
    for part in body.split(boundary):
        head, sep, rest = part.partition(b"\r\n\r\n")
        if sep and b'name="file"' in head:
            return rest[:-2] if rest.endswith(b"\r\n") else rest
    return None
