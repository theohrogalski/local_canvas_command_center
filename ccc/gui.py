"""Tkinter GUI for Canvas Command Center.

All network work runs on a background thread; results come back to the UI
thread through a queue. The GUI never talks to Canvas directly - it only uses
CommandCenter / SafeCanvasClient, so every safety rule applies to it too.

Display scaling is automatic (high-DPI screens get larger text). Override it
with the environment variable CCC_UI_SCALE, e.g. CCC_UI_SCALE=1.4
"""

from __future__ import annotations

import html
import os
import queue
import re
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from .canvas_client import CanvasError, SafeCanvasClient, SafetyViolation
from .config import (Limits, app_dir, grading_limits_from_env, upload_limits_from_env,
                     visibility_limits_from_env)
from .files import FileCenter
from .visibility import VisibilityCenter
from .copier import CommandCenter, PlanError, Target
from .grading import GradingCenter
from .ledger import Ledger
from .lock import AlreadyRunning, InstanceLock
from .simulator import SIM_TOKEN, SIM_URL, FakeCanvas

# ------------------------------------------------------------------ palette
BG = "#f3f5f9"            # window background
CARD = "#ffffff"          # card surface
BORDER = "#e3e7ef"        # hairline borders
INK = "#0f172a"           # primary text
MUTED = "#64748b"         # secondary text
SUBTLE = "#f8fafc"        # inset surfaces (preview, log, table headers)
ACCENT = "#4f46e5"        # indigo
ACCENT_HOVER = "#4338ca"
ACCENT_SOFT = "#eef2ff"
DANGER = "#dc2626"
DANGER_HOVER = "#b91c1c"
SUCCESS = "#16a34a"
WARN = "#d97706"
OUTCOME_LABELS = {
    "send": "\u2713  Send", "sent": "\u2713  Sent", "dry-run": "\u25cb  Dry run",
    "skip": "\u2013  Skip", "skipped": "\u2013  Skipped", "not-sent": "\u2013  Not sent",
    "failed": "\u2715  Failed", "unknown": "!  Check",
}
OUTCOME_COLORS = {
    "send": SUCCESS, "sent": SUCCESS, "dry-run": ACCENT,
    "skip": MUTED, "skipped": MUTED, "not-sent": MUTED,
    "failed": DANGER, "unknown": WARN,
}

FONT_CANDIDATES = ("Segoe UI Variable Text", "Segoe UI", "SF Pro Text", "Helvetica Neue", "Inter",
                   "Ubuntu Sans", "Noto Sans", "Lato", "Cantarell", "DejaVu Sans")
MONO_CANDIDATES = ("Cascadia Mono", "Consolas", "SF Mono", "Menlo", "JetBrains Mono",
                   "Ubuntu Sans Mono", "Ubuntu Mono", "DejaVu Sans Mono")

TOKEN_HELP = (
    "How to create a Canvas API token:\n\n"
    "1. Log in to Canvas in your browser.\n"
    "2. Click Account (left bar) > Settings.\n"
    "3. Scroll to 'Approved Integrations' and click '+ New Access Token'.\n"
    "4. Purpose: 'Canvas Command Center'. Set an expiry date (recommended: a few weeks).\n"
    "5. Click 'Generate Token' and copy it.\n\n"
    "Paste it into the Token box. The token is kept in memory only - it is never saved "
    "to disk by this app. Treat it like a password. You can delete it in Canvas at any time."
)


