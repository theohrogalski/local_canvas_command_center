"""Tests 41-70: grade weighting (Grading mode).

The two non-negotiables tested over and over:
  * every submission and grade is left exactly as it was
  * only the four permitted weighting edits are ever sent
"""

import copy
import os
import random
import re
import tempfile
import unittest
from pathlib import Path

from ccc.canvas_client import SafeCanvasClient, SafetyViolation, check_grading_request
from ccc.config import Limits
from ccc.copier import CommandCenter, PlanError, Target
from ccc.grading import (GradingCenter, Scheme, desired_from_scheme, norm, read_state,
                         scheme_from_state, SkipCourse)
from ccc.ledger import Ledger
from ccc.simulator import SIM_TOKEN, SIM_URL, FakeCanvas

FAST = Limits(min_seconds_between_posts=0, max_posts_per_run=400, max_posts_per_hour=100000)
ALLOWED_WRITES = (
    ("PUT", re.compile(r"^/api/v1/courses/\d+$"), ["course[apply_assignment_group_weights]"]),
    ("POST", re.compile(r"^/api/v1/courses/\d+/assignment_groups$"), ["group_weight", "name"]),
    ("PUT", re.compile(r"^/api/v1/courses/\d+/assignment_groups/\d+$"), ["group_weight"]),
    ("PUT", re.compile(r"^/api/v1/courses/\d+/assignments/\d+$"), ["assignment[assignment_group_id]"]),
)


class GEnv:
    def __init__(self, fake=None, limits=FAST):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = fake or FakeCanvas.demo()
        self.before = self.fake.snapshot()
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.db")
        self.client = SafeCanvasClient(SIM_URL, SIM_TOKEN, transport=self.fake, mode="grading")
        self.sleeps = []
        self.snap_dir = Path(self.tmp.name) / "snapshots"
        self.gc = GradingCenter(self.client, self.ledger, self.snap_dir, limits, sleep=self.sleeps.append)

    def targets(self, *ids):
        return [Target(i, self.fake.courses[i]["name"], self.fake.courses[i]["workflow_state"] == "available")
                for i in (ids or (102, 103, 104, 105))]

    def course(self, cid):
        return self.fake.courses[cid]

    def weights(self, cid):
        return {g["name"]: g["group_weight"] for g in self.course(cid)["assignment_groups"]}

    def group_of(self, cid, assignment_name):
        c = self.course(cid)
        a = next(a for a in c["assignments"] if a["name"] == assignment_name)
        return next(g["name"] for g in c["assignment_groups"] if g["id"] == a["assignment_group_id"])

    def writes_to(self, cid):
        return [w for w in self.fake.writes if w[1].startswith(f"/api/v1/courses/{cid}") and
                (w[1] == f"/api/v1/courses/{cid}" or w[1].startswith(f"/api/v1/courses/{cid}/"))]

    def close(self):
        self.ledger.close()
        self.tmp.cleanup()


def assert_grades_untouched(tc, before, after):
    """Every submission identical; every assignment identical except its group."""
    for cid in before:
        tc.assertEqual(before[cid]["submissions"], after[cid]["submissions"], f"submissions changed in {cid}")
        b = {a["id"]: {k: v for k, v in a.items() if k != "assignment_group_id"} for a in before[cid]["assignments"]}
        a = {a["id"]: {k: v for k, v in a.items() if k != "assignment_group_id"} for a in after[cid]["assignments"]}
        tc.assertEqual(b, a, f"assignment details changed in {cid}")
        tc.assertEqual(before[cid]["announcements"], after[cid]["announcements"])


def assert_only_allowed_writes(tc, fake):
    tc.assertTrue(all(m in ("GET", "POST", "PUT") for m, _ in fake.requests))
    for method, path, form in fake.writes:
        ok = any(method == m and p.match(path) and sorted(form) == keys for m, p, keys in ALLOWED_WRITES)
        tc.assertTrue(ok, f"unexpected write {method} {path} {sorted(form)}")


def assert_no_groups_deleted(tc, before, after):
    for cid in before:
        ids_before = {g["id"]: g["name"] for g in before[cid]["assignment_groups"]}
        ids_after = {g["id"]: g["name"] for g in after[cid]["assignment_groups"]}
        for gid, name in ids_before.items():
            tc.assertEqual(ids_after.get(gid), name, f"group {name} deleted/renamed in {cid}")


