"""Command line entry point.

  python3 -m ccc                 open the GUI (connect to your real Canvas)
  python3 -m ccc gui --simulate  open the GUI against a fake Canvas (safe to explore)
  python3 -m ccc simulate        run the scripted end-to-end simulation in the terminal
  python3 -m ccc simulate-grading  narrated simulation of copying grade weighting
  python3 -m ccc ledger          list everything this tool has sent
  python3 -m ccc release ID      forget ledger row ID (only after checking Canvas!)
"""

from __future__ import annotations

import argparse
import sys
import time

from .config import app_dir
from .ledger import Ledger


def _ledger_cmd():
    ledger = Ledger(app_dir() / "ledger.db")
    rows = ledger.all_rows()
    print("== Announcements & assignments sent ==")
    if not rows:
        print("(none yet)")
    for r in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.updated_at))
        print(f"#{r.id:<4} {when}  {r.status:<7} {r.kind:<12} course {r.source_course} -> "
              f"{r.target_course}  {r.title!r}" + (f"  [{r.detail}]" if r.detail else ""))
    print("\n== Grading weighting changes ==")
    runs = ledger.grading_runs()
    if not runs:
        print("(none yet)")
    for g in runs:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(g.created_at))
        print(f"#{g.id:<4} {when}  {g.action:<8} {g.status:<14} {g.course_name} ({g.course_id}): "
              f"{g.changes} edit(s), {g.submissions_checked} submissions checked")
        print(f"       snapshot: {g.snapshot_path}")


def _release_cmd(row_id: int):
    ledger = Ledger(app_dir() / "ledger.db")
    row = next((r for r in ledger.all_rows() if r.id == row_id), None)
    if not row:
        print(f"No ledger row #{row_id}.")
        return 1
    print(f"Row #{row.id}: {row.status} {row.kind} {row.title!r} -> course {row.target_course}")
    print("Releasing lets this item be sent to that course AGAIN.")
    print("Only do this after confirming in Canvas that the item is NOT there.")
    if input("Type RELEASE to confirm: ").strip() != "RELEASE":
        print("Cancelled.")
        return 1
    ledger.release(row_id)
    print("Released.")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="ccc", description="Canvas Command Center")
    sub = p.add_subparsers(dest="cmd")
    g = sub.add_parser("gui", help="open the graphical app (default)")
    g.add_argument("--simulate", action="store_true", help="use a fake in-memory Canvas")
    sub.add_parser("simulate", help="run the scripted simulation in the terminal")
    sub.add_parser("simulate-grading", help="narrated simulation of copying grade weighting")
    sub.add_parser("ledger", help="show the send history")
    r = sub.add_parser("release", help="forget one ledger row (after checking Canvas)")
    r.add_argument("row_id", type=int)
    args = p.parse_args(argv)

    if args.cmd == "simulate":
        from .demo import run
        run()
        return 0
    if args.cmd == "simulate-grading":
        from .demo import run_grading
        run_grading()
        return 0
    if args.cmd == "ledger":
        _ledger_cmd()
        return 0
    if args.cmd == "release":
        return _release_cmd(args.row_id)
    from .gui import launch
    return launch(simulate=getattr(args, "simulate", False))


if __name__ == "__main__":
    sys.exit(main())