def strip_html(text: str, limit: int = 600) -> str:
    text = re.sub(r"<(br|/p|/div|/li)[^>]*>", "\n", text or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text if len(text) <= limit else text[:limit] + " ..."


def _blend(c1: str, c2: str, t: float) -> str:
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def draw_icon(root, size: int, kind: str, on: bool, bg: str = CARD, accent: str = ACCENT) -> tk.PhotoImage:
    """Draw a smooth checkbox / radio indicator with 4x supersampling.
    kind: 'check' or 'radio'. Pure tkinter - no image libraries needed."""
    ss = 4
    n = size * ss
    r_box = n * 0.22                      # corner radius for checkbox
    fill = accent if on else CARD
    edge = accent if on else "#94a3b8"
    stroke = max(1.0, n * 0.075)

    def inside_shape(x, y, inset):
        if kind == "radio":
            c = n / 2
            return (x - c) ** 2 + (y - c) ** 2 <= (n / 2 - inset) ** 2
        lo, hi, r = inset, n - inset, max(0.0, r_box - inset)
        if not (lo <= x <= hi and lo <= y <= hi):
            return False
        cx = min(max(x, lo + r), hi - r)
        cy = min(max(y, lo + r), hi - r)
        return (x - cx) ** 2 + (y - cy) ** 2 <= r * r

    def seg_dist(px_, py_, ax, ay, bx, by):
        dx, dy = bx - ax, by - ay
        t = max(0.0, min(1.0, ((px_ - ax) * dx + (py_ - ay) * dy) / (dx * dx + dy * dy)))
        return ((px_ - ax - t * dx) ** 2 + (py_ - ay - t * dy) ** 2) ** 0.5

    def color_at(x, y):
        if not inside_shape(x, y, 0.5):
            return None
        if not inside_shape(x, y, 0.5 + stroke):
            return edge
        if on and kind == "check":
            a, b, c = (n * .27, n * .52), (n * .43, n * .68), (n * .74, n * .34)
            if min(seg_dist(x, y, *a, *b), seg_dist(x, y, *b, *c)) <= n * 0.07:
                return "#ffffff"
        if on and kind == "radio" and (x - n / 2) ** 2 + (y - n / 2) ** 2 <= (n * 0.2) ** 2:
            return "#ffffff"
        return fill

    img = tk.PhotoImage(master=root, width=size, height=size)
    rows = []
    for py in range(size):
        row = []
        for px_ in range(size):
            acc, cover = [0.0, 0.0, 0.0], 0
            for sy in range(ss):
                for sx in range(ss):
                    col = color_at(px_ * ss + sx + 0.5, py * ss + sy + 0.5)
                    if col:
                        cover += 1
                        for i, k in enumerate((1, 3, 5)):
                            acc[i] += int(col[k:k + 2], 16)
            if cover == 0:
                row.append(bg)
            else:
                avg = "#%02x%02x%02x" % tuple(round(v / cover) for v in acc)
                row.append(_blend(bg, avg, cover / (ss * ss)))
        rows.append("{" + " ".join(row) + "}")
    img.put(" ".join(rows))
    return img


def detect_scale(root: tk.Tk) -> float:
    """1.0 = standard 96-DPI screen. WSLg and some Linux setups report 96 DPI on
    high-resolution laptop panels, so we also look at the screen height."""
    env = os.environ.get("CCC_UI_SCALE")
    if env:
        try:
            return max(0.75, min(3.0, float(env)))
        except ValueError:
            pass
    if sys.platform == "darwin":
        return 1.0                       # macOS already scales for Retina
    scale = root.winfo_fpixels("1i") / 96.0
    if scale < 1.15 and root.winfo_screenheight() >= 1300:
        scale = root.winfo_screenheight() / 1000.0
    return max(1.0, min(2.5, scale))


def wheel_scroll(widget, direction: int, delta: int = 120):
    """Scroll a list by one row per wheel notch (Windows reports 120 per notch;
    macOS reports small values, which still move at least one row)."""
    steps = max(1, round(delta / 120))
    widget.yview_scroll(direction * steps, "units")
    return "break"


def autohide_scrollbar(sb):
    """yscrollcommand that hides the scrollbar when everything fits."""
    def set_(first, last):
        if float(first) <= 0.0 and float(last) >= 1.0:
            sb.grid_remove()
        else:
            sb.grid()
        sb.set(first, last)
    return set_


class App:
    def __init__(self, root: tk.Tk, simulate: bool = False):
        self.root = root
        self.simulate = simulate
        self.q: queue.Queue = queue.Queue()
        self.client: SafeCanvasClient | None = None
        self.cc: CommandCenter | None = None
        self.gc: GradingCenter | None = None
        self.fc: FileCenter | None = None
        self.vc: VisibilityCenter | None = None
        self.ledger: Ledger | None = None
        self.courses: list = []
        self.items: list = []
        self.plan = None
        self.busy = False
        self.cancel = threading.Event()
        self.checked: set[int] = set()
        self._tmp = None
        self.gen = 0
        self.items_kind = None
        self.items_source = None

        self.scale = detect_scale(root)
        root.tk.call("tk", "scaling", self.scale * 96 / 72)   # point-sized fonts follow
        root.title("Canvas Command Center" + (" - Simulation" if simulate else ""))
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(self.px(1240), int(sw * 0.94)), min(self.px(860), int(sh * 0.9))
        root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 3)}")
        root.minsize(min(self.px(1000), sw), min(self.px(700), sh))
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._fonts()
        self._style()
        self._install_scroll_fixes()
        self._build()
        self._after_id = root.after(80, self._drain)

    def px(self, n: float) -> int:
        return int(round(n * self.scale))

    # ================================================================ style
    def _fonts(self):
        families = set(tkfont.families(self.root))
        fam = next((f for f in FONT_CANDIDATES if f in families), "TkDefaultFont")
        mono = next((f for f in MONO_CANDIDATES if f in families), "TkFixedFont")
        self.fam = fam
        self.f_body = tkfont.Font(family=fam, size=11)
        self.f_bold = tkfont.Font(family=fam, size=11, weight="bold")
        self.f_small = tkfont.Font(family=fam, size=10)
        self.f_h2 = tkfont.Font(family=fam, size=13, weight="bold")
        self.f_title = tkfont.Font(family=fam, size=17, weight="bold")
        self.f_badge = tkfont.Font(family=fam, size=9, weight="bold")
        self.f_mono = tkfont.Font(family=mono, size=10)
        # Make standard dialogs (message boxes, confirmation prompt) match.
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont",
                     "TkCaptionFont", "TkTooltipFont", "TkIconFont"):
            try:
                tkfont.nametofont(name).configure(family=fam, size=11)
            except tk.TclError:
                pass
        self.root.option_add("*TCombobox*Listbox.font", self.f_body)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT_SOFT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", INK)

    def _style(self):
        s = ttk.Style(self.root)
        s.theme_use("clam")
        px = self.px
        s.configure(".", background=BG, foreground=INK, font=self.f_body,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    troughcolor=SUBTLE, focuscolor=ACCENT, selectbackground=ACCENT_SOFT,
                    selectforeground=INK)
        s.configure("TFrame", background=BG)
        s.configure("Card.TFrame", background=CARD)
        s.configure("TLabel", background=BG, foreground=INK)
        s.configure("Card.TLabel", background=CARD, foreground=INK)
        s.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=self.f_small)
        s.configure("PageMuted.TLabel", background=BG, foreground=MUTED, font=self.f_small)
        s.configure("Field.TLabel", background=CARD, foreground=MUTED, font=self.f_small)

        # Buttons: flat, generous padding.
        pad = (px(16), px(8))
        s.configure("TButton", background="#f1f5f9", foreground=INK, bordercolor="#dbe1ea",
                    lightcolor="#f1f5f9", darkcolor="#f1f5f9", padding=pad, relief="flat", font=self.f_body)
        s.map("TButton", background=[("pressed", "#cbd5e1"), ("active", "#e2e8f0")],
              lightcolor=[("active", "#e2e8f0")], darkcolor=[("active", "#e2e8f0")],
              bordercolor=[("focus", ACCENT)], foreground=[("disabled", "#a0aec0")])
        for name, bg, hover, soft in (("Accent", ACCENT, ACCENT_HOVER, "#c7c9f4"),
                                      ("Danger", DANGER, DANGER_HOVER, "#f3b4b4")):
            s.configure(f"{name}.TButton", background=bg, foreground="white", bordercolor=bg,
                        lightcolor=bg, darkcolor=bg, padding=pad, font=self.f_bold, relief="flat")
            s.map(f"{name}.TButton",
                  background=[("disabled", soft), ("pressed", hover), ("active", hover)],
                  bordercolor=[("disabled", soft), ("active", hover)],
                  lightcolor=[("disabled", soft), ("active", hover)],
                  darkcolor=[("disabled", soft), ("active", hover)],
                  foreground=[("disabled", "white")])
        s.configure("Link.TButton", background=CARD, foreground=ACCENT, bordercolor=CARD,
                    lightcolor=CARD, darkcolor=CARD, padding=(px(6), px(8)), relief="flat")
        s.map("Link.TButton", background=[("active", ACCENT_SOFT)], bordercolor=[("active", ACCENT_SOFT)])

        # Inputs
        s.configure("TEntry", fieldbackground=CARD, bordercolor=BORDER, lightcolor=BORDER,
                    darkcolor=BORDER, padding=(px(10), px(7)), insertcolor=INK)
        s.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
              fieldbackground=[("disabled", SUBTLE)])
        s.configure("TCombobox", fieldbackground=CARD, background=CARD, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, arrowcolor=MUTED, padding=(px(10), px(7)),
                    arrowsize=px(14))
        s.map("TCombobox", fieldbackground=[("readonly", CARD), ("disabled", SUBTLE)],
              bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)],
              selectbackground=[("readonly", CARD)], selectforeground=[("readonly", INK)])
        # Smooth custom indicators (the stock clam ones draw an "X").
        size = px(18)
        self.icons = {(k, on): draw_icon(self.root, size, k, on) for k in ("check", "radio") for on in (0, 1)}
        self.icons["check_off_sel"] = draw_icon(self.root, size, "check", False, bg=ACCENT_SOFT)
        self.icons["check_on_sel"] = draw_icon(self.root, size, "check", True, bg=ACCENT_SOFT)
        self.icons["check_dis_on"] = draw_icon(self.root, size, "check", True, accent="#cbd5e1")
        self.icons["check_dis_off"] = draw_icon(self.root, size, "check", False, accent="#cbd5e1")
        for k, w in (("check", "TCheckbutton"), ("radio", "TRadiobutton")):
            elem = f"Modern.{k}.indicator"
            if elem not in s.element_names():
                states = [("selected", self.icons[(k, 1)])]
                if k == "check":
                    states = [("disabled", "selected", self.icons["check_dis_on"]),
                              ("disabled", self.icons["check_dis_off"])] + states
                s.element_create(elem, "image", self.icons[(k, 0)], *states, sticky="w", border=0)
            s.layout(f"Card.{w}", [(f"{w}.padding", {"sticky": "nswe", "children": [
                (elem, {"side": "left", "sticky": ""}),
                (f"{w}.label", {"side": "left", "sticky": "w", "expand": 1}),
            ]})])
            s.configure(f"Card.{w}", background=CARD, foreground=INK, padding=(0, px(4)),
                        compound="left", focuscolor=CARD)
            s.map(f"Card.{w}", background=[("active", CARD)],
                  foreground=[("disabled", "#a0aec0")])

        # Tables
        s.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=INK,
                    rowheight=px(34), bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    font=self.f_body, borderwidth=1)
        s.map("Treeview", background=[("selected", ACCENT_SOFT)], foreground=[("selected", INK)])
        s.configure("Treeview.Heading", background=SUBTLE, foreground=MUTED, font=self.f_small,
                    relief="flat", bordercolor=BORDER, lightcolor=SUBTLE, darkcolor=BORDER,
                    padding=(px(8), px(7)))
        s.map("Treeview.Heading", background=[("active", "#eef1f6")])
        # Scrollbars: a clearly visible thumb on a light track (the old thumb was
        # nearly white on white, so you couldn't see where you were in a list).
        s.configure("Vertical.TScrollbar", background="#c3cbd8", troughcolor="#f1f4f8", bordercolor="#f1f4f8",
                    lightcolor="#c3cbd8", darkcolor="#c3cbd8", arrowcolor=MUTED, gripcount=0,
                    arrowsize=px(14), relief="flat")
        s.map("Vertical.TScrollbar", background=[("pressed", "#7b8798"), ("active", "#94a3b8")],
              lightcolor=[("pressed", "#7b8798"), ("active", "#94a3b8")],
              darkcolor=[("pressed", "#7b8798"), ("active", "#94a3b8")])

        # Tabs: flat, accent text on the selected tab.
        s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
        s.configure("TNotebook.Tab", background=BG, foreground=MUTED, padding=(px(18), px(9)),
                    font=self.f_bold, bordercolor=BG, lightcolor=BG, borderwidth=0)
        s.map("TNotebook.Tab", background=[("selected", CARD), ("active", "#e9edf4")],
              foreground=[("selected", ACCENT)], lightcolor=[("selected", CARD)],
              bordercolor=[("selected", BORDER)])

        s.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#e8ebf2",
                    bordercolor="#e8ebf2", lightcolor=ACCENT, darkcolor=ACCENT, thickness=px(6))

    # ================================================================ build
    def _build(self):
        px = self.px
        hdr = tk.Frame(self.root, bg=CARD, highlightthickness=0)
        hdr.pack(fill="x")
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x")
        logo = tk.Canvas(hdr, width=px(34), height=px(34), bg=CARD, highlightthickness=0)
        logo.create_oval(1, 1, px(33), px(33), fill=ACCENT, outline=ACCENT)
        logo.create_text(px(17), px(17), text="CC", fill="white", font=self.f_badge)
        logo.pack(side="left", padx=(px(20), px(10)), pady=px(14))
        tk.Label(hdr, text="Canvas Command Center", bg=CARD, fg=INK, font=self.f_title).pack(side="left")
        badge_text, fg, bg = (("SIMULATION", "#9a3412", "#ffedd5") if self.simulate
                              else ("LIVE", "#166534", "#dcfce7"))
        tk.Label(hdr, text=badge_text, bg=bg, fg=fg, font=self.f_badge, padx=px(10), pady=px(3)
                 ).pack(side="left", padx=px(14))
        self.status = tk.Label(hdr, text="Not connected", bg=CARD, fg=MUTED, font=self.f_small)
        self.status.pack(side="right", padx=(0, px(20)))
        self.status_dot = tk.Label(hdr, text="●", bg=CARD, fg="#cbd5e1", font=self.f_small)
        self.status_dot.pack(side="right", padx=(0, px(6)))

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=px(20), pady=(px(14), px(16)))
        self.nb = nb
        send_tab = ttk.Frame(nb, padding=(0, px(14), 0, 0))
        self.files_frame = ttk.Frame(nb, padding=(0, px(14), 0, 0))
        self.publish_frame = ttk.Frame(nb, padding=(0, px(14), 0, 0))
        self.grading_frame = ttk.Frame(nb, padding=(0, px(14), 0, 0))
        hist_tab = ttk.Frame(nb, padding=(0, px(14), 0, 0))
        nb.add(send_tab, text="Send")
        nb.add(self.files_frame, text="Files")
        nb.add(self.publish_frame, text="Publish")
        nb.add(self.grading_frame, text="Grading")
        nb.add(hist_tab, text="History")
        nb.bind("<<NotebookTabChanged>>", lambda e: self.refresh_history())
        from .gui_files import FilesTab, PublishTab
        from .gui_grading import GradingTab
        self._build_send(send_tab)
        self.files = FilesTab(self, self.files_frame)
        self.publish = PublishTab(self, self.publish_frame)
        self.grading = GradingTab(self, self.grading_frame)
        self._build_history(hist_tab)

    def _card(self, parent, step, title, subtitle=""):
        """A white card with a numbered step badge. Returns (outer, body)."""
        px = self.px
        outer = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER,
                         highlightcolor=BORDER)
        head = tk.Frame(outer, bg=CARD)
        head.pack(fill="x", padx=px(18), pady=(px(12), px(8)))
        d = px(26)
        badge = tk.Canvas(head, width=d, height=d, bg=CARD, highlightthickness=0)
        badge.create_oval(1, 1, d - 1, d - 1, fill=ACCENT_SOFT, outline=ACCENT_SOFT)
        badge.create_text(d // 2, d // 2, text=str(step), fill=ACCENT, font=self.f_badge)
        badge.pack(side="left")
        tk.Label(head, text=title, bg=CARD, fg=INK, font=self.f_h2).pack(side="left", padx=(px(10), 0))
        if subtitle:
            tk.Label(head, text=subtitle, bg=CARD, fg=MUTED, font=self.f_small).pack(side="left", padx=px(10))
        body = ttk.Frame(outer, style="Card.TFrame", padding=(px(18), 0, px(18), px(14)))
        body.pack(fill="both", expand=True)
        return outer, body

    def _table(self, parent, columns, height, **kw):
        """Treeview with a vertical scrollbar that only appears when needed."""
        frame = ttk.Frame(parent, style="Card.TFrame")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        kw.setdefault("show", "headings")
        tree = ttk.Treeview(frame, columns=columns, height=height, **kw)
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=autohide_scrollbar(sb))
        tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        sb.grid_remove()
        return frame, tree

    def _install_scroll_fixes(self):
        """Fix the two scrolling problems with Tk's defaults:
        1. Turning the mouse wheel over a drop-down (e.g. 'From course') silently
           CHANGED the selected course. Drop-downs now ignore the wheel; click
           them to choose.
        2. One wheel notch scrolled lists by 5 rows - more than a whole list at
           larger text sizes - so lists jumped past the courses you wanted.
           Now one notch = one row."""
        r = self.root
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            r.bind_class("TCombobox", seq, lambda e: "break")
        r.bind_class("Treeview", "<Button-4>", lambda e: wheel_scroll(e.widget, -1))
        r.bind_class("Treeview", "<Button-5>", lambda e: wheel_scroll(e.widget, 1))
        r.bind_class("Treeview", "<MouseWheel>",
                     lambda e: wheel_scroll(e.widget, -1 if e.delta > 0 else 1, abs(e.delta)))

    def _inset_text(self, parent, height, font, fg=INK):
        px = self.px
        box = tk.Frame(parent, bg=SUBTLE, highlightthickness=1, highlightbackground=BORDER)
        t = tk.Text(box, height=height, wrap="word", relief="flat", bd=0, bg=SUBTLE, fg=fg,
                    font=font, padx=px(12), pady=px(10), highlightthickness=0, cursor="arrow")
        t.pack(fill="both", expand=True)
        t.configure(state="disabled")
        return box, t

    def _build_send(self, tab):
        px = self.px
        gap = px(16)
        tab.columnconfigure(0, weight=1, uniform="cols")
        tab.columnconfigure(1, weight=1, uniform="cols")
        tab.rowconfigure(1, weight=1)
        tab.rowconfigure(2, weight=1)

        # ---- Step 1: connect
        outer, c = self._card(tab, 1, "Connect to Canvas",
                              "Simulation - fake courses, nothing reaches a real Canvas" if self.simulate
                              else "Your token stays in memory only")
        outer.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, gap))
        c.columnconfigure(0, weight=3)
        c.columnconfigure(1, weight=3)
        ttk.Label(c, text="Canvas address", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(c, text="API token", style="Field.TLabel").grid(row=0, column=1, sticky="w", padx=(gap, 0))
        self.url_var = tk.StringVar(value=SIM_URL if self.simulate else os.environ.get("CANVAS_URL", "https://"))
        self.url_entry = ttk.Entry(c, textvariable=self.url_var, font=self.f_body)
        self.url_entry.grid(row=1, column=0, sticky="ew", pady=(px(4), 0))
        self.token_var = tk.StringVar(value=SIM_TOKEN if self.simulate else "")
        self.token_entry = ttk.Entry(c, textvariable=self.token_var, show="•", font=self.f_body)
        self.token_entry.grid(row=1, column=1, sticky="ew", padx=(gap, 0), pady=(px(4), 0))
        self.connect_btn = ttk.Button(c, text="Connect", style="Accent.TButton", command=self.connect)
        self.connect_btn.grid(row=1, column=2, padx=(gap, 0), pady=(px(4), 0))
        ttk.Button(c, text="How do I get a token?", style="Link.TButton", command=lambda: messagebox.showinfo(
            "Canvas API token", TOKEN_HELP, parent=self.root)).grid(row=1, column=3, padx=(px(8), 0),
                                                                   pady=(px(4), 0))
        self.token_entry.bind("<Return>", lambda e: self.connect())

        # ---- Step 2: what
        outer, w = self._card(tab, 2, "What to send")
        outer.grid(row=1, column=0, sticky="nsew", padx=(0, gap // 2))
        w.columnconfigure(0, weight=1)
        w.rowconfigure(3, weight=3)
        w.rowconfigure(4, weight=1)
        ttk.Label(w, text="From course", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        self.source_var = tk.StringVar()
        self.source_cb = ttk.Combobox(w, textvariable=self.source_var, state="readonly", font=self.f_body)
        self.source_cb.grid(row=1, column=0, sticky="ew", pady=(px(4), px(10)))
        self.source_cb.bind("<<ComboboxSelected>>", lambda e: self.load_items())
        kinds = ttk.Frame(w, style="Card.TFrame")
        kinds.grid(row=2, column=0, sticky="w", pady=(0, px(8)))
        self.kind_var = tk.StringVar(value="announcement")
        for k, label in (("announcement", "Announcement"), ("assignment", "Assignment")):
            ttk.Radiobutton(kinds, text="  " + label, value=k, variable=self.kind_var, style="Card.TRadiobutton",
                            command=self.load_items).pack(side="left", padx=(0, px(22)))
        frame, self.items_tree = self._table(w, ("title", "info"), 3, selectmode="browse")
        self.items_tree.heading("title", text="TITLE", anchor="w")
        self.items_tree.heading("info", text="DATE / DUE", anchor="w")
        self.items_tree.column("title", width=px(320), anchor="w")
        self.items_tree.column("info", width=px(140), anchor="w", stretch=False)
        frame.grid(row=3, column=0, sticky="nsew")
        self.items_tree.bind("<<TreeviewSelect>>", lambda e: self.on_item_selected())
        box, self.preview = self._inset_text(w, 2, self.f_small, fg=MUTED)
        box.grid(row=4, column=0, sticky="nsew", pady=(px(10), 0))

        # ---- Step 3: where
        outer, t = self._card(tab, 3, "Where to send it", "Click a course to tick it")
        outer.grid(row=1, column=1, sticky="nsew", padx=(gap // 2, 0))
        t.columnconfigure(0, weight=1)
        t.rowconfigure(1, weight=1)
        bar = ttk.Frame(t, style="Card.TFrame")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, px(8)))
        self.target_count = ttk.Label(bar, text="0 selected", style="Muted.TLabel")
        self.target_count.pack(side="left")
        ttk.Button(bar, text="Select none", command=lambda: self.set_all(False)).pack(side="right")
        ttk.Button(bar, text="Select all", command=lambda: self.set_all(True)).pack(side="right", padx=px(8))
        frame, self.target_tree = self._table(t, ("state",), 4, selectmode="none", show="tree headings")
        self.target_tree.heading("#0", text="COURSE", anchor="w")
        self.target_tree.heading("state", text="STATUS", anchor="w")
        self.target_tree.column("#0", width=px(340), anchor="w")
        self.target_tree.column("state", width=px(120), anchor="w", stretch=False)
        self.target_tree.tag_configure("on", background=ACCENT_SOFT, foreground=INK)
        self.target_tree.tag_configure("unpublished", foreground=MUTED)
        frame.grid(row=1, column=0, sticky="nsew")
        self.target_tree.bind("<Button-1>", self.toggle_target)
        self.target_tree.bind("<space>", lambda e: (self._toggle_row(self.target_tree.focus()), "break")[1])

        # ---- Step 4: review & send
        outer, r = self._card(tab, 4, "Review and send", "Preview first - nothing is posted until you confirm")
        outer.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(gap, 0))
        r.columnconfigure(0, weight=3)
        r.columnconfigure(1, weight=2)
        r.rowconfigure(1, weight=1)
        opts = ttk.Frame(r, style="Card.TFrame")
        opts.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.dry_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="  Dry run  (check everything, post nothing)", variable=self.dry_var,
                        style="Card.TCheckbutton", command=self._update_buttons).pack(side="left")
        self.publish_var = tk.BooleanVar(value=True)
        self.publish_cb = ttk.Checkbutton(opts, text="  Publish assignment  (students see it now)",
                                          variable=self.publish_var, style="Card.TCheckbutton",
                                          command=self.invalidate_plan)
        self.publish_cb.pack(side="left", padx=px(24))
        self.send_btn = ttk.Button(opts, text="Send", style="Danger.TButton", command=self.send)
        self.send_btn.pack(side="right")
        self.preview_btn = ttk.Button(opts, text="Preview plan", style="Accent.TButton", command=self.build_plan)
        self.preview_btn.pack(side="right", padx=px(10))
        self.cancel_btn = ttk.Button(opts, text="Stop", command=self.cancel.set)

        frame, self.plan_tree = self._table(r, ("course", "action", "detail"), 3)
        for col, label, width, stretch in (("course", "COURSE", 240, True), ("action", "RESULT", 130, False),
                                           ("detail", "DETAILS", 400, True)):
            self.plan_tree.heading(col, text=label, anchor="w")
            self.plan_tree.column(col, width=px(width), anchor="w", stretch=stretch)
        for tag, color in OUTCOME_COLORS.items():
            self.plan_tree.tag_configure(tag, foreground=color)
        frame.grid(row=1, column=0, sticky="nsew", pady=(px(12), 0))

        box, self.log = self._inset_text(r, 3, self.f_mono, fg="#334155")
        box.grid(row=1, column=1, sticky="nsew", padx=(gap, 0), pady=(px(12), 0))
        self.progress = ttk.Progressbar(r, mode="determinate", style="Horizontal.TProgressbar")
        self.progress.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(px(12), 0))
        self._update_buttons()

    def _build_history(self, tab):
        px = self.px
        tab.columnconfigure(0, weight=1)
        for i in range(3):
            tab.rowconfigure(i, weight=1)
        outer, body = self._card(tab, "✓", "Sent items and uploaded files",
                                 "A course never gets the same item or file twice while its row exists here")
        outer.grid(row=0, column=0, sticky="nsew")
        self.publish.build_history(tab).grid(row=1, column=0, sticky="nsew", pady=(px(12), 0))
        self.grading.build_history(tab).grid(row=2, column=0, sticky="nsew", pady=(px(12), 0))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        bar = ttk.Frame(body, style="Card.TFrame")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, px(8)))
        ttk.Button(bar, text="Refresh", command=self.refresh_history).pack(side="right")
        cols = ("id", "when", "status", "kind", "title", "from", "to", "new_id", "detail")
        widths = (60, 150, 90, 120, 280, 80, 80, 90, 320)
        frame, self.hist = self._table(body, cols, 3)
        for col, w in zip(cols, widths):
            self.hist.heading(col, text=col.replace("_", " ").upper(), anchor="w")
            self.hist.column(col, width=px(w), anchor="w")
        self.hist.tag_configure("sent", foreground=SUCCESS)
        self.hist.tag_configure("pending", foreground=WARN)
        self.hist.tag_configure("failed", foreground=DANGER)
        frame.grid(row=1, column=0, sticky="nsew")
        ttk.Label(body, text="pending = outcome unknown. Check that course in Canvas. If the item is NOT "
                             "there, run:  python3 -m ccc release <ID>", style="Muted.TLabel"
                  ).grid(row=2, column=0, sticky="w", pady=(px(10), 0))

    # ============================================================ helpers
    def say(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("%H:%M:%S  ") + text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def run_bg(self, work, done, label="Working..."):
        self.busy = True
        self._update_buttons()
        self.status.configure(text=label)
        self.status_dot.configure(fg=WARN)

        def target():
            try:
                result = work()
                cb = lambda: done(result)
            except Exception as e:  # report every failure in the UI
                cb = lambda e=e: self._bg_error(e)
            # clear "busy" first so the callback may start follow-up work
            self.q.put(lambda: (self._bg_finished(), cb()))

        threading.Thread(target=target, daemon=True).start()

    def _bg_finished(self):
        self.busy = False
        self._update_buttons()
        if self.client:
            self.status.configure(text=f"Connected to {self.client.base_url}")
            self.status_dot.configure(fg=SUCCESS)
        else:
            self.status.configure(text="Not connected")
            self.status_dot.configure(fg="#cbd5e1")

    def _bg_error(self, e):
        kind = {PlanError: "Cannot do that", SafetyViolation: "Blocked by safety rule",
                CanvasError: "Canvas error"}.get(type(e), "Error")
        self.say(f"{kind}: {e}")
        messagebox.showerror(kind, str(e), parent=self.root)

    def _drain(self):
        try:
            while True:
                self.q.get_nowait()()
        except queue.Empty:
            pass
        self._after_id = self.root.after(80, self._drain)

    def _update_buttons(self):
        connected = self.client is not None
        st = lambda ok: ("!disabled" if ok and not self.busy else "disabled")
        self.connect_btn.state([st(True)])
        self.preview_btn.state([st(connected)])
        can_send = connected and self.plan is not None and bool(self.plan.to_send)
        self.send_btn.state([st(can_send)])
        if self.dry_var.get():
            self.send_btn.configure(text="Run dry run", style="Accent.TButton")
        else:
            n = len(self.plan.to_send) if self.plan else 0
            self.send_btn.configure(text=f"Send for real ({n})" if n else "Send for real",
                                    style="Danger.TButton")
        self.source_cb.state(["readonly", "!disabled"] if not self.busy else ["disabled"])
        self.publish_cb.state(["!disabled" if self.kind_var.get() == "assignment" else "disabled"])
        for tab in ("grading", "files", "publish"):
            if hasattr(self, tab):
                getattr(self, tab).update_buttons()

    def invalidate_plan(self):
        self.gen += 1          # any plan being built for the old selection is discarded
        if self.busy:
            return
        if self.plan is not None:
            self.plan = None
            self.plan_tree.delete(*self.plan_tree.get_children())
        self._update_buttons()

    # ============================================================ actions
    def connect(self):
        if self.busy:
            return
        url = self.url_var.get().strip()
        token = self.token_var.get().strip() or os.environ.get("CANVAS_API_TOKEN", "")
        try:
            if self.simulate:
                if self._tmp is None:
                    self._tmp = tempfile.TemporaryDirectory()
                    self.fake = FakeCanvas.demo(page_size=3)
                    self.ledger = Ledger(Path(self._tmp.name) / "sim-ledger.db")
                client = SafeCanvasClient(url, token, transport=self.fake)
                gclient = SafeCanvasClient(url, token, transport=self.fake, mode="grading")
                pclient = SafeCanvasClient(url, token, transport=self.fake, mode="publish")
                ulimits = Limits(min_seconds_between_posts=0.5, max_posts_per_run=50, max_posts_per_hour=100)
                vlimits = Limits(min_seconds_between_posts=0.2, max_posts_per_run=100, max_posts_per_hour=300)
                limits = Limits(min_seconds_between_posts=1.0)
                glimits = Limits(min_seconds_between_posts=0.2, max_posts_per_run=400, max_posts_per_hour=1000)
                snap_dir = Path(self._tmp.name) / "snapshots"
            else:
                client = SafeCanvasClient(url, token)
                gclient = SafeCanvasClient(url, token, mode="grading")
                pclient = SafeCanvasClient(url, token, mode="publish")
                ulimits = upload_limits_from_env()
                vlimits = visibility_limits_from_env()
                if self.ledger is None:
                    self.ledger = Ledger(app_dir() / "ledger.db")
                limits = Limits.from_env()
                glimits = grading_limits_from_env()
                snap_dir = app_dir() / "snapshots"
        except ValueError as e:
            messagebox.showerror("Check your details", str(e), parent=self.root)
            return

        def work():
            me = client.whoami()
            return me, client.list_teacher_courses()

        def done(res):
            me, courses = res
            self.client = client
            self.cc = CommandCenter(client, self.ledger, limits)
            self.gc = GradingCenter(gclient, self.ledger, snap_dir, glimits)
            self.fc = FileCenter(client, pclient, self.ledger, ulimits)
            self.vc = VisibilityCenter(pclient, self.ledger, vlimits)
            if not self.simulate:
                self.token_var.set("")  # do not keep the token in the widget
            self.courses = sorted(courses, key=lambda c: c.get("name", ""))
            self.source_cb["values"] = [self._course_label(c) for c in self.courses]
            self.say(f"Connected as {me.get('name')}. You teach {len(self.courses)} course(s).")
            self.grading.on_connected(self.courses)
            self.files.on_connected(self.courses)
            self.publish.on_connected(self.courses)
            if self.courses:
                self.source_cb.current(0)
                self.load_items()
            self._update_buttons()

        self.run_bg(work, done, "Connecting...")

    @staticmethod
    def _course_label(c):
        return f"{c.get('name')}  (#{c['id']})"

    def source_course(self):
        i = self.source_cb.current()
        return self.courses[i] if i >= 0 else None

    def load_items(self):
        self.invalidate_plan()
        src = self.source_course()
        if not src or not self.cc or self.busy:
            return
        kind = self.kind_var.get()
        self._fill_targets(src["id"])

        def done(items):
            self.items = items
            self.items_kind = kind           # the plan always uses the kind of the listed items
            self.items_source = src["id"]
            self.items_tree.delete(*self.items_tree.get_children())
            for i, it in enumerate(items):
                title = it.get("title") or it.get("name") or "(untitled)"
                info = (it.get("posted_at") or "")[:10] if kind == "announcement" else \
                       ("due " + it["due_at"][:10] if it.get("due_at") else "no due date")
                self.items_tree.insert("", "end", iid=str(i), values=(title, info))
            self._set_preview("")
            self.say(f"Loaded {len(items)} {kind}(s) from {src['name']}.")

        self.run_bg(lambda: self.cc.list_items(kind, src["id"]), done, f"Loading {kind}s...")

    def _fill_targets(self, source_id):
        y = self.target_tree.yview()[0]                # keep the scroll position
        self.target_tree.delete(*self.target_tree.get_children())
        self.checked &= {c["id"] for c in self.courses if c["id"] != source_id}
        for c in self.courses:
            if c["id"] == source_id:
                continue
            state = "Published" if c.get("workflow_state") == "available" else "Unpublished"
            self.target_tree.insert("", "end", iid=str(c["id"]), text="  " + c["name"], values=(state,))
            self._paint_target(str(c["id"]))
        self.target_tree.yview_moveto(y)
        self._update_target_count()

    def _paint_target(self, row):
        on = int(row) in self.checked
        state = self.target_tree.item(row, "values")[0]
        tags = ["on"] if on else []
        if state == "Unpublished" and not on:
            tags.append("unpublished")
        img = self.icons["check_on_sel"] if on else self.icons[("check", 0)]
        self.target_tree.item(row, image=img, tags=tags)

    def _update_target_count(self):
        n = len(self.checked)
        self.target_count.configure(text=f"{n} course{'s' if n != 1 else ''} selected")

    def toggle_target(self, event):
        self._toggle_row(self.target_tree.identify_row(event.y))

    def _toggle_row(self, row):
        if self.busy or not row:
            return
        cid = int(row)
        self.checked.symmetric_difference_update({cid})
        self._paint_target(row)
        self._update_target_count()
        self.invalidate_plan()

    def set_all(self, on: bool):
        if self.busy:
            return
        for row in self.target_tree.get_children():
            cid = int(row)
            (self.checked.add if on else self.checked.discard)(cid)
            self._paint_target(row)
        self._update_target_count()
        self.invalidate_plan()

    def selected_item(self):
        sel = self.items_tree.selection()
        return self.items[int(sel[0])] if sel else None

    def _set_preview(self, text):
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)
        self.preview.configure(state="disabled")

    def on_item_selected(self):
        self.invalidate_plan()
        it = self.selected_item()
        if it:
            self._set_preview(strip_html(it.get("message") or it.get("description") or "(no text)"))

    def build_plan(self):
        if self.busy or not self.cc:
            return
        src, item = self.source_course(), self.selected_item()
        if not src or not item:
            messagebox.showinfo("Pick something", "Choose a source course and an item to send.", parent=self.root)
            return
        targets = [Target(c["id"], c["name"], c.get("workflow_state") == "available")
                   for c in self.courses if c["id"] in self.checked]
        kind = self.items_kind
        if self.items_source != src["id"] or kind != self.kind_var.get():
            self.load_items()
            return
        publish = self.publish_var.get()
        gen = self.gen

        def done(plan):
            if gen != self.gen:
                self.say("Selection changed while previewing - press Preview plan again.")
                return
            self.plan = plan
            self.plan_tree.delete(*self.plan_tree.get_children())
            for i, row in enumerate(plan.rows):
                detail = row.reason + ("  -  " + "; ".join(row.warnings) if row.warnings else "")
                self.plan_tree.insert("", "end", iid=str(row.target.course_id),
                                      values=(row.target.name, OUTCOME_LABELS[row.action], detail), tags=(row.action,))
            self.say(f"Plan for '{plan.title}': {len(plan.to_send)} to send, "
                     f"{len(plan.rows) - len(plan.to_send)} skipped.")
            for w in plan.warnings:
                self.say(f"WARNING: {w}")
            if plan.warnings:
                messagebox.showwarning("Please check", "\n\n".join(plan.warnings), parent=self.root)
            self._update_buttons()

        self.run_bg(lambda: self.cc.build_plan(kind, src["id"], item["id"], targets, publish), done,
                    "Building plan...")

    def send(self):
        if self.busy or not self.plan or not self.plan.to_send:
            return
        plan, dry = self.plan, self.dry_var.get()
        n = len(plan.to_send)
        if not dry:
            names = "\n".join(f"  - {r.target.name}" for r in plan.to_send)
            effect = ("Students will be notified (email, per their Canvas settings)."
                      if plan.kind == "announcement" else
                      ("The assignment will be published." if self.publish_var.get()
                       else "The assignment will be created unpublished."))
            answer = simpledialog.askstring(
                "Confirm real send",
                f"Post the {plan.kind} '{plan.title}' to {n} course(s):\n\n{names}\n\n{effect}\n"
                f"This cannot be undone by this tool.\n\nType {n} to confirm:", parent=self.root)
            if (answer or "").strip() != str(n):
                self.say("Send cancelled (confirmation did not match).")
                return
        self.cancel.clear()
        self.progress.configure(maximum=n, value=0)
        self.cancel_btn.pack(side="right", padx=8)

        def on_result(r):
            self.q.put(lambda: self._show_result(r))

        def done(results):
            counts = {}
            for r in results:
                counts[r.outcome] = counts.get(r.outcome, 0) + 1
            summary = ", ".join(f"{v} {k}" for k, v in counts.items())
            self.say(f"Finished: {summary}.")
            self.cancel_btn.pack_forget()
            self.plan = None   # a plan is used once; preview again to send more
            self._update_buttons()
            self.refresh_history()
            if any(r.outcome == "unknown" for r in results):
                messagebox.showwarning(
                    "Check Canvas", "One send could not be confirmed. The tool stopped and will not resend "
                    "it. Open that course in Canvas to check. See the History tab.", parent=self.root)

        self.run_bg(lambda: self.cc.execute(plan, dry_run=dry, progress=on_result, cancel=self.cancel),
                    done, "Dry run..." if dry else "Sending...")

    def _show_result(self, r):
        iid = str(r.target.course_id)
        if self.plan_tree.exists(iid):
            extra = f" (new id {r.created_id})" if r.created_id else ""
            self.plan_tree.item(iid, values=(r.target.name, OUTCOME_LABELS.get(r.outcome, r.outcome), r.message + extra),
                                tags=(r.outcome,))
        self.progress.step(1)
        self.say(f"{r.outcome.upper():8} {r.target.name}: {r.message}")

    def refresh_history(self):
        if not self.ledger:
            return
        self.grading.refresh_history()
        self.publish.refresh_history()
        self.hist.delete(*self.hist.get_children())
        for row in self.ledger.all_rows():
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row.updated_at))
            self.hist.insert("", "end", values=(row.id, when, row.status, row.kind, row.title,
                                                "your computer" if row.kind == "file" else row.source_course,
                                                row.target_course,
                                                row.created_item_id or "", row.detail or ""),
                             tags=(row.status,))

    def on_close(self):
        if getattr(self, "_closing", False):
            return
        if self.busy:
            if not messagebox.askyesno("Task in progress",
                                       "A task is running. Stop after the current step and close?",
                                       parent=self.root):
                return
            # Ask every running task to stop after its current step, then wait for
            # that step to finish AND be recorded before closing. (Closing at once
            # would cut off a request mid-flight and lose its result.)
            self._closing = True
            for ev in (self.cancel, self.grading.cancel, self.files.cancel):
                ev.set()
            self.status.configure(text="Finishing the current step, then closing...")
            self._wait_then_close()
            return
        self._finish_close()

    def _wait_then_close(self):
        if self.busy:
            self.root.after(200, self._wait_then_close)
        else:
            self._finish_close()

    def _finish_close(self):
        try:
            self.root.after_cancel(self._after_id)
        except (tk.TclError, AttributeError):
            pass
        if self.ledger:
            self.ledger.close()
        if self._tmp:
            self._tmp.cleanup()
        self.root.destroy()


def launch(simulate: bool = False) -> int:
    lock = None
    if not simulate:
        try:
            lock = InstanceLock(app_dir() / "app.lock").acquire()
        except AlreadyRunning as e:
            print(e)
            try:                                  # also visible when started from an icon
                r = tk.Tk()
                r.withdraw()
                messagebox.showinfo("Canvas Command Center", str(e), parent=r)
                r.destroy()
            except tk.TclError:
                pass
            return 1
    root = tk.Tk()
    App(root, simulate=simulate)
    try:
        root.mainloop()
    finally:
        if lock:
            lock.release()
    return 0