# ===================================================================== client
class GradingClientTests(unittest.TestCase):
    def client(self, mode):
        self.calls = []
        transport = lambda m, u, h, b: (self.calls.append((m, u, b)) or __import__("ccc.canvas_client",
                                        fromlist=["Response"]).Response(200, {}, b"{}"))
        return SafeCanvasClient("https://school.instructure.com", "tok", transport=transport, mode=mode)

    # 41
    def test_41_only_four_edits_with_exact_fields_are_allowed(self):
        ok = [("PUT", "/api/v1/courses/5", [("course[apply_assignment_group_weights]", "true")]),
              ("POST", "/api/v1/courses/5/assignment_groups", [("name", "Exams"), ("group_weight", "55")]),
              ("PUT", "/api/v1/courses/5/assignment_groups/9", [("group_weight", "12.5")]),
              ("PUT", "/api/v1/courses/5/assignments/9", [("assignment[assignment_group_id]", "3")])]
        for m, p, f in ok:
            check_grading_request(m, p, f)
        bad = [
            ("PUT", "/api/v1/courses/5", [("course[name]", "x")]),
            ("PUT", "/api/v1/courses/5", [("course[apply_assignment_group_weights]", "true"), ("course[name]", "x")]),
            ("PUT", "/api/v1/courses/5/assignments/9", [("assignment[points_possible]", "0")]),
            ("PUT", "/api/v1/courses/5/assignments/9", [("assignment[assignment_group_id]", "3"),
                                                        ("assignment[due_at]", "")]),
            ("PUT", "/api/v1/courses/5/assignment_groups/9", [("group_weight", "10"), ("rules[drop_lowest]", "2")]),
            ("PUT", "/api/v1/courses/5/assignment_groups/9", [("name", "Renamed")]),
            ("POST", "/api/v1/courses/5/assignment_groups", [("name", "X")]),
            ("PUT", "/api/v1/courses/5/assignment_groups/9", [("group_weight", "1"), ("group_weight", "2")]),
        ]
        for m, p, f in bad:
            with self.subTest(form=f), self.assertRaises(SafetyViolation):
                check_grading_request(m, p, f)

    # 42
    def test_42_values_are_validated(self):
        bad = [("PUT", "/api/v1/courses/5", [("course[apply_assignment_group_weights]", "yes")]),
               ("PUT", "/api/v1/courses/5/assignment_groups/9", [("group_weight", "-5")]),
               ("PUT", "/api/v1/courses/5/assignment_groups/9", [("group_weight", "abc")]),
               ("PUT", "/api/v1/courses/5/assignment_groups/9", [("group_weight", "1e9")]),
               ("PUT", "/api/v1/courses/5/assignment_groups/9", [("group_weight", "5000")]),
               ("PUT", "/api/v1/courses/5/assignments/9", [("assignment[assignment_group_id]", "3; DROP")]),
               ("POST", "/api/v1/courses/5/assignment_groups", [("name", "   "), ("group_weight", "5")]),
               ("POST", "/api/v1/courses/5/assignment_groups", [("name", "a\nb"), ("group_weight", "5")])]
        for m, p, f in bad:
            with self.subTest(form=f), self.assertRaises(SafetyViolation):
                check_grading_request(m, p, f)

    # 43
    def test_43_grading_mode_cannot_touch_grades_submissions_or_content(self):
        c = self.client("grading")
        attempts = [
            ("PUT", "/api/v1/courses/5/assignments/9/submissions/77", [("submission[posted_grade]", "100")]),
            ("POST", "/api/v1/courses/5/assignments/9/submissions/update_grades", [("grade_data[1][posted_grade]", "1")]),
            ("PUT", "/api/v1/courses/5/submissions/update_grades", [("x", "1")]),
            ("POST", "/api/v1/courses/5/discussion_topics", [("title", "t")]),
            ("POST", "/api/v1/courses/5/assignments", [("assignment[name]", "x")]),
            ("PUT", "/api/v1/courses/5/enrollments/3", [("x", "1")]),
            ("DELETE", "/api/v1/courses/5/assignment_groups/9", []),
            ("DELETE", "/api/v1/courses/5/assignments/9", []),
            ("PATCH", "/api/v1/courses/5", []),
        ]
        for m, p, f in attempts:
            with self.subTest(path=p, method=m), self.assertRaises(SafetyViolation):
                c._request(m, p, form=f)
        self.assertEqual(self.calls, [])

    # 44
    def test_44_send_mode_cannot_make_any_grading_edit(self):
        c = self.client("send")
        with self.assertRaises(SafetyViolation):
            c.set_weighting(5, True)
        with self.assertRaises(SafetyViolation):
            c.set_group_weight(5, 9, 10)
        with self.assertRaises(SafetyViolation):
            c.move_assignment(5, 9, 3)
        with self.assertRaises(SafetyViolation):
            c.create_assignment_group(5, "Exams", 10)
        self.assertEqual(self.calls, [])
        with self.assertRaises(ValueError):
            GradingCenter(c, None, "/tmp/x")       # grading service refuses a send-mode client


