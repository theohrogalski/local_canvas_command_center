"""The Grading tab (copy grade weighting between courses) and the grading part
of the History tab (with Undo). Uses the main App's helpers and styling."""

from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from .canvas_client import fmt_weight
from .copier import PlanError, Target
from .grading import GradingPlan

OUTCOME_TEXT = {
    "applied": "✓  Applied", "dry-run": "○  Dry run", "no-change": "✓  Already matches",
    "skipped": "–  Skipped", "not-run": "–  Not run", "failed": "✕  Failed",
    "unknown": "!  Check Canvas", "verify-failed": "✕  VERIFY FAILED",
}


class GradingTab:
    def __init__(self, app, tab):
        self.app = app
        self.plan: GradingPlan | None = None
        self.checked: set[int] = set()
        self.gen = 0
        self.scheme_ok = False
        self.cancel = threading.Event()
        self._build(tab)

    # ================================================================ build
    def _build(self, tab):
        a, px = self.app, self.app.px
        from .gui import ACCENT_SOFT, INK, MUTED, OUTCOME_COLORS, DANGER, SUCCESS, WARN
        self.colors = {"MUTED": MUTED, "DANGER": DANGER, "SUCCESS": SUCCESS}
        gap = px(16)
        tab.columnconfigure(0, weight=1, uniform="g")
        tab.columnconfigure(1, weight=1, uniform="g")
        tab.rowconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        # ---- 1: source
        outer, s = a._card(tab, 1, "Copy weighting from", "Set it up once in Canvas, then copy it here")
        outer.grid(row=0, column=0, sticky="nsew", padx=(0, gap // 2))
        s.columnconfigure(0, weight=1)
        s.rowconfigure(2, weight=1)
        ttk.Label(s, text="Source course", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        self.source_var = tk.StringVar()
        self.source_cb = ttk.Combobox(s, textvariable=self.source_var, state="readonly", font=a.f_body)
        self.source_cb.grid(row=1, column=0, sticky="ew", pady=(px(4), px(10)))
        self.source_cb.bind("<<ComboboxSelected>>", lambda e: self.load_scheme())
        frame, self.scheme_tree = a._table(s, ("weight", "count"), 4, show="tree headings", selectmode="none")
        self.scheme_tree.heading("#0", text="ASSIGNMENT GROUP", anchor="w")
        self.scheme_tree.heading("weight", text="WEIGHT", anchor="w")
        self.scheme_tree.heading("count", text="ASSIGNMENTS", anchor="w")
        self.scheme_tree.column("#0", width=px(260))
        self.scheme_tree.column("weight", width=px(100), stretch=False)
        self.scheme_tree.column("count", width=px(120), stretch=False)
        frame.grid(row=2, column=0, sticky="nsew")
        self.scheme_note = ttk.Label(s, text="Connect on the Send tab first.", style="Muted.TLabel")
        self.scheme_note.grid(row=3, column=0, sticky="w", pady=(px(8), 0))

        # ---- 2: targets
        outer, t = a._card(tab, 2, "Apply to", "Click a course to tick it")
        outer.grid(row=0, column=1, sticky="nsew", padx=(gap // 2, 0))
        t.columnconfigure(0, weight=1)
        t.rowconfigure(1, weight=1)
        bar = ttk.Frame(t, style="Card.TFrame")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, px(8)))
        self.count_lbl = ttk.Label(bar, text="0 courses selected", style="Muted.TLabel")
        self.count_lbl.pack(side="left")
        ttk.Button(bar, text="Select none", command=lambda: self.set_all(False)).pack(side="right")
        ttk.Button(bar, text="Select all", command=lambda: self.set_all(True)).pack(side="right", padx=px(8))
        frame, self.target_tree = a._table(t, ("state",), 4, selectmode="none", show="tree headings")
        self.target_tree.heading("#0", text="COURSE", anchor="w")
        self.target_tree.heading("state", text="STATUS", anchor="w")
        self.target_tree.column("#0", width=px(340))
        self.target_tree.column("state", width=px(120), stretch=False)
        self.target_tree.tag_configure("on", background=ACCENT_SOFT, foreground=INK)
        self.target_tree.tag_configure("unpublished", foreground=MUTED)
        frame.grid(row=1, column=0, sticky="nsew")
        self.target_tree.bind("<Button-1>", self.toggle)

        # ---- 3: review & apply
        outer, r = a._card(tab, 3, "Review and apply",
                           "Scores and submissions are never changed - this is verified after every course")
        outer.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(gap, 0))
        r.columnconfigure(0, weight=3)
        r.columnconfigure(1, weight=2)
        r.rowconfigure(1, weight=1)
        opts = ttk.Frame(r, style="Card.TFrame")
        opts.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.dry_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="  Dry run  (check everything, change nothing)", variable=self.dry_var,
                        style="Card.TCheckbutton", command=self.update_buttons).pack(side="left")
        self.apply_btn = ttk.Button(opts, text="Apply", style="Danger.TButton", command=self.apply)
        self.apply_btn.pack(side="right")
        self.preview_btn = ttk.Button(opts, text="Preview changes", style="Accent.TButton", command=self.preview)
        self.preview_btn.pack(side="right", padx=px(10))
        self.cancel_btn = ttk.Button(opts, text="Stop", command=self.cancel.set)

        frame, self.plan_tree = a._table(r, ("before", "after"), 4, show="tree headings")
        self.plan_tree.heading("#0", text="COURSE / CHANGE", anchor="w")
        self.plan_tree.heading("before", text="BEFORE", anchor="w")
        self.plan_tree.heading("after", text="AFTER", anchor="w")
        self.plan_tree.column("#0", width=px(420))
        self.plan_tree.column("before", width=px(150), stretch=False)
        self.plan_tree.column("after", width=px(150), stretch=False)
        for tag, color in OUTCOME_COLORS.items():
            self.plan_tree.tag_configure(tag, foreground=color)
        for tag, color in (("applied", SUCCESS), ("no-change", SUCCESS), ("not-run", MUTED),
                           ("verify-failed", DANGER), ("warn", WARN)):
            self.plan_tree.tag_configure(tag, foreground=color)
        self.plan_tree.tag_configure("course", font=a.f_bold)
        frame.grid(row=1, column=0, sticky="nsew", pady=(px(12), 0))
        box, self.log = a._inset_text(r, 3, a.f_mono, fg="#334155")
        box.grid(row=1, column=1, sticky="nsew", padx=(gap, 0), pady=(px(12), 0))
        self.progress = ttk.Progressbar(r, mode="determinate", style="Horizontal.TProgressbar")
        self.progress.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(px(12), 0))
        self.update_buttons()

    def build_history(self, parent):
        a, px = self.app, self.app.px
        from .gui import DANGER, SUCCESS, WARN
        outer, body = a._card(parent, "↺", "Grading changes",
                              "Every weighting change, with a snapshot you can restore")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        bar = ttk.Frame(body, style="Card.TFrame")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, px(8)))
        self.undo_btn = ttk.Button(bar, text="Undo selected (restore weighting)", style="Accent.TButton",
                                   command=self.start_restore)
        self.undo_btn.pack(side="right")
        cols = ("id", "when", "action", "course", "status", "changes", "verified", "detail")
        widths = (60, 150, 90, 240, 120, 90, 110, 420)
        frame, self.runs_tree = a._table(body, cols, 5, selectmode="browse")
        for col, w in zip(cols, widths):
            self.runs_tree.heading(col, text=col.upper(), anchor="w")
            self.runs_tree.column(col, width=px(w), anchor="w")
        self.runs_tree.tag_configure("applied", foreground=SUCCESS)
        for tag in ("failed", "verify-failed"):
            self.runs_tree.tag_configure(tag, foreground=DANGER)
        self.runs_tree.tag_configure("unknown", foreground=WARN)
        frame.grid(row=1, column=0, sticky="nsew")
        return outer

    # ============================================================ helpers
    def say(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S  ") + text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def update_buttons(self):
        a = self.app
        ok = a.gc is not None and not a.busy
        self.preview_btn.state(["!disabled" if ok else "disabled"])
        can = ok and self.plan is not None and bool(self.plan.to_change)
        self.apply_btn.state(["!disabled" if can else "disabled"])
        if self.dry_var.get():
            self.apply_btn.configure(text="Run dry run", style="Accent.TButton")
        else:
            n = len(self.plan.to_change) if self.plan else 0
            self.apply_btn.configure(text=f"Apply for real ({n})" if n else "Apply for real", style="Danger.TButton")
        self.source_cb.state(["readonly", "!disabled"] if not a.busy else ["disabled"])
        if hasattr(self, "undo_btn"):
            self.undo_btn.state(["!disabled" if ok else "disabled"])

    def invalidate(self):
        self.gen += 1
        if self.app.busy:
            return
        if self.plan is not None:
            self.plan = None
            self.plan_tree.delete(*self.plan_tree.get_children())
        self.update_buttons()

    # ======================================================= connection
    def on_connected(self, courses):
        self.courses = courses
        self.source_cb["values"] = [self.app._course_label(c) for c in courses]
        self.scheme_note.configure(text="Choose the course whose weighting you want to copy.")
        self.update_buttons()

    def source(self):
        i = self.source_cb.current()
        return self.courses[i] if i >= 0 else None

    def load_scheme(self):
        self.invalidate()
        src = self.source()
        if not src or self.app.busy:
            return
        self._fill_targets(src["id"])

        def work():
            try:
                scheme, state = self.app.gc.read_scheme(src["id"])
                return scheme, state, None
            except PlanError as e:
                return None, None, str(e)

        def done(res):
            scheme, state, err = res
            self.scheme_tree.delete(*self.scheme_tree.get_children())
            self.scheme_ok = scheme is not None
            if err:
                self.scheme_note.configure(text=err, foreground=self.colors["DANGER"])
                return
            counts = {}
            for a_ in state.assignments:
                counts[a_.get("assignment_group_id")] = counts.get(a_.get("assignment_group_id"), 0) + 1
            for g in state.groups:
                self.scheme_tree.insert("", "end", text="  " + g["name"],
                                        values=(f"{fmt_weight(g['group_weight'])}%", counts.get(g["id"], 0)))
            total = scheme.total
            self.scheme_note.configure(
                text=f"Total {fmt_weight(total)}%  ·  weighted grading is on  ·  "
                     f"{len(scheme.assignment_groups)} assignment name(s) to match",
                foreground=self.colors["SUCCESS"] if abs(total - 100) < 0.01 else self.colors["DANGER"])

        self.app.run_bg(work, done, "Reading weighting...")

    def _fill_targets(self, source_id):
        self.target_tree.delete(*self.target_tree.get_children())
        self.checked &= {c["id"] for c in self.courses if c["id"] != source_id}
        for c in self.courses:
            if c["id"] == source_id:
                continue
            state = "Published" if c.get("workflow_state") == "available" else "Unpublished"
            self.target_tree.insert("", "end", iid=str(c["id"]), text="  " + c["name"], values=(state,))
            self._paint(str(c["id"]))
        self._count()

    def _paint(self, row):
        on = int(row) in self.checked
        tags = ["on"] if on else (["unpublished"] if self.target_tree.item(row, "values")[0] == "Unpublished" else [])
        img = self.app.icons["check_on_sel"] if on else self.app.icons[("check", 0)]
        self.target_tree.item(row, image=img, tags=tags)

    def _count(self):
        n = len(self.checked)
        self.count_lbl.configure(text=f"{n} course{'s' if n != 1 else ''} selected")

    def toggle(self, event):
        if self.app.busy:
            return
        row = self.target_tree.identify_row(event.y)
        if not row:
            return
        self.checked.symmetric_difference_update({int(row)})
        self._paint(row)
        self._count()
        self.invalidate()

    def set_all(self, on):
        if self.app.busy:
            return
        for row in self.target_tree.get_children():
            (self.checked.add if on else self.checked.discard)(int(row))
            self._paint(row)
        self._count()
        self.invalidate()

    # =========================================================== preview
    def preview(self):
        a = self.app
        if a.busy or not a.gc:
            return
        src = self.source()
        if not src:
            messagebox.showinfo("Pick a source", "Choose the course whose weighting you want to copy.", parent=a.root)
            return
        targets = [Target(c["id"], c["name"], c.get("workflow_state") == "available")
                   for c in self.courses if c["id"] in self.checked]
        gen = self.gen
        self.app.run_bg(lambda: a.gc.build_copy_plan(src["id"], targets, src["name"]),
                        lambda plan: self._show_plan(plan, gen), "Building preview...")

    def _show_plan(self, plan, gen=None):
        if gen is not None and gen != self.gen:
            self.say("Selection changed while previewing - press Preview changes again.")
            return
        self.plan = plan
        t = self.plan_tree
        t.delete(*t.get_children())
        for cp in plan.courses:
            iid = str(cp.target.course_id)
            if cp.skip:
                t.insert("", "end", iid=iid, text="  " + cp.target.name, values=("Skip", cp.skip), tags=("skip", "course"))
                continue
            if not cp.changes:
                t.insert("", "end", iid=iid, text="  " + cp.target.name, values=("", "Already matches"),
                         tags=("no-change", "course"))
                continue
            t.insert("", "end", iid=iid, text=f"  {cp.target.name}   ·   {len(cp.changes)} change(s)",
                     values=("", ""), open=True, tags=("send", "course"))
            for ch in cp.changes:
                t.insert(iid, "end", text="  " + ch.what, values=(ch.before, ch.after))
            for w in cp.warnings:
                t.insert(iid, "end", text="  ! " + w, values=("", ""), tags=("warn",))
        title = "Undo" if plan.action == "restore" else "Copy"
        self.say(f"{title} preview: {len(plan.to_change)} course(s) to change, {plan.total_changes} edit(s).")
        self.update_buttons()

    # ============================================================= apply
    def apply(self):
        a = self.app
        if a.busy or not self.plan or not self.plan.to_change:
            return
        plan, dry = self.plan, self.dry_var.get()
        n = len(plan.to_change)
        if not dry:
            names = "\n".join(f"  - {cp.target.name}  ({len(cp.changes)} change(s))" for cp in plan.to_change)
            verb = "Restore the previous grade weighting in" if plan.action == "restore" else "Change grade weighting in"
            answer = simpledialog.askstring(
                "Confirm grade weighting change",
                f"{verb} {n} course(s):\n\n{names}\n\n"
                "Canvas will recalculate students' course totals.\n"
                "Individual scores, grades and submissions are NOT changed - this is checked after each course.\n"
                "A snapshot is saved first, so you can undo from the History tab.\n\n"
                f"Type {n} to confirm:", parent=a.root)
            if (answer or "").strip() != str(n):
                self.say("Cancelled (confirmation did not match).")
                return
        self.cancel.clear()
        self.progress.configure(maximum=len(plan.courses), value=0)
        self.cancel_btn.pack(side="right", padx=px_(a, 8))

        def on_result(r):
            a.q.put(lambda: self._show_result(r))

        def done(results):
            counts = {}
            for r in results:
                counts[r.outcome] = counts.get(r.outcome, 0) + 1
            self.say("Finished: " + ", ".join(f"{v} {k}" for k, v in counts.items()) + ".")
            self.cancel_btn.pack_forget()
            self.plan = None
            self.update_buttons()
            a.refresh_history()
            bad = [r for r in results if r.outcome in ("failed", "unknown", "verify-failed")]
            if bad:
                messagebox.showwarning("Please check", "\n\n".join(f"{r.target.name}: {r.message}" for r in bad),
                                       parent=a.root)

        a.run_bg(lambda: a.gc.execute(plan, dry_run=dry, progress=on_result, cancel=self.cancel),
                 done, "Dry run..." if dry else "Applying weighting...")

    def _show_result(self, r):
        iid = str(r.target.course_id)
        if self.plan_tree.exists(iid):
            self.plan_tree.item(iid, values=(OUTCOME_TEXT.get(r.outcome, r.outcome), r.message),
                                tags=(r.outcome, "course"))
        self.progress.step(1)
        self.say(f"{OUTCOME_TEXT.get(r.outcome, r.outcome)}  {r.target.name}: {r.message}")

    # ======================================================= history/undo
    def refresh_history(self):
        if not self.app.ledger or not hasattr(self, "runs_tree"):
            return
        self.runs_tree.delete(*self.runs_tree.get_children())
        self._runs = {r.id: r for r in self.app.ledger.grading_runs()}
        for r in self._runs.values():
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.created_at))
            self.runs_tree.insert("", "end", iid=str(r.id), tags=(r.status,), values=(
                r.id, when, r.action, r.course_name, r.status, r.changes, f"{r.submissions_checked} subs",
                r.detail or ""))

    def start_restore(self):
        a = self.app
        sel = self.runs_tree.selection()
        if a.busy or not a.gc:
            return
        if not sel:
            messagebox.showinfo("Pick a row", "Select the grading change you want to undo.", parent=a.root)
            return
        run = self._runs[int(sel[0])]
        target = Target(run.course_id, run.course_name)

        def done(plan):
            a.nb.select(a.grading_frame)
            self._show_plan(plan)
            self.say(f"Undo preview for {run.course_name} (restores the state before change #{run.id}). "
                     "Review it, then Apply.")

        a.run_bg(lambda: a.gc.build_restore_plan(run.snapshot_path, target), done, "Building undo preview...")


def px_(app, n):
    return app.px(n)
