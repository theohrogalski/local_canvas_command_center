"""Tests 101-103: the scrolling fixes, run against the real GUI.

These need a display (they run in a hidden window, nothing appears on screen).
Without a display they are skipped and say so."""

import os
import time
import unittest

try:
    import tkinter as tk
    _root = tk.Tk()
    _root.destroy()
    HAVE_DISPLAY = True
except Exception:
    HAVE_DISPLAY = False


@unittest.skipUnless(HAVE_DISPLAY, "no display available for GUI tests")
class ScrollTests(unittest.TestCase):
    def setUp(self):
        from ccc import gui
        self.gui = gui
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = gui.App(self.root, simulate=True)
        self.app.connect()
        self.pump()

    def tearDown(self):
        self.app.on_close()

    def pump(self, sec=0.2):
        end = time.time() + sec
        while time.time() < end or self.app.busy:
            self.root.update()
            time.sleep(0.01)

    # 101
    def test_101_mouse_wheel_over_a_course_dropdown_never_changes_the_course(self):
        for cb in (self.app.source_cb, self.app.grading.source_cb, self.app.publish.course_cb):
            cb.current(0)
            before = cb.get()
            for seq in ("<Button-4>", "<Button-5>"):
                cb.event_generate(seq)
            cb.event_generate("<MouseWheel>", delta=-120)
            self.pump()
            self.assertEqual(cb.get(), before)
            self.assertNotIn("combobox::Scroll", self.root.bind_class("TCombobox", "<Button-5>"))
        self.assertEqual(self.app.source_course()["id"], 101)

    # 102
    def test_102_one_wheel_notch_scrolls_one_row(self):
        frame, tree = self.app._table(self.root, ("a",), 4)
        for i in range(40):
            tree.insert("", "end", iid=str(i), values=(i,))
        calls = []
        tree.yview_scroll = lambda n, what: calls.append((n, what))   # record what the wheel asks for
        tree.event_generate("<Button-5>")       # Linux: wheel down
        tree.event_generate("<Button-4>")       # Linux: wheel up
        tree.event_generate("<MouseWheel>", delta=-120)   # Windows: one notch down
        tree.event_generate("<MouseWheel>", delta=240)    # Windows: two notches up
        tree.event_generate("<MouseWheel>", delta=-3)     # macOS: small delta still moves one row
        self.assertEqual(calls, [(1, "units"), (-1, "units"), (1, "units"), (-2, "units"), (1, "units")])

    # 103
    def test_103_scrollbar_appears_only_when_a_list_overflows(self):
        holder = tk.Frame(self.root)
        sb = tk.Scrollbar(holder)
        sb.grid(row=0, column=0)
        setter = self.gui.autohide_scrollbar(sb)
        setter("0.0", "1.0")
        self.assertEqual(sb.winfo_manager(), "")
        setter("0.0", "0.4")
        self.assertEqual(sb.winfo_manager(), "grid")
        self.assertEqual(tuple(round(x, 2) for x in sb.get()), (0.0, 0.4))
        setter("0.0", "1.0")
        self.assertEqual(sb.winfo_manager(), "")


if __name__ == "__main__":
    unittest.main()
