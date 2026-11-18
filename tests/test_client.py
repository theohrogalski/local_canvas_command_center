"""Tests 1-13: the restricted Canvas client (connection & request safety)."""

import unittest

from ccc.canvas_client import (CanvasError, Response, SafeCanvasClient, SafetyViolation,
                               _NoRedirect, validate_base_url, validate_token)
from ccc.simulator import SIM_TOKEN, SIM_URL, FakeCanvas


class Recorder:
    """Transport that records calls and returns a canned response."""

    def __init__(self, response=None, exc=None):
        self.calls = []
        self.response = response or Response(200, {}, b"{}")
        self.exc = exc

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if self.exc:
            raise self.exc
        return self.response


def client(transport):
    return SafeCanvasClient("https://school.instructure.com", "tok-123", transport=transport)


class ClientTests(unittest.TestCase):
    # 1
    def test_01_base_url_is_normalised(self):
        self.assertEqual(validate_base_url(" https://School.Instructure.com/ "), "https://school.instructure.com")
        self.assertEqual(validate_base_url("https://canvas.edu:8443"), "https://canvas.edu:8443")

    # 2
    def test_02_plain_http_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_base_url("http://school.instructure.com")

    # 3
    def test_03_paths_queries_and_credentials_rejected(self):
        for bad in ("https://school.instructure.com/courses/1", "https://school.instructure.com?x=1",
                    "https://user:pw@school.instructure.com", "https://", "school.instructure.com"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_base_url(bad)

    # 4
    def test_04_token_validation(self):
        for bad in ("", "   ", "abc def", "abc\ndef", "abc\r\nX-Evil: 1"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_token(bad)
        self.assertEqual(validate_token("  good~token  "), "good~token")

    # 5
    def test_05_put_patch_delete_blocked_before_network(self):
        t = Recorder()
        c = client(t)
        for method in ("PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            with self.subTest(method=method), self.assertRaises(SafetyViolation):
                c._request(method, "/api/v1/courses/1/assignments")
        self.assertEqual(t.calls, [])

    # 6
    def test_06_post_only_to_allowlisted_create_endpoints(self):
        t = Recorder()
        c = client(t)
        blocked = ["/api/v1/courses/1/assignments/5",            # edit an assignment
                   "/api/v1/courses/1",                          # edit course
                   "/api/v1/courses/1/discussion_topics/9",      # edit topic
                   "/api/v1/courses/1/assignments/5/submissions",
                   "/api/v1/courses/1/enrollments",
                   "/api/v1/accounts/1/courses"]
        for path in blocked:
            with self.subTest(path=path), self.assertRaises(SafetyViolation):
                c._request("POST", path, form=[("x", "1")])
        self.assertEqual(t.calls, [])
        c.create_assignment(7, [("assignment[name]", "A")])
        c.create_announcement(7, [("title", "T")])
        self.assertEqual([x[0] for x in t.calls], ["POST", "POST"])

    # 7
    def test_07_path_traversal_and_non_api_paths_rejected(self):
        t = Recorder()
        c = client(t)
        for path in ("/api/v1/courses/1/assignments/../../../accounts", "/login", "/api/v2/x",
                     "/api/v1/courses/1/assignments%2F..%2F"):
            with self.subTest(path=path), self.assertRaises(SafetyViolation):
                c._request("GET", path)
        self.assertEqual(t.calls, [])

    # 8
    def test_08_pagination_follows_link_headers(self):
        fake = FakeCanvas.demo(page_size=2)        # 5 teacher courses -> 3 pages
        c = SafeCanvasClient(SIM_URL, SIM_TOKEN, transport=fake)
        courses = c.list_teacher_courses()
        self.assertEqual(sorted(x["id"] for x in courses), [101, 102, 103, 104, 105])
        self.assertEqual(sum(1 for m, p in fake.requests if p == "/api/v1/courses"), 3)

    # 9
    def test_09_pagination_refuses_foreign_host(self):
        evil = Response(200, {"Link": '<https://evil.example.com/api/v1/courses?page=2>; rel="next"'}, b"[]")
        t = Recorder(evil)
        with self.assertRaises(SafetyViolation):
            client(t).list_teacher_courses()
        self.assertEqual(len(t.calls), 1)          # never contacted the foreign host

    # 10
    def test_10_token_only_in_header_and_redacted(self):
        t = Recorder()
        c = client(t)
        c.whoami()
        method, url, headers, body = t.calls[0]
        self.assertNotIn("tok-123", url)
        self.assertEqual(headers["Authorization"], "Bearer tok-123")
        self.assertNotIn("tok-123", repr(c))
        self.assertNotIn("tok-123", str(vars(c).get("base_url")))

    # 11
    def test_11_http_errors_are_definite(self):
        c = client(Recorder(Response(401, {}, b'{"errors":[{"message":"Invalid access token."}]}')))
        with self.assertRaises(CanvasError) as cm:
            c.whoami()
        self.assertEqual(cm.exception.status, 401)
        self.assertFalse(cm.exception.ambiguous)
        c = client(Recorder(Response(403, {}, b"{}")))
        with self.assertRaises(CanvasError) as cm:
            c.create_assignment(1, [])
        self.assertFalse(cm.exception.ambiguous)

    # 12
    def test_12_post_5xx_and_network_errors_are_ambiguous(self):
        with self.assertRaises(CanvasError) as cm:
            client(Recorder(Response(502, {}, b""))).create_announcement(1, [])
        self.assertTrue(cm.exception.ambiguous)
        with self.assertRaises(CanvasError) as cm:
            client(Recorder(exc=TimeoutError("timed out"))).create_announcement(1, [])
        self.assertTrue(cm.exception.ambiguous)
        with self.assertRaises(CanvasError) as cm:   # a failed GET is never "ambiguous"
            client(Recorder(exc=TimeoutError("timed out"))).whoami()
        self.assertFalse(cm.exception.ambiguous)

    # 13
    def test_13_redirects_are_never_followed(self):
        h = _NoRedirect()
        self.assertIsNone(h.redirect_request(None, None, 302, "Found", {}, "https://elsewhere/"))
        c = client(Recorder(Response(302, {"Location": "https://elsewhere/"}, b"")))
        with self.assertRaises(CanvasError) as cm:
            c.create_assignment(1, [])
        self.assertEqual(cm.exception.status, 302)


if __name__ == "__main__":
    unittest.main()