# ================================================================== planning
class GradingPlanTests(unittest.TestCase):
    def setUp(self):
        self.env = GEnv()

    def tearDown(self):
        self.env.close()

    def plan(self, *ids):
        return self.env.gc.build_copy_plan(101, self.env.targets(*ids))

    # 45
    def test_45_source_must_use_weighting_and_have_unique_group_names(self):
        self.env.course(101)["apply_assignment_group_weights"] = False
        with self.assertRaises(PlanError):
            self.plan(102)
        self.env.course(101)["apply_assignment_group_weights"] = True
        self.env.fake.add_group(101, " homework ", 0)
        with self.assertRaises(PlanError):
            self.plan(102)

    # 46
    def test_46_fresh_course_plan_is_create_then_move_then_switch_on(self):
        cp = self.plan(102).courses[0]
        ops = [c.op for c in cp.changes]
        self.assertEqual(ops, ["create_group"] * 3 + ["move_assignment"] * 3 + ["set_weighting"])
        self.assertEqual([c.args["name"] for c in cp.changes[:3]], ["Homework", "Labs", "Exams"])
        self.assertEqual(cp.changes[-1].args, {"on": True})

    # 47
    def test_47_existing_groups_only_get_their_weights_changed(self):
        cp = self.plan(103).courses[0]
        weights = {c.what: (c.before, c.after) for c in cp.changes if c.op == "set_weight"}
        self.assertEqual(weights, {"Weight of 'Homework'": ("30%", "20%"), "Weight of 'Labs'": ("30%", "25%"),
                                   "Weight of 'Exams'": ("40%", "55%")})
        self.assertNotIn("create_group", [c.op for c in cp.changes])
        self.assertNotIn("set_weighting", [c.op for c in cp.changes])

    # 48
    def test_48_matching_course_needs_no_changes(self):
        cp = self.plan(105).courses[0]
        self.assertIsNone(cp.skip)
        self.assertEqual(cp.changes, [])

    # 49
    def test_49_extra_weighted_group_skips_course_but_zero_weight_extra_is_fine(self):
        cp = self.plan(104).courses[0]
        self.assertIn("110%", cp.skip)
        self.assertIn("Extra Credit", cp.skip)
        g = next(g for g in self.env.course(104)["assignment_groups"] if g["name"] == "Extra Credit")
        g["group_weight"] = 0.0
        cp = self.plan(104).courses[0]
        self.assertIsNone(cp.skip)
        self.assertTrue(cp.changes)

    # 50
    def test_50_ambiguous_names_are_never_guessed(self):
        self.env.fake.add_group(103, "LABS", 0)              # two "labs" groups in the target
        self.assertIn("Two groups named", self.plan(103).courses[0].skip)
        self.env.fake.add_assignment(102, "homework 1")       # duplicate assignment name in target
        cp = self.plan(102).courses[0]
        moved = [c.what for c in cp.changes if c.op == "move_assignment"]
        self.assertFalse(any("omework 1" in m for m in moved))
        self.assertTrue(any("more than one assignment" in w for w in cp.warnings))

    # 51
    def test_51_matching_ignores_case_and_spaces_and_warns_about_uncounted_work(self):
        for g in self.env.course(103)["assignment_groups"]:
            g["name"] = "  " + g["name"].upper() + " "
        cp = self.plan(103).courses[0]
        self.assertNotIn("create_group", [c.op for c in cp.changes])
        cp102 = self.plan(102).courses[0]
        self.assertTrue(any("Reading Quiz" in w for w in cp102.warnings))

    # 52
    def test_52_source_drop_rules_are_flagged_not_copied(self):
        g = next(g for g in self.env.course(101)["assignment_groups"] if g["name"] == "Homework")
        g["rules"] = {"drop_lowest": 1}
        cp = self.plan(103).courses[0]
        self.assertTrue(any("drop rules" in w for w in cp.warnings))
        self.assertFalse(any("rules" in str(c.args) for c in cp.changes))

    # 53
    def test_53_preview_reads_only_and_oversized_runs_are_refused(self):
        self.plan(102, 103, 104, 105)
        self.assertEqual(self.env.fake.writes, [])
        small = GEnv(limits=Limits(0, 5, 1000))
        try:
            with self.assertRaises(PlanError):
                small.gc.build_copy_plan(101, small.targets(102, 103))   # 7 + 4 edits > 5
            self.assertEqual(small.fake.writes, [])
        finally:
            small.close()


