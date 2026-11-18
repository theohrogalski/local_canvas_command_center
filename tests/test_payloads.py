"""Tests 26-30: exactly what gets posted to Canvas."""

import unittest

from ccc.copier import PlanError, announcement_form, assignment_form, source_link_warning

SRC_ASSIGNMENT = {
    "id": 55, "name": "Homework 7", "description": "<p>Do it</p>", "points_possible": 10,
    "grading_type": "points", "submission_types": ["online_upload", "online_text_entry"],
    "allowed_extensions": ["pdf", "docx"], "due_at": "2026-11-24T04:59:00Z", "unlock_at": None,
    "lock_at": None, "published": True, "assignment_group_id": 77, "rubric": [{"id": 1}],
    "course_id": 101, "html_url": "https://x/courses/101/assignments/55",
}


class PayloadTests(unittest.TestCase):
    # 26
    def test_26_announcement_payload_is_exact(self):
        form = announcement_form({"id": 9, "title": " Week 12 ", "message": "<p>Hi</p>",
                                  "delayed_post_at": "2030-01-01", "lock_at": "x", "user_id": 5})
        self.assertEqual(form, [("title", "Week 12"), ("message", "<p>Hi</p>"),
                                ("is_announcement", "true"), ("published", "true")])

    # 27
    def test_27_assignment_payload_maps_fields_and_drops_course_specific_ids(self):
        form = assignment_form(SRC_ASSIGNMENT, publish=True)
        keys = [k for k, _ in form]
        d = dict(form)
        self.assertEqual(d["assignment[name]"], "Homework 7")
        self.assertEqual(d["assignment[points_possible]"], "10")
        self.assertEqual(d["assignment[due_at]"], "2026-11-24T04:59:00Z")
        self.assertEqual([v for k, v in form if k == "assignment[submission_types][]"],
                         ["online_upload", "online_text_entry"])
        self.assertEqual([v for k, v in form if k == "assignment[allowed_extensions][]"], ["pdf", "docx"])
        for forbidden in ("id", "assignment_group_id", "rubric", "course_id", "html_url", "unlock_at", "lock_at"):
            self.assertFalse(any(forbidden in k for k in keys), forbidden)
        self.assertTrue(all(k.startswith("assignment[") for k in keys))

    # 28
    def test_28_publish_flag_controls_visibility(self):
        self.assertIn(("assignment[published]", "true"), assignment_form(SRC_ASSIGNMENT, publish=True))
        self.assertIn(("assignment[published]", "false"), assignment_form(SRC_ASSIGNMENT, publish=False))

    # 29
    def test_29_unsupported_assignment_types_are_refused(self):
        cases = [dict(SRC_ASSIGNMENT, submission_types=["online_quiz"], quiz_id=3),
                 dict(SRC_ASSIGNMENT, submission_types=["discussion_topic"]),
                 dict(SRC_ASSIGNMENT, submission_types=["external_tool"]),
                 dict(SRC_ASSIGNMENT, submission_types=["student_annotation"]),
                 dict(SRC_ASSIGNMENT, name="   ")]
        for src in cases:
            with self.subTest(src=src.get("submission_types")), self.assertRaises(PlanError):
                assignment_form(src, publish=True)
        with self.assertRaises(PlanError):
            announcement_form({"title": "", "message": "x"})

    # 30
    def test_30_warns_about_links_into_the_source_course(self):
        self.assertIsNotNone(source_link_warning(
            {"description": '<a href="/courses/101/files/9/download">rubric</a>'}, 101))
        self.assertIsNotNone(source_link_warning({"message": "x", "attachments": [{"id": 1}]}, 101))
        self.assertIsNone(source_link_warning({"description": "/courses/1010/files/9"}, 101))
        self.assertIsNone(source_link_warning({"message": "plain text"}, 101))


if __name__ == "__main__":
    unittest.main()
