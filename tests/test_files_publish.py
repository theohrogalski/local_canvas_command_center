"""Tests 71-100: file uploads (Files tab) and publish/unpublish (Publish tab)."""

import os
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ccc.canvas_client import (CanvasError, Response, SafeCanvasClient, SafetyViolation,
                               check_publish_request, check_upload_preflight, upload_host_allowed,
                               validate_folder_path)
from ccc.config import Limits
from ccc.copier import PlanError, Target
from ccc.files import FileCenter
from ccc.ledger import Ledger
from ccc.simulator import SIM_TOKEN, SIM_URL, FakeCanvas
from ccc.visibility import VisibilityCenter

FAST = Limits(min_seconds_between_posts=0, max_posts_per_run=100, max_posts_per_hour=100000)


class FEnv:
    def __init__(self, limits=FAST, transport=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.fake = FakeCanvas.demo()
        self.before = self.fake.snapshot()
        self.ledger = Ledger(self.dir / "ledger.db")
        t = transport or self.fake
        self.send = SafeCanvasClient(SIM_URL, SIM_TOKEN, transport=t)
        self.pub = SafeCanvasClient(SIM_URL, SIM_TOKEN, transport=t, mode="publish")
        self.sleeps = []
        self.fc = FileCenter(self.send, self.pub, self.ledger, limits, sleep=self.sleeps.append)
        self.vc = VisibilityCenter(self.pub, self.ledger, limits, sleep=self.sleeps.append)

    def file(self, name, data=None):
        p = self.dir / name
        p.write_bytes(data if data is not None else f"contents of {name}".encode() * 50)
        return str(p)

    def targets(self, *ids):
        return [Target(i, self.fake.courses[i]["name"]) for i in (ids or (101, 102, 103, 105))]

    def files_named(self, cid, name, folder=None):
        c = self.fake.courses[cid]
        fids = {f["id"] for f in c["folders"]} if folder is None else \
            {f["id"] for f in c["folders"] if f["full_name"].lower() == ("course files/" + folder).lower().rstrip("/")}
        return [f for f in c["files"] if f["display_name"].lower() == name.lower() and f["folder_id"] in fids]

    def assignment(self, cid, name):
        return next(a for a in self.fake.courses[cid]["assignments"] if a["name"] == name)

    def close(self):
        self.ledger.close()
        self.tmp.cleanup()


def strip_files(courses):
    return {cid: {k: v for k, v in c.items() if k not in ("files", "folders")} for cid, c in courses.items()}


# ============================================================ client rules
class UploadPublishClientTests(unittest.TestCase):
    # 71
    def test_71_upload_request_fields_are_exact_and_never_overwrite(self):
        good = [("name", "a.pdf"), ("size", "10"), ("content_type", "application/pdf"), ("on_duplicate", "rename")]
        check_upload_preflight(good)
        check_upload_preflight(good + [("parent_folder_path", "Week 12/Handouts")])
        bad = [
            [*good[:3], ("on_duplicate", "overwrite")],
            good[:3],
            [*good, ("parent_folder_id", "5")],
            [*good, ("locked", "false")],
            [("name", "../x.pdf"), *good[1:]],
            [("name", "a/b.pdf"), *good[1:]],
            [("name", ""), *good[1:]],
            [good[0], ("size", "-1"), *good[2:]],
            [good[0], ("size", str(10**12)), *good[2:]],
            [*good[:2], ("content_type", "text/html; evil"), good[3]],
            [*good, ("parent_folder_path", "../../etc")],
            [*good, ("name", "b.pdf")],
        ]
        for form in bad:
            with self.subTest(form=form), self.assertRaises(SafetyViolation):
                check_upload_preflight(form)

    # 72
    def test_72_upload_server_must_be_a_known_canvas_storage_host(self):
        base = "https://school.instructure.com"
        for ok in ("https://inst-fs-iad-prod.inscloudgate.net/files?token=1", "https://instructure-uploads.s3.amazonaws.com/",
                   "https://instructure-uploads-2.s3-us-west-2.amazonaws.com/x", "https://school.instructure.com/files_api",
                   "https://other.instructure.com/upload"):
            self.assertTrue(upload_host_allowed(ok, base), ok)
        for bad in ("https://evil.example.com/", "https://inscloudgate.net.evil.com/", "http://x.inscloudgate.net/",
                    "https://user:pw@x.inscloudgate.net/", "https://x.inscloudgate.net:8443/",
                    "https://evil-instructure-uploads.s3.amazonaws.com/", "https://s3.amazonaws.com/instructure-uploads",
                    "not a url", "https://"):
            self.assertFalse(upload_host_allowed(bad, base), bad)
        with mock.patch.dict(os.environ, {"CCC_EXTRA_UPLOAD_HOSTS": "files.myschool.edu"}):
            self.assertTrue(upload_host_allowed("https://files.myschool.edu/u", base))
            self.assertFalse(upload_host_allowed("https://x.files.myschool.edu/u", base))

    # 73
    def test_73_file_bytes_never_carry_the_token_and_bad_hosts_get_nothing(self):
        calls = []
        t = lambda m, u, h, b: (calls.append((u, h)) or Response(201, {}, b'{"id": 1, "display_name": "a"}'))
        c = SafeCanvasClient("https://school.instructure.com", "SECRET-TOKEN", transport=t)
        c.upload_file_data("https://x.inscloudgate.net/u", {"k": "v"}, "a.pdf", "application/pdf", b"data")
        self.assertNotIn("Authorization", calls[0][1])
        self.assertFalse(any("SECRET-TOKEN" in str(v) for v in calls[0][1].values()))
        self.assertNotIn("SECRET-TOKEN", calls[0][0])
        with self.assertRaises(SafetyViolation):
            c.upload_file_data("https://evil.example.com/u", {}, "a.pdf", "application/pdf", b"data")
        self.assertEqual(len(calls), 1)                    # nothing sent to the bad host
        pc = SafeCanvasClient("https://school.instructure.com", "T", transport=t, mode="publish")
        with self.assertRaises(SafetyViolation):
            pc.upload_file_data("https://x.inscloudgate.net/u", {}, "a.pdf", "application/pdf", b"x")

    # 74
    def test_74_publish_mode_can_only_flip_published_state(self):
        check_publish_request("PUT", "/api/v1/courses/1/assignments/2", [("assignment[published]", "false")])
        check_publish_request("PUT", "/api/v1/files/9", [("locked", "true")])
        bad = [("PUT", "/api/v1/courses/1/assignments/2", [("assignment[published]", "true"), ("assignment[name]", "x")]),
               ("PUT", "/api/v1/courses/1/assignments/2", [("assignment[points_possible]", "0")]),
               ("PUT", "/api/v1/courses/1/assignments/2", [("assignment[published]", "yes")]),
               ("PUT", "/api/v1/files/9", [("locked", "true"), ("name", "x")]),
               ("PUT", "/api/v1/files/9", [("hidden", "true")]),
               ("PUT", "/api/v1/files/9", [("parent_folder_id", "1")]),
               ("PUT", "/api/v1/courses/1", [("course[apply_assignment_group_weights]", "true")]),
               ("POST", "/api/v1/courses/1/files", [("name", "a")])]
        for m, p, f in bad:
            with self.subTest(form=f), self.assertRaises(SafetyViolation):
                check_publish_request(m, p, f)
        calls = []
        t = lambda *a: calls.append(a) or Response(200, {}, b"{}")
        pc = SafeCanvasClient("https://s.instructure.com", "T", transport=t, mode="publish")
        for m, p, f in [("POST", "/api/v1/courses/1/discussion_topics", [("title", "x")]),
                        ("POST", "/api/v1/courses/1/assignments", [("assignment[name]", "x")]),
                        ("DELETE", "/api/v1/files/9", []), ("PATCH", "/api/v1/files/9", [])]:
            with self.subTest(path=p), self.assertRaises(SafetyViolation):
                pc._request(m, p, form=f)
        for mode in ("send", "grading"):
            other = SafeCanvasClient("https://s.instructure.com", "T", transport=t, mode=mode)
            with self.assertRaises(SafetyViolation):
                other.set_file_locked(9, True)
            with self.assertRaises(SafetyViolation):
                other.set_assignment_published(1, 2, False)
        self.assertEqual(calls, [])

    # 75
    def test_75_folder_paths_are_cleaned_and_validated(self):
        self.assertEqual(validate_folder_path(" Week 12 / Handouts/ "), "Week 12/Handouts")
        self.assertEqual(validate_folder_path("\\Labs\\Week 1"), "Labs/Week 1")
        self.assertEqual(validate_folder_path(""), "")
        for bad in ("../x", "a/../b", "a/./b", "a\nb", "/".join(["d"] * 11), "x" * 101):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_folder_path(bad)

    # 76
    def test_76_upload_confirmation_only_ever_goes_back_to_canvas(self):
        calls = []
        t = lambda m, u, h, b: calls.append((m, u, h)) or Response(200, {}, b'{"id": 5, "display_name": "a"}')
        c = SafeCanvasClient("https://school.instructure.com", "TOK", transport=t)
        c.confirm_upload(Response(302, {"Location": "https://school.instructure.com/api/v1/files/5/create_success?uuid=1"}, b""))
        self.assertEqual(calls[-1][2]["Authorization"], "Bearer TOK")
        for evil in ("https://evil.example.com/api/v1/files/5", "https://school.instructure.com.evil.com/api/v1/x",
                     "https://school.instructure.com/login/oauth"):
            with self.subTest(evil=evil), self.assertRaises(SafetyViolation):
                c.confirm_upload(Response(302, {"Location": evil}, b""))
            with self.assertRaises(SafetyViolation):
                c.confirm_upload(Response(201, {}, ('{"location": "%s"}' % evil).encode()))
        self.assertEqual(len(calls), 1)


# ================================================================ uploads
class UploadTests(unittest.TestCase):
    def setUp(self):
        self.env = FEnv()

    def tearDown(self):
        self.env.close()

    def upload(self, paths, folder="", ids=(), publish=True, dry=False):
        plan = self.env.fc.build_plan(paths, folder, self.env.targets(*ids), publish)
        return plan, self.env.fc.execute(plan, dry_run=dry)

    # 77
    def test_77_dry_run_uploads_nothing(self):
        _, results = self.upload([self.env.file("Slides.pdf")], dry=True)
        self.assertEqual({r.outcome for r in results}, {"dry-run"})
        self.assertEqual(self.env.fake.upload_requests, [])
        self.assertEqual(self.env.fake.writes, [])
        self.assertEqual(self.env.ledger.all_rows(), [])

    # 78
    def test_78_upload_puts_exactly_one_intact_copy_in_each_course(self):
        data = os.urandom(5000)
        p = self.env.file("Week 13 Slides.pdf", data)
        _, results = self.upload([p], folder="Week 13/Slides")
        self.assertEqual([r.outcome for r in results], ["sent"] * 4)
        import hashlib
        for cid in (101, 102, 103, 105):
            copies = self.env.files_named(cid, "Week 13 Slides.pdf", "Week 13/Slides")
            self.assertEqual(len(copies), 1)
            self.assertEqual(copies[0]["_sha256"], hashlib.sha256(data).hexdigest())
            self.assertFalse(copies[0]["locked"])
        self.assertEqual(self.env.files_named(104, "Week 13 Slides.pdf"), [])   # not selected

    # 79
    def test_79_unpublished_upload_is_hidden_and_verified(self):
        _, results = self.upload([self.env.file("Answer Key.pdf")], publish=False)
        self.assertEqual({r.outcome for r in results}, {"sent"})
        self.assertTrue(all("unpublished" in r.message for r in results))
        for cid in (101, 102, 103, 105):
            self.assertTrue(self.env.files_named(cid, "Answer Key.pdf")[0]["locked"])
        locks = [w for w in self.env.fake.writes if w[0] == "PUT"]
        self.assertEqual(len(locks), 4)
        self.assertTrue(all(sorted(w[2]) == ["locked"] and w[2]["locked"] == ["true"] for w in locks))

    # 80
    def test_80_all_three_canvas_upload_reply_styles_work(self):
        for mode in ("json", "redirect", "created_location"):
            with self.subTest(mode=mode):
                self.env.fake.upload_mode = mode
                _, results = self.upload([self.env.file(f"{mode}.pdf")], ids=(102,))
                self.assertEqual(results[0].outcome, "sent", results[0].message)
                self.assertEqual(len(self.env.files_named(102, f"{mode}.pdf")), 1)
                self.assertIsNotNone(results[0].file_id)

    # 81
    def test_81_same_file_is_never_uploaded_twice_even_if_renamed(self):
        p = self.env.file("Notes.pdf", b"exact same bytes" * 100)
        self.upload([p])
        n = len(self.env.fake.upload_requests)
        plan, results = self.upload([p])
        self.assertEqual(plan.to_send, [])
        renamed = self.env.file("Notes (copy).pdf", b"exact same bytes" * 100)
        plan2 = self.env.fc.build_plan([renamed], "", self.env.targets(), True)
        self.assertEqual(plan2.to_send, [])
        self.assertTrue(all("already uploaded" in r.reason for r in plan2.rows))
        self.assertEqual(len(self.env.fake.upload_requests), n)

    # 82
    def test_82_existing_file_name_in_that_folder_is_skipped(self):
        plan = self.env.fc.build_plan([self.env.file("syllabus.PDF")], "", self.env.targets(101, 102), True)
        rows = {r.target.course_id: r for r in plan.rows}
        self.assertEqual(rows[101].action, "skip")          # 101 already has Syllabus.pdf at top level
        self.assertIn("already in", rows[101].reason)
        self.assertEqual(rows[102].action, "send")
        plan = self.env.fc.build_plan([self.env.file("Syllabus.pdf")], "Archive", self.env.targets(101), True)
        self.assertEqual(plan.rows[0].action, "send")       # a different folder is fine

    # 83
    def test_83_file_changed_on_disk_after_preview_is_not_uploaded(self):
        p = self.env.file("Draft.pdf", b"version 1")
        plan = self.env.fc.build_plan([p], "", self.env.targets(102), True)
        Path(p).write_bytes(b"version 2!")
        results = self.env.fc.execute(plan, dry_run=False)
        self.assertEqual(results[0].outcome, "skipped")
        self.assertIn("changed on your computer", results[0].message)
        self.assertEqual(self.env.fake.upload_requests, [])

    # 84
    def test_84_lost_reply_after_storing_stops_and_never_duplicates(self):
        self.env.fake.fail_uploads = {1: "timeout"}
        p = self.env.file("Quiz Review.pdf")
        _, results = self.upload([p])
        self.assertEqual([r.outcome for r in results], ["unknown", "not-sent", "not-sent", "not-sent"])
        self.assertEqual(self.env.ledger.all_rows()[0].status, "pending")
        _, results = self.upload([p])
        self.assertEqual([r.outcome for r in results], ["sent"] * 3)          # 101 stays blocked
        for cid in (101, 102, 103, 105):
            self.assertEqual(len(self.env.files_named(cid, "Quiz Review.pdf")), 1)

    # 85
    def test_85_refusals_are_recorded_and_retryable(self):
        self.env.fake.fail_writes = {1: 403}               # first preflight refused
        p = self.env.file("Handout.pdf")
        _, results = self.upload([p], ids=(101, 102))
        self.assertEqual([r.outcome for r in results], ["failed", "sent"])
        self.assertEqual(self.env.files_named(101, "Handout.pdf"), [])
        _, results = self.upload([p], ids=(101, 102))
        self.assertEqual([r.outcome for r in results], ["sent"])           # only the failed one retried
        self.env.fake.fail_writes = {len(self.env.fake.writes) + 1: 401}
        _, results = self.upload([self.env.file("Other.pdf")], ids=(101, 102))
        self.assertEqual([r.outcome for r in results], ["failed", "not-sent"])   # bad token stops the run

    # 86
    def test_86_unknown_storage_server_is_refused_before_any_bytes(self):
        self.env.fake.upload_host_override = "evil.example.com"
        _, results = self.upload([self.env.file("Secret.pdf")], ids=(101, 102))
        self.assertEqual([r.outcome for r in results], ["failed", "not-sent"])
        self.assertIn("unrecognised server", results[0].message)
        self.assertEqual(self.env.fake.upload_requests, [])
        self.assertFalse(any("evil" in p for m, p in self.env.fake.requests))
        self.assertEqual(self.env.ledger.all_rows()[0].status, "failed")

    # 87
    def test_87_unconfirmed_upload_is_held_pending(self):
        self.env.fake.upload_mode = "redirect"
        self.env.fake.fail_confirm = True
        _, results = self.upload([self.env.file("Lab.pdf")], ids=(101, 102))
        self.assertEqual([r.outcome for r in results], ["unknown", "not-sent"])
        self.assertIn("did not confirm", results[0].message)
        self.assertEqual(self.env.ledger.all_rows()[0].status, "pending")

    # 88
    def test_88_ambiguous_file_selections_are_refused_or_deduplicated(self):
        a = self.env.file("Notes.pdf", b"AAA")
        sub = self.env.dir / "other"
        sub.mkdir()
        (sub / "Notes.pdf").write_bytes(b"BBB")
        with self.assertRaises(PlanError):
            self.env.fc.build_plan([a, str(sub / "Notes.pdf")], "", self.env.targets(102), True)
        plan = self.env.fc.build_plan([a, a], "", self.env.targets(102), True)
        self.assertEqual(len(plan.to_send), 1)
        self.assertTrue(plan.warnings)

    # 89
    def test_89_bad_inputs_are_refused_at_preview(self):
        with self.assertRaises(PlanError):
            self.env.fc.build_plan([str(self.env.dir / "missing.pdf")], "", self.env.targets(102), True)
        with self.assertRaises(PlanError):
            self.env.fc.build_plan([self.env.file("ok.pdf")], "../up", self.env.targets(102), True)
        with self.assertRaises(PlanError):
            self.env.fc.build_plan([], "", self.env.targets(102), True)
        with mock.patch("ccc.files.MAX_UPLOAD_BYTES", 10):
            with self.assertRaises(PlanError):
                self.env.fc.build_plan([self.env.file("big.pdf", b"x" * 11)], "", self.env.targets(102), True)
        self.assertEqual(self.env.fake.writes, [])

    # 90
    def test_90_upload_rate_limits(self):
        small = FEnv(Limits(0, 3, 1000))
        try:
            with self.assertRaises(PlanError):
                small.fc.build_plan([small.file("a.pdf"), small.file("b.pdf")], "", small.targets(101, 102), True)
        finally:
            small.close()
        hourly = FEnv(Limits(2, 100, 2))
        try:
            for _ in range(40):
                hourly.ledger.record_attempt(channel="post")        # announcement budget used up
            plan = hourly.fc.build_plan([hourly.file("a.pdf")], "", hourly.targets(), True)
            results = hourly.fc.execute(plan, dry_run=False)
            self.assertEqual([r.outcome for r in results], ["sent", "sent", "failed", "not-sent"])
            self.assertEqual(len(hourly.ledger.all_rows()), 2)        # no stray reservation
            self.assertTrue(hourly.sleeps and all(0 < s <= 2 for s in hourly.sleeps))
        finally:
            hourly.close()

    # 91
    def test_91_uploads_never_change_anything_else(self):
        self.upload([self.env.file("A.pdf"), self.env.file("B.docx")], folder="Week 13")
        self.upload([self.env.file("C.pdf")], publish=False)
        self.assertEqual(strip_files(self.env.fake.courses), strip_files(self.env.before))
        for cid, c in self.env.before.items():
            for f in c["files"]:
                self.assertIn(f, self.env.fake.courses[cid]["files"])     # existing files untouched
        for m, p, form in self.env.fake.writes:
            ok = (m == "POST" and p.endswith("/files") and form["on_duplicate"] == ["rename"]) or \
                 (m == "PUT" and p.startswith("/api/v1/files/") and sorted(form) == ["locked"])
            self.assertTrue(ok, (m, p, form))
        self.assertFalse(any("Authorization" in u["headers"] for u in self.env.fake.upload_requests))


# ========================================================== publish/unpublish
class PublishTests(unittest.TestCase):
    def setUp(self):
        self.env = FEnv()
        self.A = Target(101, "MA 121 - Section A")

    def tearDown(self):
        self.env.close()

    def ids(self, *names):
        return [self.env.assignment(101, n)["id"] for n in names]

    # 92
    def test_92_plan_skips_what_must_not_or_need_not_change(self):
        ids = self.ids("Bonus Problem Set", "Homework 1", "Quiz 5", "Bonus Problem Set") + [999999]
        plan = self.env.vc.build_plan(self.A, "assignment", ids, publish=False)
        rows = {r.name: r for r in plan.rows}
        self.assertEqual(len(plan.rows), 4)                         # duplicate id removed
        self.assertEqual(rows["Bonus Problem Set"].action, "change")
        self.assertIn("submitted work", rows["Homework 1"].reason)
        self.assertIn("quiz", rows["Quiz 5"].reason)
        self.assertIn("No longer exists", rows["#999999"].reason)
        plan = self.env.vc.build_plan(self.A, "assignment", self.ids("Homework 1"), publish=True)
        self.assertIn("Already published", plan.rows[0].reason)
        self.assertEqual(self.env.fake.writes, [])

    # 93
    def test_93_publish_and_unpublish_assignments_without_touching_grades(self):
        plan = self.env.vc.build_plan(self.A, "assignment", self.ids("Bonus Problem Set"), publish=False)
        r = self.env.vc.execute(plan, dry_run=False)[0]
        self.assertEqual(r.outcome, "changed", r.message)
        self.assertFalse(self.env.assignment(101, "Bonus Problem Set")["published"])
        plan = self.env.vc.build_plan(self.A, "assignment", self.ids("Final Project (draft)", "Bonus Problem Set"), True)
        self.assertEqual([x.outcome for x in self.env.vc.execute(plan, dry_run=False)], ["changed", "changed"])
        self.assertTrue(self.env.assignment(101, "Final Project (draft)")["published"])
        for cid, c in self.env.before.items():
            self.assertEqual(c["submissions"], self.env.fake.courses[cid]["submissions"])
            for a in c["assignments"]:
                now = next(x for x in self.env.fake.courses[cid]["assignments"] if x["id"] == a["id"])
                self.assertEqual({k: v for k, v in a.items() if k != "published"},
                                 {k: v for k, v in now.items() if k != "published"})
        self.assertTrue(all(sorted(f) == ["assignment[published]"] for _, _, f in self.env.fake.writes))

    # 94
    def test_94_files_publish_and_unpublish_via_locked_only(self):
        files = {f["display_name"]: f for f in self.env.fake.courses[101]["files"]}
        plan = self.env.vc.build_plan(self.A, "file", [files["Syllabus.pdf"]["id"], files["Lab Safety Rules.pdf"]["id"]],
                                      publish=False)
        self.assertEqual([r.action for r in plan.rows], ["change", "skip"])     # Lab Safety is already hidden
        self.env.vc.execute(plan, dry_run=False)
        self.assertTrue(files["Syllabus.pdf"]["locked"])
        plan = self.env.vc.build_plan(self.A, "file", [files["Syllabus.pdf"]["id"], files["Lab Safety Rules.pdf"]["id"]],
                                      publish=True)
        self.assertEqual([r.outcome for r in self.env.vc.execute(plan, dry_run=False)], ["changed", "changed"])
        self.assertFalse(files["Lab Safety Rules.pdf"]["locked"])
        self.assertTrue(all(m == "PUT" and sorted(f) == ["locked"] for m, _, f in self.env.fake.writes))
        self.assertEqual(files["Syllabus.pdf"]["display_name"], "Syllabus.pdf")

    # 95
    def test_95_item_changed_after_preview_is_skipped(self):
        plan = self.env.vc.build_plan(self.A, "assignment", self.ids("Bonus Problem Set"), publish=False)
        self.env.assignment(101, "Bonus Problem Set")["published"] = False       # someone else unpublished it
        r = self.env.vc.execute(plan, dry_run=False)[0]
        self.assertEqual(r.outcome, "skipped")
        self.assertIn("changed since the preview", r.message)
        self.assertEqual(self.env.fake.writes, [])

    # 96
    def test_96_refusal_continues_but_unclear_result_stops(self):
        a, b = self.ids("Final Project (draft)", "Bonus Problem Set")
        self.env.fake.add_assignment(101, "Extra Draft", published=False, submissions=False)
        c = self.ids("Extra Draft")[0]
        self.env.assignment(101, "Bonus Problem Set")["published"] = False
        plan = self.env.vc.build_plan(self.A, "assignment", [a, b, c], publish=True)
        self.env.fake.fail_writes = {1: 400}
        self.assertEqual([r.outcome for r in self.env.vc.execute(plan, dry_run=False)], ["failed", "changed", "changed"])
        for x in (a, b, c):
            next(i for i in self.env.fake.courses[101]["assignments"] if i["id"] == x)["published"] = False
        plan = self.env.vc.build_plan(self.A, "assignment", [a, b, c], publish=True)
        self.env.fake.fail_writes = {len(self.env.fake.writes) + 1: "timeout_before"}
        self.assertEqual([r.outcome for r in self.env.vc.execute(plan, dry_run=False)], ["unknown", "not-run", "not-run"])

    # 97
    def test_97_verification_catches_a_change_that_did_not_stick(self):
        fake = FakeCanvas.demo()

        def lying(method, url, headers, body):           # Canvas says OK but ignores the change
            if method == "PUT":
                fake.writes.append((method, url, {}))
                return Response(200, {}, b"{}")
            return fake(method, url, headers, body)
        env = FEnv(transport=lying)
        env.fake = fake
        try:
            env.vc = VisibilityCenter(SafeCanvasClient(SIM_URL, SIM_TOKEN, transport=lying, mode="publish"),
                                      env.ledger, FAST, sleep=lambda s: None)
            ids = [a["id"] for a in fake.courses[101]["assignments"] if a["name"] in ("Bonus Problem Set",)]
            fake.add_assignment(101, "Draft 2", published=False, submissions=False)
            plan = env.vc.build_plan(self.A, "assignment", ids, publish=False)
            r = env.vc.execute(plan, dry_run=False)[0]
            self.assertEqual(r.outcome, "verify-failed")
            self.assertIn("still published", r.message)
            self.assertEqual(env.ledger.visibility_changes()[0].status, "verify-failed")
        finally:
            env.close()

    # 98
    def test_98_every_change_is_recorded_and_can_be_undone(self):
        plan = self.env.vc.build_plan(self.A, "assignment", self.ids("Final Project (draft)"), publish=True)
        self.env.vc.execute(plan, dry_run=False)
        change = self.env.ledger.visibility_changes()[0]
        self.assertEqual((change.before, change.after, change.status), ("Unpublished", "Published", "changed"))
        undo = self.env.vc.build_undo_plan(change)
        self.assertEqual(undo.to_change[0].after, False)
        self.assertEqual(self.env.vc.execute(undo, dry_run=False)[0].outcome, "changed")
        self.assertFalse(self.env.assignment(101, "Final Project (draft)")["published"])
        self.assertEqual(len(self.env.ledger.visibility_changes()), 2)

    # 99
    def test_99_old_ledgers_upgrade_without_losing_history(self):
        path = self.env.dir / "v1.db"
        db = sqlite3.connect(path)
        db.executescript("""
            CREATE TABLE sends (id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('announcement', 'assignment')),
              source_course INTEGER NOT NULL, source_item INTEGER NOT NULL, target_course INTEGER NOT NULL,
              title TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
              created_item_id INTEGER, detail TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
              UNIQUE (kind, source_course, source_item, target_course));
            INSERT INTO sends VALUES (1, 'announcement', 101, 5, 102, 'Hi', 'sent', 9, NULL, 1, 1);
            INSERT INTO sends VALUES (2, 'assignment', 101, 6, 103, 'HW', 'pending', NULL, 'x', 1, 1);""")
        db.commit()
        db.close()
        from ccc.ledger import AlreadyRecorded, SendKey
        led = Ledger(path)
        try:
            self.assertEqual([(r.id, r.status) for r in sorted(led.all_rows(), key=lambda r: r.id)],
                             [(1, "sent"), (2, "pending")])
            for key in (SendKey("announcement", 101, 5, 102), SendKey("assignment", 101, 6, 103)):
                with self.assertRaises(AlreadyRecorded):
                    led.reserve(key, "x")                    # protection survived the upgrade
            led.reserve(SendKey("file", 0, 123, 102), "a.pdf")
            self.assertEqual(led.visibility_changes(), [])
        finally:
            led.close()
        Ledger(path).close()                                  # upgrading twice is harmless

    # 100
    def test_100_randomized_mixed_operations_with_faults(self):
        """Random mixes of uploads and publish changes with random failures:
        never a duplicate file, never a grade change, never a disallowed write,
        and the token never reaches the storage server."""
        rounds = int(os.environ.get("CCC_RANDOM_ROUNDS", "60"))
        for seed in range(rounds):
            with self.subTest(seed=seed):
                rnd = random.Random(seed)
                env = FEnv()
                try:
                    env.fake.upload_mode = rnd.choice(["json", "redirect", "created_location"])
                    names = [f"File {i}.pdf" for i in range(rnd.randint(1, 3))]
                    paths = [env.file(n, os.urandom(rnd.randint(1, 400))) for n in names]
                    for step in range(rnd.randint(2, 5)):
                        if rnd.random() < 0.4:
                            env.fake.fail_writes = {len(env.fake.writes) + rnd.randint(1, 3):
                                                    rnd.choice(["timeout", "timeout_before", 500, 403])}
                        if rnd.random() < 0.3:
                            env.fake.fail_uploads = {len(env.fake.upload_requests) + 1:
                                                     rnd.choice(["timeout", "timeout_before", 500, 400])}
                        if rnd.random() < 0.6:
                            plan = env.fc.build_plan(rnd.sample(paths, rnd.randint(1, len(paths))),
                                                     rnd.choice(["", "Week 1", "week 1"]),
                                                     env.targets(*rnd.sample([101, 102, 103, 105], rnd.randint(1, 4))),
                                                     rnd.random() < 0.5)
                            env.fc.execute(plan, dry_run=False)
                        else:
                            kind = rnd.choice(["assignment", "file"])
                            items = env.vc.list_items(kind, 101)
                            if items:
                                ids = [i["id"] for i in rnd.sample(items, rnd.randint(1, len(items)))]
                                env.vc.execute(env.vc.build_plan(self.A, kind, ids, rnd.random() < 0.5), dry_run=False)
                        env.fake.fail_writes, env.fake.fail_uploads = {}, {}
                    for cid, c in env.fake.courses.items():
                        self.assertEqual(c["submissions"], env.before[cid]["submissions"])
                        for n in names:
                            for folder in ("", "Week 1"):
                                self.assertLessEqual(len(env.files_named(cid, n, folder)), 1, (cid, n, folder))
                    self.assertFalse(any("Authorization" in u["headers"] for u in env.fake.upload_requests))
                    for m, p, form in env.fake.writes:
                        ok = ((m == "POST" and p.endswith("/files") and form.get("on_duplicate") == ["rename"]) or
                              (m == "PUT" and p.startswith("/api/v1/files/") and sorted(form) == ["locked"]) or
                              (m == "PUT" and "/assignments/" in p and sorted(form) == ["assignment[published]"]))
                        self.assertTrue(ok, (m, p, form))
                finally:
                    env.close()


if __name__ == "__main__":
    unittest.main()
