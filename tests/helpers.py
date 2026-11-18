"""Shared test fixtures: a fake Canvas, a temp ledger, and a wired-up CommandCenter."""

import tempfile
from pathlib import Path

from ccc.canvas_client import SafeCanvasClient
from ccc.config import Limits
from ccc.copier import CommandCenter, Target
from ccc.ledger import Ledger
from ccc.simulator import SIM_TOKEN, SIM_URL, FakeCanvas


class Env:
    def __init__(self, limits=None, page_size=2):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "ledger.db"
        self.fake = FakeCanvas.demo(page_size=page_size)
        self.before = self.fake.snapshot()
        self.ledger = Ledger(self.db_path)
        self.client = SafeCanvasClient(SIM_URL, SIM_TOKEN, transport=self.fake)
        self.sleeps = []
        self.cc = CommandCenter(self.client, self.ledger,
                                limits or Limits(min_seconds_between_posts=0.0),
                                sleep=self.sleeps.append)

    def targets(self, *ids):
        names = {c["id"]: c for c in self.fake.courses.values()}
        ids = ids or (102, 103, 104, 105)
        return [Target(i, names[i]["name"], names[i]["workflow_state"] == "available") for i in ids]

    def item_id(self, kind, title):
        coll = "announcements" if kind == "announcement" else "assignments"
        key = "title" if kind == "announcement" else "name"
        return next(i["id"] for i in self.fake.courses[101][coll] if i[key] == title)

    def copies(self, course_id, kind, title):
        coll = "announcements" if kind == "announcement" else "assignments"
        key = "title" if kind == "announcement" else "name"
        return sum(1 for i in self.fake.courses[course_id][coll] if i[key] == title)

    def posts(self):
        return [p for m, p in self.fake.requests if m == "POST"]

    def close(self):
        self.ledger.close()
        self.tmp.cleanup()
