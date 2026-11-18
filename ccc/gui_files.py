"""The Files tab (upload files to courses) and the Publish tab (publish /
unpublish assignments and files), plus their parts of the History tab.
They reuse the main App's helpers (cards, tables, background runner, icons)."""

from __future__ import annotations

import os
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from .copier import PlanError, Target
from .files import folder_label
from .visibility import state_word

RESULT_TEXT = {
    "send": "✓  Upload", "skip": "–  Skip", "sent": "✓  Uploaded", "dry-run": "○  Dry run",
    "skipped": "–  Skipped", "failed": "✕  Failed", "unknown": "!  Check Canvas",
    "not-sent": "–  Not sent", "not-run": "–  Not run", "verify-failed": "✕  VERIFY FAILED",
    "change": "→  Change", "changed": "✓  Changed",
}


def human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


class CourseChecklist:
    """A course list where clicking a row ticks it (same look as the Send tab)."""

    def __init__(self, app, parent, on_change, height=4):
        from .gui import ACCENT_SOFT, INK, MUTED
        self.app, self.on_change, self.checked, self.courses = app, on_change, set(), []
        px = app.px
        bar = ttk.Frame(parent, style="Card.TFrame")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, px(8)))
        self.count = ttk.Label(bar, text="0 courses selected", style="Muted.TLabel")
        self.count.pack(side="left")
        ttk.Button(bar, text="Select none", command=lambda: self.set_all(False)).pack(side="right")
        ttk.Button(bar, text="Select all", command=lambda: self.set_all(True)).pack(side="right", padx=px(8))
        frame, self.tree = app._table(parent, ("state",), height, selectmode="none", show="tree headings")
        self.tree.heading("#0", text="COURSE", anchor="w")
        self.tree.heading("state", text="STATUS", anchor="w")
        self.tree.column("#0", width=px(320))
        self.tree.column("state", width=px(120), stretch=False)
        self.tree.tag_configure("on", background=ACCENT_SOFT, foreground=INK)
        self.tree.tag_configure("unpublished", foreground=MUTED)
        frame.grid(row=1, column=0, sticky="nsew")
        self.tree.bind("<Button-1>", self._click)
        self.tree.bind("<space>", self._key)
        self.tree.bind("<Return>", self._key)

    def fill(self, courses):
        self.courses = courses
        y = self.tree.yview()[0]
        self.tree.delete(*self.tree.get_children())
        self.checked &= {c["id"] for c in courses}
        for c in courses:
            state = "Published" if c.get("workflow_state") == "available" else "Unpublished"
            self.tree.insert("", "end", iid=str(c["id"]), text="  " + c["name"], values=(state,))
            self._paint(str(c["id"]))
        self.tree.yview_moveto(y)
        self._count()

    def targets(self):
        return [Target(c["id"], c["name"], c.get("workflow_state") == "available")
                for c in self.courses if c["id"] in self.checked]

    def _paint(self, row):
        on = int(row) in self.checked
        tags = ["on"] if on else (["unpublished"] if self.tree.item(row, "values")[0] == "Unpublished" else [])
        self.tree.item(row, image=self.app.icons["check_on_sel"] if on else self.app.icons[("check", 0)], tags=tags)

    def _count(self):
        n = len(self.checked)
        self.count.configure(text=f"{n} course{'s' if n != 1 else ''} selected")

    def toggle(self, row):
        if self.app.busy or not row:
            return
        self.checked.symmetric_difference_update({int(row)})
        self._paint(row)
        self._count()
        self.on_change()

    def _click(self, event):
        self.toggle(self.tree.identify_row(event.y))

    def _key(self, event):
        self.toggle(self.tree.focus())
        return "break"

    def set_all(self, on):
        if self.app.busy:
            return
        for row in self.tree.get_children():
            (self.checked.add if on else self.checked.discard)(int(row))
            self._paint(row)
        self._count()
        self.on_change()


