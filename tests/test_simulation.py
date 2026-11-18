"""Tests 31-40: end-to-end simulation against the fake Canvas server.

These run the real CommandCenter, client, ledger and rate limiter; only the
network is replaced by ccc.simulator.FakeCanvas.
"""

import contextlib
import io
import re
import unittest

from ccc.canvas_client import ALLOWED_POST_PATHS
from ccc.config import Limits
from ccc.copier import PlanError

from helpers import Env

WEEK12 = "Welcome to Week 12"
HW7 = "Homework 7 - Integration by Parts"


class SimulationTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def plan(self, kind=None, title=WEEK12, targets=None, env=None):
        env = env or self.env
        kind = kind or "announcement"
        return env.cc.build_plan(kind, 101, env.item_id(kind, title), targets or env.targets())

    # 31
    def test_31_lists_only_courses_you_teach_across_pages(self):
        courses = self.env.client.list_teacher_courses()
        ids = sorted(c["id"] for c in courses)
        self.assertEqual(ids, [101, 102, 103, 104, 105])
        self.assertNotIn(900, ids)                       # student enrollment excluded

    # 32
    def test_32_dry_run_posts_nothing_and_records_nothing(self):
        results = self.env.cc.execute(self.plan(), dry_run=True)
        self.assertEqual({r.outcome for r in results}, {"dry-run"})
        self.assertEqual(self.env.posts(), [])
        self.assertEqual(self.env.ledger.all_rows(), [])
        self.assertEqual(self.env.fake.emails, [])

    # 33
    def test_33_real_send_creates_exactly_one_copy_per_target_and_notifies(self):
        results = self.env.cc.execute(self.plan(), dry_run=False)
        self.assertEqual([r.outcome for r in results], ["sent"] * 4)
        for cid in (102, 103, 104, 105):
            self.assertEqual(self.env.copies(cid, "announcement", WEEK12), 1)
        self.assertEqual(self.env.copies(101, "announcement", WEEK12), 1)   # source untouched
        emailed = sorted(e["course_id"] for e in self.env.fake.emails)
        self.assertEqual(emailed, [102, 103, 105])        # 104 is unpublished: no email
        self.assertEqual({r.status for r in self.env.ledger.all_rows()}, {"sent"})

    # 34
    def test_34_sending_the_same_item_again_makes_zero_posts(self):
        stale_plan = self.plan()
        self.env.cc.execute(stale_plan, dry_run=False)
        n = len(self.env.posts())
        fresh = self.plan()
        self.assertEqual(fresh.to_send, [])
        self.env.cc.execute(fresh, dry_run=False)
        results = self.env.cc.execute(stale_plan, dry_run=False)   # re-clicking an old preview
        self.assertEqual({r.outcome for r in results}, {"skipped"})
        self.assertEqual(len(self.env.posts()), n)
        for cid in (102, 103, 104, 105):
            self.assertEqual(self.env.copies(cid, "announcement", WEEK12), 1)

    # 35
    def test_35_item_with_same_title_already_in_target_is_skipped(self):
        plan = self.plan("assignment", HW7)
        rows = {r.target.course_id: r for r in plan.rows}
        self.assertEqual(rows[103].action, "skip")       # Section C already has Homework 7
        self.env.cc.execute(plan, dry_run=False)
        self.assertEqual(self.env.copies(103, "assignment", HW7), 1)
        # Also caught if it appears in Canvas between preview and send:
        plan2 = self.plan("assignment", "Lab Report 3", targets=self.env.targets(102))
        self.env.fake.add_assignment(102, "  lab report 3 ")
        results = self.env.cc.execute(plan2, dry_run=False)
        self.assertEqual(results[0].outcome, "skipped")

    # 36
    def test_36_source_course_and_repeated_targets_are_handled(self):
        plan = self.plan(targets=self.env.targets(101, 102, 102, 102))
        self.assertEqual([(r.target.course_id, r.action) for r in plan.rows], [(101, "skip"), (102, "send")])
        self.env.cc.execute(plan, dry_run=False)
        self.assertEqual(len(self.env.posts()), 1)
        with self.assertRaises(PlanError):
            self.env.cc.build_plan("announcement", 101, self.env.item_id("announcement", WEEK12), [])

    # 37
    def test_37_network_drop_after_create_stops_and_never_resends(self):
        self.env.fake.fail_next_post = "timeout"    # Canvas creates it, reply is lost
        results = self.env.cc.execute(self.plan(), dry_run=False)
        self.assertEqual([r.outcome for r in results], ["unknown", "not-sent", "not-sent", "not-sent"])
        self.assertEqual(len(self.env.posts()), 1)
        self.assertEqual(self.env.ledger.all_rows()[0].status, "pending")
        # Next attempt: course 102 is blocked by the ledger (it already has a copy)
        plan2 = self.plan()
        self.assertEqual({r.target.course_id: r.action for r in plan2.rows}[102], "skip")
        self.env.cc.execute(plan2, dry_run=False)
        for cid in (102, 103, 104, 105):
            self.assertEqual(self.env.copies(cid, "announcement", WEEK12), 1)

    # 38
    def test_38_definite_rejection_is_recorded_and_can_be_retried(self):
        self.env.fake.fail_next_post = 403
        results = self.env.cc.execute(self.plan(), dry_run=False)
        self.assertEqual([r.outcome for r in results], ["failed", "sent", "sent", "sent"])
        self.assertEqual(self.env.copies(102, "announcement", WEEK12), 0)
        retry = self.plan()
        self.assertEqual([r.target.course_id for r in retry.to_send], [102])
        self.env.cc.execute(retry, dry_run=False)
        self.assertEqual(self.env.copies(102, "announcement", WEEK12), 1)

    # 39
    def test_39_rate_limits_block_oversized_runs_and_stop_cleanly(self):
        small = Env(Limits(min_seconds_between_posts=0, max_posts_per_run=2))
        try:
            with self.assertRaises(PlanError):           # refused at preview
                self.plan(env=small)
            self.assertEqual(small.posts(), [])
        finally:
            small.close()
        hourly = Env(Limits(min_seconds_between_posts=5, max_posts_per_hour=2))
        try:
            results = hourly.cc.execute(self.plan(env=hourly), dry_run=False)
            self.assertEqual([r.outcome for r in results], ["sent", "sent", "not-sent", "not-sent"])
            self.assertEqual(len(hourly.posts()), 2)
            self.assertEqual(len(hourly.ledger.all_rows()), 2)    # no stray reservations
            self.assertTrue(hourly.sleeps and all(0 < s <= 5 for s in hourly.sleeps))
        finally:
            hourly.close()

    # 40
    def test_40_full_scenario_never_modifies_existing_canvas_content(self):
        from ccc import demo
        with contextlib.redirect_stdout(io.StringIO()):
            summary = demo.run()
        self.assertEqual(summary["methods"], ["GET", "POST"])
        self.assertTrue(summary["unchanged"])
        # And in this test's own environment, run every action type:
        self.env.cc.execute(self.plan(), dry_run=False)
        self.env.cc.execute(self.plan("assignment", HW7), dry_run=False)
        self.env.cc.execute(self.plan("assignment", "Lab Report 3"), dry_run=False)
        self.assertEqual({m for m, _ in self.env.fake.requests}, {"GET", "POST"})
        for path in self.env.posts():
            self.assertTrue(any(p.match(path) for p in ALLOWED_POST_PATHS), path)
            self.assertRegex(path, r"^/api/v1/courses/\d+/(discussion_topics|assignments)$")
        for cid, course in self.env.before.items():
            for coll in ("announcements", "assignments"):
                for original in course[coll]:
                    self.assertIn(original, self.env.fake.courses[cid][coll])
        titles = {}
        for cid, course in self.env.fake.courses.items():
            for coll, key in (("announcements", "title"), ("assignments", "name")):
                for i in course[coll]:
                    k = (cid, re.sub(r"\s+", " ", i[key]).strip().lower())
                    titles[k] = titles.get(k, 0) + 1
        self.assertEqual(max(titles.values()), 1)       # no course has any item twice


if __name__ == "__main__":
    unittest.main()