# ================================================================= execution
class GradingExecutionTests(unittest.TestCase):
    def setUp(self):
        self.env = GEnv()

    def tearDown(self):
        self.env.close()

    def run_copy(self, *ids, dry_run=False):
        plan = self.env.gc.build_copy_plan(101, self.env.targets(*ids))
        return plan, self.env.gc.execute(plan, dry_run=dry_run)

    # 54
    def test_54_dry_run_changes_nothing(self):
        _, results = self.run_copy(dry_run=True)
        self.assertEqual([r.outcome for r in results], ["dry-run", "dry-run", "skipped", "no-change"])
        self.assertEqual(self.env.fake.writes, [])
        self.assertEqual(self.env.fake.snapshot(), self.env.before)
        self.assertEqual(list(self.env.snap_dir.glob("*")) if self.env.snap_dir.exists() else [], [])

    # 55
    def test_55_apply_reaches_the_source_weighting_and_is_idempotent(self):
        _, results = self.run_copy()
        self.assertEqual([r.outcome for r in results], ["applied", "applied", "skipped", "no-change"])
        for cid in (102, 103):
            self.assertTrue(self.env.course(cid)["apply_assignment_group_weights"])
            w = self.env.weights(cid)
            self.assertEqual((w["Homework"], w["Labs"], w["Exams"]), (20.0, 25.0, 55.0))
            self.assertEqual(self.env.group_of(cid, "Midterm Exam"), "Exams")
            self.assertEqual(self.env.group_of(cid, "Lab Report 1"), "Labs")
        self.assertEqual(self.env.group_of(102, "Reading Quiz"), "Assignments")   # not in source: untouched
        n = len(self.env.fake.writes)
        plan2, results2 = self.run_copy()
        self.assertEqual(plan2.total_changes, 0)
        self.assertEqual(len(self.env.fake.writes), n)

    # 56
    def test_56_every_grade_and_submission_is_preserved_and_verified(self):
        _, results = self.run_copy()
        assert_grades_untouched(self, self.env.before, self.env.fake.courses)
        applied = [r for r in results if r.outcome == "applied"]
        self.assertTrue(all(r.submissions_checked == 20 for r in applied))
        self.assertTrue(all("verified unchanged" in r.message for r in applied))

    # 57
    def test_57_only_allowed_writes_reach_canvas_and_nothing_is_deleted(self):
        self.run_copy()
        assert_only_allowed_writes(self, self.env.fake)
        assert_no_groups_deleted(self, self.env.before, self.env.fake.courses)
        self.assertEqual(self.env.writes_to(104), [])          # skipped course untouched
        self.assertEqual(self.env.writes_to(105), [])
        self.assertEqual(self.env.writes_to(101), [])          # source untouched
        self.assertEqual(self.env.fake.courses[101], self.env.before[101])

    # 58
    def test_58_course_changed_after_preview_is_skipped(self):
        plan = self.env.gc.build_copy_plan(101, self.env.targets(102, 103))
        g = next(g for g in self.env.course(103)["assignment_groups"] if g["name"] == "Labs")
        g["group_weight"] = 33.0                               # someone edits Canvas meanwhile
        results = self.env.gc.execute(plan, dry_run=False)
        self.assertEqual([r.outcome for r in results], ["applied", "skipped"])
        self.assertIn("changed since the preview", results[1].message)
        self.assertEqual(self.env.writes_to(103), [])

    # 59
    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_59_snapshot_is_saved_first_private_and_holds_no_student_data(self):
        self.env.fake.fail_writes = {1: 500}                   # first write fails...
        _, results = self.run_copy(102)
        snap = Path(results[0].snapshot_path)                 # ...but the snapshot already exists
        self.assertTrue(snap.exists())
        self.assertEqual(os.stat(snap).st_mode & 0o777, 0o600)
        text = snap.read_text()
        for word in ("score", "grade", "user_id", "submission", "7000"):
            self.assertNotIn(word, text)
        self.assertIn("Midterm Exam", text)

    # 60
    def test_60_undo_restores_the_original_setup_exactly(self):
        self.run_copy(102, 103)
        for run in self.env.ledger.grading_runs():
            t = Target(run.course_id, run.course_name)
            plan = self.env.gc.build_restore_plan(run.snapshot_path, t)
            r = self.env.gc.execute(plan, dry_run=False)[0]
            self.assertEqual(r.outcome, "applied", r.message)
        for cid in (102, 103):
            b, a = self.env.before[cid], self.env.course(cid)
            self.assertEqual(a["apply_assignment_group_weights"], b["apply_assignment_group_weights"])
            self.assertEqual({x["id"]: x["assignment_group_id"] for x in a["assignments"]},
                             {x["id"]: x["assignment_group_id"] for x in b["assignments"]})
            orig = {g["id"]: g["group_weight"] for g in b["assignment_groups"]}
            for g in a["assignment_groups"]:
                self.assertEqual(g["group_weight"], orig.get(g["id"], 0.0))   # created groups -> 0%
        assert_grades_untouched(self, self.env.before, self.env.fake.courses)
        assert_no_groups_deleted(self, self.env.before, self.env.fake.courses)
        self.assertEqual({r.action for r in self.env.ledger.grading_runs()}, {"apply", "restore"})

    # 61
    def test_61_refused_edit_stops_the_run_and_can_be_finished_or_undone(self):
        hw = next(a for a in self.env.course(102)["assignments"] if a["name"] == "Lab Report 1")
        self.env.fake.locked_assignments.add(hw["id"])        # closed grading period
        _, results = self.run_copy(102, 103)
        self.assertEqual([r.outcome for r in results], ["failed", "not-run"])
        self.assertIn("refused", results[0].message)
        self.assertEqual(self.env.writes_to(103), [])
        assert_grades_untouched(self, self.env.before, self.env.fake.courses)
        self.env.fake.locked_assignments.clear()
        _, results = self.run_copy(102, 103)                   # finish the job
        self.assertEqual([r.outcome for r in results], ["applied", "applied"])
        first = sorted(self.env.ledger.grading_runs(), key=lambda r: r.id)[0]
        r = self.env.gc.execute(self.env.gc.build_restore_plan(first.snapshot_path, Target(102, "B")), False)[0]
        self.assertEqual(r.outcome, "applied")
        self.assertEqual({x["id"]: x["assignment_group_id"] for x in self.env.course(102)["assignments"]},
                         {x["id"]: x["assignment_group_id"] for x in self.env.before[102]["assignments"]})

    # 62
    def test_62_lost_reply_never_creates_a_duplicate_group(self):
        self.env.fake.fail_writes = {2: "timeout"}            # 'Labs' created, reply lost
        _, results = self.run_copy(102, 103)
        self.assertEqual([r.outcome for r in results], ["unknown", "not-run"])
        self.assertIn("may or may not", results[0].message)
        _, results = self.run_copy(102, 103)
        self.assertEqual([r.outcome for r in results], ["applied", "applied"])
        names = [norm(g["name"]) for g in self.env.course(102)["assignment_groups"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names.count("labs"), 1)

    # 63
    def test_63_verification_catches_any_grade_change_and_stops(self):
        self.env.fake.corrupt_on_move = True                   # simulate Canvas changing a score
        _, results = self.run_copy(102, 103)
        self.assertEqual(results[0].outcome, "verify-failed")
        self.assertIn("submissions/grades differ", results[0].message)
        self.assertEqual(results[1].outcome, "not-run")
        self.assertEqual(self.env.writes_to(103), [])
        # The fingerprint catches every grade-relevant change, but not the clock ticking.
        from ccc.grading import submissions_fingerprint as fp
        base = {"id": 1, "score": 5.0, "excused": False, "late_policy_status": None, "seconds_late": 10,
                "workflow_state": "graded", "attachments": [{"id": 3}]}
        self.assertEqual(fp([base]), fp([dict(base, seconds_late=999999)]))
        for change in ({"score": 5.5}, {"excused": True}, {"late_policy_status": "missing"},
                       {"workflow_state": "submitted"}, {"attachments": [{"id": 4}]}, {"grade": "A"}):
            self.assertNotEqual(fp([base]), fp([dict(base, **change)]), change)
        self.assertNotEqual(fp([base]), fp([base, dict(base, id=2)]))      # a new submission

    # 64
    def test_64_grading_edits_are_spaced_and_have_their_own_budget(self):
        env = GEnv(limits=Limits(min_seconds_between_posts=0.5, max_posts_per_run=400, max_posts_per_hour=1000))
        try:
            send_env_ledger = env.ledger
            for _ in range(40):                                # announcement budget fully used
                send_env_ledger.record_attempt(channel="post")
            plan = env.gc.build_copy_plan(101, env.targets(102))
            r = env.gc.execute(plan, dry_run=False)[0]
            self.assertEqual(r.outcome, "applied")             # grading unaffected by post budget
            self.assertTrue(env.sleeps and all(0 < s <= 0.5 for s in env.sleeps))
            self.assertEqual(env.ledger.attempts_since(0, "grading"), 7)
            self.assertEqual(env.ledger.attempts_since(0, "post"), 40)
        finally:
            env.close()

    # 65
    def test_65_hourly_edit_cap_stops_mid_course_cleanly(self):
        env = GEnv(limits=Limits(min_seconds_between_posts=0, max_posts_per_run=400, max_posts_per_hour=4))
        try:
            plan = env.gc.build_copy_plan(101, env.targets(102))
            r = env.gc.execute(plan, dry_run=False)[0]
            self.assertEqual(r.outcome, "failed")
            self.assertEqual(r.changes_done, 4)
            self.assertIn("Hourly limit", r.message)
            assert_grades_untouched(self, env.before, env.fake.courses)
            self.assertTrue(Path(r.snapshot_path).exists())     # undo still possible
        finally:
            env.close()

    # 66
    def test_66_send_features_still_cannot_edit(self):
        send_client = SafeCanvasClient(SIM_URL, SIM_TOKEN, transport=self.env.fake)   # default: send mode
        cc = CommandCenter(send_client, self.env.ledger, Limits(0), sleep=lambda s: None)
        with self.assertRaises(SafetyViolation):
            send_client.set_group_weight(102, 500, 10)
        self.assertEqual(cc.client.mode, "send")
        self.assertEqual(self.env.fake.writes, [])

    # 67
    def test_67_old_ledger_files_are_upgraded_safely(self):
        import sqlite3
        path = Path(self.env.tmp.name) / "old.db"
        db = sqlite3.connect(path)
        db.executescript("CREATE TABLE post_attempts (id INTEGER PRIMARY KEY, ts REAL NOT NULL);"
                         "INSERT INTO post_attempts (ts) VALUES (100.0);")
        db.commit()
        db.close()
        led = Ledger(path)
        self.assertEqual(led.attempts_since(0, "post"), 1)    # old rows count as announcement posts
        self.assertEqual(led.attempts_since(0, "grading"), 0)
        led.record_attempt(200.0, "grading")
        self.assertEqual(led.last_attempt("grading"), 200.0)
        led.close()


# ============================================================ randomized
GROUP_POOL = ["Homework", "Labs", "Exams", "Quizzes", "Projects", "Participation", "Assignments"]
ASSIGNMENT_POOL = [f"{kind} {n}" for kind in ("HW", "Lab", "Quiz", "Exam", "Essay") for n in range(1, 7)]


def random_world(rnd: random.Random, n_targets: int):
    fc = FakeCanvas(page_size=rnd.choice([2, 3, 100]))
    fc.SUBMITTERS_PER_COURSE = rnd.randint(1, 4)
    # Source course
    fc.add_course(1, "Source", students=rnd.randint(0, 6), weighted=True)
    names = rnd.sample(GROUP_POOL[:-1], rnd.randint(1, 5))
    cuts = sorted(rnd.sample(range(1, 100), len(names) - 1))
    weights = [b - a for a, b in zip([0] + cuts, cuts + [100])]
    if rnd.random() < 0.2:
        weights[0] += rnd.choice([5, 12.5])                    # source not summing to 100
    for name, w in zip(names, weights):
        fc.add_group(1, name, w, rules={"drop_lowest": 1} if rnd.random() < 0.1 else None)
    for a in rnd.sample(ASSIGNMENT_POOL, rnd.randint(1, 12)):
        fc.add_assignment(1, a, group=rnd.choice(names + ["Assignments"]), points_possible=rnd.choice([5, 10, 100]))
    # Targets
    for t in range(2, 2 + n_targets):
        fc.add_course(t, f"Target {t}", students=rnd.randint(0, 6), weighted=rnd.random() < 0.5)
        for name in rnd.sample(GROUP_POOL[:-1], rnd.randint(0, 5)):
            shown = rnd.choice([name, name.upper(), f"  {name.lower()} "])
            fc.add_group(t, shown, rnd.choice([0, 0, 10, 20, 33.3]) if rnd.random() < 0.3 else 0)
        if rnd.random() < 0.05:
            fc.add_group(t, rnd.choice(names), 0)               # occasional duplicate group name
        group_names = [g["name"] for g in fc.courses[t]["assignment_groups"]]
        for a in rnd.sample(ASSIGNMENT_POOL, rnd.randint(0, 10)):
            fc.add_assignment(t, rnd.choice([a, a.lower()]), group=rnd.choice(group_names))
        if rnd.random() < 0.1 and fc.courses[t]["assignments"]:
            dup = fc.courses[t]["assignments"][0]["name"]
            fc.add_assignment(t, dup, group=rnd.choice(group_names))
    return fc


class RandomizedGradingTests(unittest.TestCase):
    ROUNDS = int(os.environ.get("CCC_RANDOM_ROUNDS", "150"))

    def check_target_matches_scheme(self, fc, scheme, cid):
        c = fc.courses[cid]
        self.assertTrue(c["apply_assignment_group_weights"])
        by_name = {norm(g["name"]): g for g in c["assignment_groups"]}
        for name, w, _ in scheme.groups:
            self.assertAlmostEqual(by_name[norm(name)]["group_weight"], w, places=4)
        counts = {}
        for a in c["assignments"]:
            counts[norm(a["name"])] = counts.get(norm(a["name"]), 0) + 1
        gname = {g["id"]: norm(g["name"]) for g in c["assignment_groups"]}
        for a in c["assignments"]:
            n = norm(a["name"])
            if n in scheme.assignment_groups and counts[n] == 1:
                self.assertEqual(gname[a["assignment_group_id"]], norm(scheme.assignment_groups[n]))

    # 68
    def test_68_randomized_courses_apply_verify_and_undo(self):
        """Hundreds of random course setups: grades never change, only allowed
        writes are sent, results match the source, a re-run changes nothing,
        and undo restores every original setting."""
        stats = {"applied": 0, "skipped": 0, "no-change": 0}
        for seed in range(self.ROUNDS):
            with self.subTest(seed=seed):
                rnd = random.Random(seed)
                env = GEnv(random_world(rnd, rnd.randint(1, 4)))
                try:
                    targets = env.targets(*[c for c in env.fake.courses if c != 1])
                    scheme, _ = env.gc.read_scheme(1)
                    plan = env.gc.build_copy_plan(1, targets)
                    results = env.gc.execute(plan, dry_run=False)
                    for r in results:
                        self.assertIn(r.outcome, ("applied", "skipped", "no-change"), r.message)
                        stats[r.outcome] += 1
                        if r.outcome in ("applied", "no-change"):
                            self.check_target_matches_scheme(env.fake, scheme, r.target.course_id)
                        if r.outcome == "skipped":
                            self.assertEqual(env.fake.courses[r.target.course_id],
                                             env.before[r.target.course_id])
                    assert_grades_untouched(self, env.before, env.fake.courses)
                    assert_only_allowed_writes(self, env.fake)
                    assert_no_groups_deleted(self, env.before, env.fake.courses)
                    self.assertEqual(env.fake.courses[1], env.before[1])
                    self.assertEqual(env.gc.build_copy_plan(1, targets).total_changes, 0)   # idempotent
                    for run in env.ledger.grading_runs():                                   # undo all
                        rp = env.gc.build_restore_plan(run.snapshot_path, Target(run.course_id, run.course_name))
                        self.assertEqual(env.gc.execute(rp, dry_run=False)[0].outcome, "applied")
                    for cid, b in env.before.items():
                        a = env.fake.courses[cid]
                        self.assertEqual(a["apply_assignment_group_weights"], b["apply_assignment_group_weights"])
                        self.assertEqual({x["id"]: x["assignment_group_id"] for x in a["assignments"]},
                                         {x["id"]: x["assignment_group_id"] for x in b["assignments"]})
                        orig = {g["id"]: g["group_weight"] for g in b["assignment_groups"]}
                        for g in a["assignment_groups"]:
                            self.assertAlmostEqual(g["group_weight"], orig.get(g["id"], 0.0), places=4)
                    assert_grades_untouched(self, env.before, env.fake.courses)
                finally:
                    env.close()
        self.assertGreater(stats["applied"], self.ROUNDS // 2)   # the generator exercises real work
        self.assertGreater(stats["skipped"], 0)

    # 69
    def test_69_random_failures_at_random_moments_never_harm_grades(self):
        """Inject a timeout, dropped connection, server error or refusal at a
        random edit. Grades stay intact, no duplicate groups appear, a second
        run finishes the job, and undo returns to the original."""
        failures = ["timeout", "timeout_before", 500, 503, 403, 400, 429]
        for seed in range(self.ROUNDS):
            with self.subTest(seed=seed):
                rnd = random.Random(10_000 + seed)
                env = GEnv(random_world(rnd, rnd.randint(1, 3)))
                try:
                    targets = env.targets(*[c for c in env.fake.courses if c != 1])
                    plan = env.gc.build_copy_plan(1, targets)
                    if plan.total_changes == 0:
                        continue
                    env.fake.fail_writes = {rnd.randint(1, plan.total_changes): rnd.choice(failures)}
                    results = env.gc.execute(plan, dry_run=False)
                    bad = [r for r in results if r.outcome in ("failed", "unknown")]
                    self.assertEqual(len(bad), 1)
                    after_bad = results[results.index(bad[0]) + 1:]
                    self.assertTrue(all(r.outcome in ("not-run", "skipped") for r in after_bad))
                    assert_grades_untouched(self, env.before, env.fake.courses)
                    assert_only_allowed_writes(self, env.fake)
                    for cid, c in env.fake.courses.items():
                        names = [norm(g["name"]) for g in c["assignment_groups"]]
                        before_names = [norm(g["name"]) for g in env.before[cid]["assignment_groups"]]
                        for n in set(names):
                            self.assertLessEqual(names.count(n), max(1, before_names.count(n)), n)
                    # second run converges
                    plan2 = env.gc.build_copy_plan(1, targets)
                    self.assertTrue(all(r.outcome in ("applied", "skipped", "no-change")
                                        for r in env.gc.execute(plan2, dry_run=False)))
                    self.assertEqual(env.gc.build_copy_plan(1, targets).total_changes, 0)
                    # undo each course from its FIRST snapshot (the original state)
                    first = {}
                    for run in sorted(env.ledger.grading_runs(), key=lambda r: r.id):
                        first.setdefault(run.course_id, run)
                    for run in first.values():
                        rp = env.gc.build_restore_plan(run.snapshot_path, Target(run.course_id, run.course_name))
                        self.assertEqual(env.gc.execute(rp, dry_run=False)[0].outcome, "applied")
                    for cid, b in env.before.items():
                        a = env.fake.courses[cid]
                        self.assertEqual({x["id"]: x["assignment_group_id"] for x in a["assignments"]},
                                         {x["id"]: x["assignment_group_id"] for x in b["assignments"]})
                        self.assertEqual(a["apply_assignment_group_weights"], b["apply_assignment_group_weights"])
                    assert_grades_untouched(self, env.before, env.fake.courses)
                finally:
                    env.close()

    # 70
    def test_70_preview_never_writes_and_scheme_reflects_source_exactly(self):
        for seed in range(self.ROUNDS):
            with self.subTest(seed=seed):
                rnd = random.Random(20_000 + seed)
                fc = random_world(rnd, 2)
                env = GEnv(fc)
                try:
                    scheme, state = env.gc.read_scheme(1)
                    self.assertEqual([(n, w) for n, w, _ in scheme.groups],
                                     [(g["name"], g["group_weight"]) for g in fc.courses[1]["assignment_groups"]])
                    gname = {g["id"]: g["name"] for g in fc.courses[1]["assignment_groups"]}
                    for a in fc.courses[1]["assignments"]:
                        self.assertEqual(scheme.assignment_groups[norm(a["name"])], gname[a["assignment_group_id"]])
                    env.gc.build_copy_plan(1, env.targets(2, 3))
                    self.assertEqual(fc.writes, [])
                    self.assertEqual(fc.snapshot(), env.before)
                finally:
                    env.close()


if __name__ == "__main__":
    unittest.main()