# ======================================================================= Files
class FilesTab:
    def __init__(self, app, tab):
        self.app = app
        self.paths: list[str] = []
        self.plan = None
        self.gen = 0
        self.cancel = threading.Event()
        self._build(tab)

    def _build(self, tab):
        a, px = self.app, self.app.px
        from .gui import OUTCOME_COLORS, DANGER, SUCCESS, MUTED, WARN
        gap = px(16)
        tab.columnconfigure(0, weight=1, uniform="f")
        tab.columnconfigure(1, weight=1, uniform="f")
        tab.rowconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        outer, c = a._card(tab, 1, "Choose files", "From your computer")
        outer.grid(row=0, column=0, sticky="nsew", padx=(0, gap // 2))
        c.columnconfigure(0, weight=1)
        c.rowconfigure(1, weight=1)
        bar = ttk.Frame(c, style="Card.TFrame")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, px(8)))
        ttk.Button(bar, text="Add files…", style="Accent.TButton", command=self.add_files).pack(side="left")
        ttk.Button(bar, text="Clear", command=self.clear).pack(side="right")
        ttk.Button(bar, text="Remove selected", command=self.remove_selected).pack(side="right", padx=px(8))
        frame, self.files_tree = a._table(c, ("size",), 3, show="tree headings", selectmode="extended")
        self.files_tree.heading("#0", text="FILE", anchor="w")
        self.files_tree.heading("size", text="SIZE", anchor="w")
        self.files_tree.column("#0", width=px(300))
        self.files_tree.column("size", width=px(110), stretch=False)
        frame.grid(row=1, column=0, sticky="nsew")
        ttk.Label(c, text="Folder in each course  (optional, e.g. Week 12/Handouts; blank = top folder)",
                  style="Field.TLabel").grid(row=2, column=0, sticky="w", pady=(px(10), 0))
        self.folder_var = tk.StringVar()
        e = ttk.Entry(c, textvariable=self.folder_var, font=a.f_body)
        e.grid(row=3, column=0, sticky="ew", pady=(px(4), 0))
        self.folder_var.trace_add("write", lambda *_: self.invalidate())
        self.publish_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(c, text="  Publish files  (students can see them right away)", variable=self.publish_var,
                        style="Card.TCheckbutton", command=self.invalidate).grid(row=4, column=0, sticky="w",
                                                                                pady=(px(8), 0))

        outer, t = a._card(tab, 2, "Upload to", "Click a course to tick it")
        outer.grid(row=0, column=1, sticky="nsew", padx=(gap // 2, 0))
        t.columnconfigure(0, weight=1)
        t.rowconfigure(1, weight=1)
        self.targets = CourseChecklist(a, t, self.invalidate)

        outer, r = a._card(tab, 3, "Review and upload",
                           "Nothing is ever overwritten - a course that already has the file is skipped")
        outer.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(gap, 0))
        r.columnconfigure(0, weight=3)
        r.columnconfigure(1, weight=2)
        r.rowconfigure(1, weight=1)
        opts = ttk.Frame(r, style="Card.TFrame")
        opts.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.dry_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="  Dry run  (check everything, upload nothing)", variable=self.dry_var,
                        style="Card.TCheckbutton", command=self.update_buttons).pack(side="left")
        self.go_btn = ttk.Button(opts, text="Upload", style="Danger.TButton", command=self.go)
        self.go_btn.pack(side="right")
        self.preview_btn = ttk.Button(opts, text="Preview plan", style="Accent.TButton", command=self.preview)
        self.preview_btn.pack(side="right", padx=px(10))
        self.cancel_btn = ttk.Button(opts, text="Stop", command=self.cancel.set)
        frame, self.plan_tree = a._table(r, ("result", "detail"), 4, show="tree headings")
        self.plan_tree.heading("#0", text="COURSE / FILE", anchor="w")
        self.plan_tree.heading("result", text="RESULT", anchor="w")
        self.plan_tree.heading("detail", text="DETAILS", anchor="w")
        self.plan_tree.column("#0", width=px(300))
        self.plan_tree.column("result", width=px(150), stretch=False)
        self.plan_tree.column("detail", width=px(360))
        for tag, color in OUTCOME_COLORS.items():
            self.plan_tree.tag_configure(tag, foreground=color)
        for tag, color in (("verify-failed", DANGER), ("not-run", MUTED), ("warn", WARN), ("sent", SUCCESS)):
            self.plan_tree.tag_configure(tag, foreground=color)
        self.plan_tree.tag_configure("course", font=a.f_bold)
        frame.grid(row=1, column=0, sticky="nsew", pady=(px(12), 0))
        box, self.log = a._inset_text(r, 3, a.f_mono, fg="#334155")
        box.grid(row=1, column=1, sticky="nsew", padx=(gap, 0), pady=(px(12), 0))
        self.progress = ttk.Progressbar(r, mode="determinate", style="Horizontal.TProgressbar")
        self.progress.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(px(12), 0))
        self.update_buttons()

    # ------------------------------------------------------------ helpers
    def say(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S  ") + text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def update_buttons(self):
        a = self.app
        ok = a.fc is not None and not a.busy
        self.preview_btn.state(["!disabled" if ok else "disabled"])
        can = ok and self.plan is not None and bool(self.plan.to_send)
        self.go_btn.state(["!disabled" if can else "disabled"])
        if self.dry_var.get():
            self.go_btn.configure(text="Run dry run", style="Accent.TButton")
        else:
            n = len(self.plan.to_send) if self.plan else 0
            self.go_btn.configure(text=f"Upload for real ({n})" if n else "Upload for real", style="Danger.TButton")

    def invalidate(self):
        self.gen += 1
        if self.app.busy:
            return
        if self.plan is not None:
            self.plan = None
            self.plan_tree.delete(*self.plan_tree.get_children())
        self.update_buttons()

    def on_connected(self, courses):
        self.targets.fill(courses)
        self.update_buttons()

    def _refresh_files(self):
        self.files_tree.delete(*self.files_tree.get_children())
        for i, p in enumerate(self.paths):
            try:
                size = human_size(os.path.getsize(p))
            except OSError:
                size = "missing"
            self.files_tree.insert("", "end", iid=str(i), text="  " + os.path.basename(p), values=(size,))
        self.invalidate()

    def add_files(self):
        if self.app.busy:
            return
        chosen = filedialog.askopenfilenames(parent=self.app.root, title="Choose files to upload")
        for p in chosen:
            if p not in self.paths:
                self.paths.append(p)
        self._refresh_files()

    def remove_selected(self):
        if self.app.busy:
            return
        drop = {int(i) for i in self.files_tree.selection()}
        self.paths = [p for i, p in enumerate(self.paths) if i not in drop]
        self._refresh_files()

    def clear(self):
        if self.app.busy:
            return
        self.paths = []
        self._refresh_files()

    # ------------------------------------------------------------ actions
    def preview(self):
        a = self.app
        if a.busy or not a.fc:
            return
        paths, folder, publish = list(self.paths), self.folder_var.get(), self.publish_var.get()
        targets, gen = self.targets.targets(), self.gen
        a.run_bg(lambda: a.fc.build_plan(paths, folder, targets, publish),
                 lambda plan: self._show_plan(plan, gen), "Checking courses...")

    def _show_plan(self, plan, gen):
        if gen != self.gen:
            self.say("Selection changed while previewing - press Preview plan again.")
            return
        self.plan = plan
        t = self.plan_tree
        t.delete(*t.get_children())
        by_course = {}
        for row in plan.rows:
            by_course.setdefault(row.target.course_id, []).append(row)
        for cid, rows in by_course.items():
            n = sum(r.action == "send" for r in rows)
            t.insert("", "end", iid=f"c{cid}", text=f"  {rows[0].target.name}   ·   {n} to upload",
                     values=("", ""), open=True, tags=("course",))
            for row in rows:
                t.insert(f"c{cid}", "end", iid=f"c{cid}-{row.file.sha256}", text="  " + row.file.name,
                         values=(RESULT_TEXT[row.action], row.reason), tags=(row.action,))
        for w in plan.warnings:
            self.say("Note: " + w)
        vis = "published" if plan.publish else "UNPUBLISHED (hidden from students)"
        self.say(f"Plan: {len(plan.to_send)} upload(s) into {folder_label(plan.folder)}, {vis}.")
        self.update_buttons()

    def go(self):
        a = self.app
        if a.busy or not self.plan or not self.plan.to_send:
            return
        plan, dry = self.plan, self.dry_var.get()
        n = len(plan.to_send)
        if not dry:
            per_course = {}
            for r in plan.to_send:
                per_course.setdefault(r.target.name, []).append(r.file.name)
            lines = "\n".join(f"  - {c}: {', '.join(fs)}" for c, fs in per_course.items())
            vis = ("Students will be able to see them right away." if plan.publish
                   else "They will be unpublished (hidden from students).")
            answer = simpledialog.askstring(
                "Confirm upload",
                f"Upload {n} file copy(ies) into {folder_label(plan.folder)}:\n\n{lines}\n\n{vis}\n"
                "Existing files are never overwritten.\n\n"
                f"Type {n} to confirm:", parent=a.root)
            if (answer or "").strip() != str(n):
                self.say("Cancelled (confirmation did not match).")
                return
        self.cancel.clear()
        self.progress.configure(maximum=n, value=0)
        self.cancel_btn.pack(side="right", padx=a.px(8))

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
                messagebox.showwarning("Please check", "\n\n".join(
                    f"{r.file.name} → {r.target.name}: {r.message}" for r in bad), parent=a.root)

        a.run_bg(lambda: a.fc.execute(plan, dry_run=dry, progress=on_result, cancel=self.cancel),
                 done, "Dry run..." if dry else "Uploading...")

    def _show_result(self, r):
        iid = f"c{r.target.course_id}-{r.file.sha256}"
        if self.plan_tree.exists(iid):
            self.plan_tree.item(iid, values=(RESULT_TEXT.get(r.outcome, r.outcome), r.message), tags=(r.outcome,))
        self.progress.step(1)
        self.say(f"{RESULT_TEXT.get(r.outcome, r.outcome)}  {r.file.name} → {r.target.name}: {r.message}")


# ===================================================================== Publish
class PublishTab:
    def __init__(self, app, tab):
        self.app = app
        self.plan = None
        self.items: list = []
        self.items_kind = None
        self.items_course = None
        self.selected: set = set()
        self.gen = 0
        self.courses: list = []
        self._changes = {}
        self._build(tab)

    def _build(self, tab):
        a, px = self.app, self.app.px
        from .gui import ACCENT_SOFT, INK, MUTED, OUTCOME_COLORS, DANGER, SUCCESS
        gap = px(16)
        tab.columnconfigure(0, weight=1, uniform="p")
        tab.columnconfigure(1, weight=1, uniform="p")
        tab.rowconfigure(0, weight=1)

        outer, c = a._card(tab, 1, "Choose items", "Click items to tick them")
        outer.grid(row=0, column=0, sticky="nsew", padx=(0, gap // 2))
        c.columnconfigure(0, weight=1)
        c.rowconfigure(4, weight=1)
        ttk.Label(c, text="Course", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        self.course_var = tk.StringVar()
        self.course_cb = ttk.Combobox(c, textvariable=self.course_var, state="readonly", font=a.f_body)
        self.course_cb.grid(row=1, column=0, sticky="ew", pady=(px(4), px(10)))
        self.course_cb.bind("<<ComboboxSelected>>", lambda e: self.load_items())
        kinds = ttk.Frame(c, style="Card.TFrame")
        kinds.grid(row=2, column=0, sticky="w", pady=(0, px(6)))
        self.kind_var = tk.StringVar(value="assignment")
        for k, label in (("assignment", "Assignments"), ("file", "Files")):
            ttk.Radiobutton(kinds, text="  " + label, value=k, variable=self.kind_var, style="Card.TRadiobutton",
                            command=self.load_items).pack(side="left", padx=(0, px(22)))
        bar = ttk.Frame(c, style="Card.TFrame")
        bar.grid(row=3, column=0, sticky="ew", pady=(0, px(8)))
        self.count = ttk.Label(bar, text="0 selected", style="Muted.TLabel")
        self.count.pack(side="left")
        ttk.Button(bar, text="Select none", command=lambda: self.set_all(False)).pack(side="right")
        ttk.Button(bar, text="Select all", command=lambda: self.set_all(True)).pack(side="right", padx=px(8))
        frame, self.items_tree = a._table(c, ("state", "info"), 6, selectmode="none", show="tree headings")
        self.items_tree.heading("#0", text="NAME", anchor="w")
        self.items_tree.heading("state", text="STATUS", anchor="w")
        self.items_tree.heading("info", text="DETAILS", anchor="w")
        self.items_tree.column("#0", width=px(300))
        self.items_tree.column("state", width=px(130), stretch=False)
        self.items_tree.column("info", width=px(150), stretch=False)
        self.items_tree.tag_configure("on", background=ACCENT_SOFT, foreground=INK)
        self.items_tree.tag_configure("Unpublished", foreground=MUTED)
        frame.grid(row=4, column=0, sticky="nsew")
        self.items_tree.bind("<Button-1>", lambda e: self.toggle(self.items_tree.identify_row(e.y)))
        self.items_tree.bind("<space>", lambda e: (self.toggle(self.items_tree.focus()), "break")[1])

        outer, r = a._card(tab, 2, "Review and apply", "Only the published/unpublished switch is changed")
        outer.grid(row=0, column=1, sticky="nsew", padx=(gap // 2, 0))
        r.columnconfigure(0, weight=1)
        r.rowconfigure(2, weight=3)
        r.rowconfigure(3, weight=1)
        btns = ttk.Frame(r, style="Card.TFrame")
        btns.grid(row=0, column=0, sticky="ew")
        self.pub_btn = ttk.Button(btns, text="Preview: Publish", style="Accent.TButton",
                                  command=lambda: self.preview(True))
        self.pub_btn.pack(side="left")
        self.unpub_btn = ttk.Button(btns, text="Preview: Unpublish", command=lambda: self.preview(False))
        self.unpub_btn.pack(side="left", padx=px(10))
        opts = ttk.Frame(r, style="Card.TFrame")
        opts.grid(row=1, column=0, sticky="ew", pady=(px(10), 0))
        self.dry_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="  Dry run", variable=self.dry_var, style="Card.TCheckbutton",
                        command=self.update_buttons).pack(side="left")
        self.go_btn = ttk.Button(opts, text="Apply", style="Danger.TButton", command=self.go)
        self.go_btn.pack(side="right")
        frame, self.plan_tree = a._table(r, ("change", "detail"), 5, show="tree headings")
        self.plan_tree.heading("#0", text="ITEM", anchor="w")
        self.plan_tree.heading("change", text="CHANGE", anchor="w")
        self.plan_tree.heading("detail", text="RESULT / DETAILS", anchor="w")
        self.plan_tree.column("#0", width=px(220))
        self.plan_tree.column("change", width=px(200), stretch=False)
        self.plan_tree.column("detail", width=px(260))
        for tag, color in OUTCOME_COLORS.items():
            self.plan_tree.tag_configure(tag, foreground=color)
        for tag, color in (("change", SUCCESS), ("changed", SUCCESS), ("verify-failed", DANGER),
                           ("not-run", MUTED)):
            self.plan_tree.tag_configure(tag, foreground=color)
        frame.grid(row=2, column=0, sticky="nsew", pady=(px(12), 0))
        box, self.log = a._inset_text(r, 3, a.f_mono, fg="#334155")
        box.grid(row=3, column=0, sticky="nsew", pady=(px(12), 0))
        self.update_buttons()

    def say(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S  ") + text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def update_buttons(self):
        a = self.app
        ok = a.vc is not None and not a.busy
        for b in (self.pub_btn, self.unpub_btn):
            b.state(["!disabled" if ok else "disabled"])
        can = ok and self.plan is not None and bool(self.plan.to_change)
        self.go_btn.state(["!disabled" if can else "disabled"])
        if self.dry_var.get():
            self.go_btn.configure(text="Run dry run", style="Accent.TButton")
        else:
            n = len(self.plan.to_change) if self.plan else 0
            self.go_btn.configure(text=f"Apply for real ({n})" if n else "Apply for real", style="Danger.TButton")
        self.course_cb.state(["readonly", "!disabled"] if not a.busy else ["disabled"])
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

    def on_connected(self, courses):
        self.courses = courses
        self.course_cb["values"] = [self.app._course_label(c) for c in courses]
        self.update_buttons()

    def course(self):
        i = self.course_cb.current()
        return self.courses[i] if i >= 0 else None

    def load_items(self):
        self.invalidate()
        c = self.course()
        if not c or self.app.busy or not self.app.vc:
            return
        kind = self.kind_var.get()

        def done(items):
            self.items, self.items_kind, self.items_course = items, kind, c["id"]
            self.selected = set()
            from .visibility import is_published, item_name
            self.items_tree.delete(*self.items_tree.get_children())
            for it in items:
                pub = is_published(kind, it)
                if kind == "assignment":
                    info = ("due " + it["due_at"][:10]) if it.get("due_at") else "no due date"
                    if it.get("has_submitted_submissions"):
                        info = "has submissions"
                else:
                    info = human_size(int(it.get("size") or 0))
                self.items_tree.insert("", "end", iid=str(it["id"]), text="  " + item_name(kind, it),
                                       values=(state_word(pub), info), image=self.app.icons[("check", 0)],
                                       tags=(state_word(pub),))
            self._count()
            self.say(f"Loaded {len(items)} {kind}(s) from {c['name']}.")

        self.app.run_bg(lambda: self.app.vc.list_items(kind, c["id"]), done, f"Loading {kind}s...")

    def _count(self):
        self.count.configure(text=f"{len(self.selected)} selected")

    def _paint(self, row):
        on = int(row) in self.selected
        state = self.items_tree.item(row, "values")[0]
        self.items_tree.item(row, image=self.app.icons["check_on_sel"] if on else self.app.icons[("check", 0)],
                             tags=(["on"] if on else [state]))

    def toggle(self, row):
        if self.app.busy or not row:
            return
        self.selected.symmetric_difference_update({int(row)})
        self._paint(row)
        self._count()
        self.invalidate()

    def set_all(self, on):
        if self.app.busy:
            return
        for row in self.items_tree.get_children():
            (self.selected.add if on else self.selected.discard)(int(row))
            self._paint(row)
        self._count()
        self.invalidate()

    def preview(self, publish: bool):
        a = self.app
        c = self.course()
        if a.busy or not a.vc or not c:
            return
        if self.items_course != c["id"] or self.items_kind != self.kind_var.get():
            self.load_items()
            return
        target = Target(c["id"], c["name"], c.get("workflow_state") == "available")
        ids = [i["id"] for i in self.items if i["id"] in self.selected]
        kind, gen = self.items_kind, self.gen
        a.run_bg(lambda: a.vc.build_plan(target, kind, ids, publish), lambda p: self.show_plan(p, gen),
                 "Building preview...")

    def show_plan(self, plan, gen=None):
        if gen is not None and gen != self.gen:
            self.say("Selection changed while previewing - preview again.")
            return
        self.plan = plan
        t = self.plan_tree
        t.delete(*t.get_children())
        for row in plan.rows:
            change = f"{state_word(row.before)} → {state_word(row.after)}" if row.action == "change" else "–"
            t.insert("", "end", iid=str(row.item_id), text="  " + row.name,
                     values=(change, row.reason), tags=(row.action,))
        for w in plan.warnings:
            self.say("Note: " + w)
        verb = "publish" if plan.publish else "unpublish"
        self.say(f"Preview ({plan.target.name}): {len(plan.to_change)} to {verb}, "
                 f"{len(plan.rows) - len(plan.to_change)} skipped.")
        self.update_buttons()

    def go(self):
        a = self.app
        if a.busy or not self.plan or not self.plan.to_change:
            return
        plan, dry = self.plan, self.dry_var.get()
        n = len(plan.to_change)
        if not dry:
            verb = "PUBLISH (students can see)" if plan.publish else "UNPUBLISH (hide from students)"
            names = "\n".join(f"  - {r.name}" for r in plan.to_change)
            answer = simpledialog.askstring(
                "Confirm", f"{verb} {n} {plan.kind}(s) in {plan.target.name}:\n\n{names}\n\n"
                "Only the published/unpublished setting changes. Grades and submissions are checked "
                f"before and after.\n\nType {n} to confirm:", parent=a.root)
            if (answer or "").strip() != str(n):
                self.say("Cancelled (confirmation did not match).")
                return

        def on_result(r):
            a.q.put(lambda: self._show_result(r))

        def done(results):
            counts = {}
            for r in results:
                counts[r.outcome] = counts.get(r.outcome, 0) + 1
            self.say("Finished: " + ", ".join(f"{v} {k}" for k, v in counts.items()) + ".")
            self.plan = None
            self.update_buttons()
            a.refresh_history()
            if self.items_course == plan.target.course_id:
                self.load_items()
            bad = [r for r in results if r.outcome in ("failed", "unknown", "verify-failed")]
            if bad:
                messagebox.showwarning("Please check", "\n\n".join(f"{r.row.name}: {r.message}" for r in bad),
                                       parent=a.root)

        a.run_bg(lambda: a.vc.execute(plan, dry_run=dry, progress=on_result), done,
                 "Dry run..." if dry else "Applying...")

    def _show_result(self, r):
        iid = str(r.row.item_id)
        if self.plan_tree.exists(iid):
            self.plan_tree.item(iid, values=(RESULT_TEXT.get(r.outcome, r.outcome), r.message), tags=(r.outcome,))
        self.say(f"{RESULT_TEXT.get(r.outcome, r.outcome)}  {r.row.name}: {r.message}")

    # ------------------------------------------------------------ history
    def build_history(self, parent):
        a, px = self.app, self.app.px
        from .gui import DANGER, SUCCESS, WARN
        outer, body = a._card(parent, "◐", "Publish / unpublish changes", "Select one to undo it")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        bar = ttk.Frame(body, style="Card.TFrame")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, px(8)))
        self.undo_btn = ttk.Button(bar, text="Undo selected", style="Accent.TButton", command=self.start_undo)
        self.undo_btn.pack(side="right")
        cols = ("id", "when", "course", "kind", "item", "change", "status")
        widths = (60, 150, 240, 100, 260, 220, 120)
        frame, self.hist = a._table(body, cols, 3, selectmode="browse")
        for col, w in zip(cols, widths):
            self.hist.heading(col, text=col.upper(), anchor="w")
            self.hist.column(col, width=px(w), anchor="w")
        self.hist.tag_configure("changed", foreground=SUCCESS)
        self.hist.tag_configure("failed", foreground=DANGER)
        self.hist.tag_configure("verify-failed", foreground=DANGER)
        self.hist.tag_configure("unknown", foreground=WARN)
        frame.grid(row=1, column=0, sticky="nsew")
        return outer

    def refresh_history(self):
        if not self.app.ledger or not hasattr(self, "hist"):
            return
        self.hist.delete(*self.hist.get_children())
        self._changes = {c.id: c for c in self.app.ledger.visibility_changes()}
        for c in self._changes.values():
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(c.created_at))
            self.hist.insert("", "end", iid=str(c.id), tags=(c.status,), values=(
                c.id, when, c.course_name, c.kind, c.item_name, f"{c.before} → {c.after}", c.status))

    def start_undo(self):
        a = self.app
        sel = self.hist.selection()
        if a.busy or not a.vc:
            return
        if not sel:
            messagebox.showinfo("Pick a row", "Select the change you want to undo.", parent=a.root)
            return
        change = self._changes[int(sel[0])]

        def done(plan):
            a.nb.select(a.publish_frame)
            self.show_plan(plan)
            self.say(f"Undo preview for '{change.item_name}'. Review it, then apply.")

        a.run_bg(lambda: a.vc.build_undo_plan(change), done, "Building undo preview...")
