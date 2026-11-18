# Canvas Command Center

From your own computer, Canvas Command Center manages all the Canvas courses (sections) you teach:

- **Send tab.** Send an **announcement** or an **assignment** from one course to any of your other courses.
- **Files tab.** **Upload files** from your computer to any of your courses, published or hidden.
- **Publish tab.** **Publish or unpublish** assignments and files in any course.
- **Grading tab.** Copy one course's **grade weighting** (assignment groups and their percentages) to other courses.
- **History tab.** See everything the app has done, and **undo** publish and grading changes.

How it behaves:

- Runs **locally**. It uses only the Python standard library: nothing to `pip install` and no server.
- **Sending and uploading only add.** They can create an announcement, assignment or file. They can't
  edit, overwrite or delete anything.
- **Publishing changes one switch.** The Publish tab can only flip an item between published and
  unpublished, and nothing else about it.
- **Grading never touches grades.** The Grading tab can change only weighting. Every score and
  submission is checked to be identical after each course, and every change can be undone.
- **Never sends twice.** Every send and upload is recorded locally, and a course that already got an item or file is skipped.
- **Rate limited.** Posts are spaced out and capped per click and per hour.
- **Announcements email students.** Canvas notifies students the normal way, per their notification settings.

---

## Contents

1. [What you need](#1-what-you-need)
2. [Try it safely first (simulation)](#2-try-it-safely-first-simulation)
3. [Get your Canvas API token](#3-get-your-canvas-api-token)
4. [Using the app](#4-using-the-app)
5. [Uploading files (Files tab)](#5-uploading-files-files-tab)
6. [Publishing and unpublishing (Publish tab)](#6-publishing-and-unpublishing-publish-tab)
7. [Copying grade weighting (Grading tab)](#7-copying-grade-weighting-grading-tab)
8. [How it keeps your courses safe](#8-how-it-keeps-your-courses-safe)
9. [What gets copied (and what doesn't)](#9-what-gets-copied-and-what-doesnt)
10. [If something goes wrong](#10-if-something-goes-wrong)
11. [Command reference](#11-command-reference)
12. [Settings](#12-settings)
13. [Tests and simulation](#13-tests-and-simulation)
14. [Project layout](#14-project-layout)
15. [FAQ](#15-faq)

---

## 1. What you need

| Requirement | How to check |
|---|---|
| Python 3.10 or newer | `python3 --version` |
| Tkinter (the GUI toolkit that ships with Python) | `python3 -c "import tkinter"` (no error = OK) |
| A Canvas account where you are a **Teacher** in the courses | - |

On Ubuntu/WSL, if Tkinter is missing: `sudo apt install python3-tk`.
On Windows and macOS, the python.org installer already includes it.

There are no other dependencies.

---

## 2. Try it safely first (simulation)

The simulation runs the **real app** against a **fake, in-memory Canvas**. Nothing touches your
real courses, so you can click everything.

```bash
cd canvas_command_center
python3 -m ccc gui --simulate      # the GUI, with 5 fake "MA 121" sections
python3 -m ccc simulate            # a narrated, step-by-step demo in the terminal
python3 -m ccc simulate-grading    # the same for copying grade weighting (Grading tab)
```

The terminal demo shows each safety feature in action: a dry run, a real send, a blocked
resend, a duplicate-title skip, a refused quiz, a dropped network connection, and the rate
limits. It ends with a safety audit.

---

## 3. Get your Canvas API token

The token lets the app act as you in Canvas. Treat it like a password.

1. Log in to Canvas in your browser.
2. Click **Account** (left bar) → **Settings**.
3. Under **Approved Integrations**, click **+ New Access Token**.
4. Purpose: `Canvas Command Center`. **Set an expiry date** (a few weeks is a good habit).
5. Click **Generate Token** and copy it.

The app keeps the token **in memory only**. It is never written to disk, never logged, and
cleared from the input box after you connect. To revoke it, delete it on the same Canvas
settings page.

> Optional: put the values in environment variables so you don't paste them each time:
> `export CANVAS_URL=https://yourschool.instructure.com` and
> `export CANVAS_API_TOKEN=...` (leave the token box empty and press Connect).

---

## 4. Using the app

```bash
python3 -m ccc
```

The window follows four numbered steps, top to bottom:

| Step | What you do |
|---|---|
| **1 Connect** | Enter your Canvas address (e.g. `https://yourschool.instructure.com`) and token, then click **Connect**. |
| **2 What to send** | Pick the **From course**, choose **Announcement** or **Assignment**, and click the item. Its text appears below the list. |
| **3 Where to send it** | Tick the courses that should receive it (**Select all** ticks every course). The source course is never listed. |
| **4 Review and send** | Click **Preview plan**. Each course shows **Send** or **Skip**, with the reason. |

Then:

1. Leave **Dry run** ticked and click **Run dry run**. Everything is checked and nothing is posted.
2. Untick **Dry run** and click **Send for real (N)**.
3. A confirmation box lists every course. **Type the number of courses** to confirm.
4. Watch each row change to **Sent**. The log on the right shows every step.

**After a send, the plan is used up.** To send something else, preview again.
Changing anything (item, courses, options) also clears the preview, so you never send a stale plan.

The **History** tab lists everything this app has sent, with dates and the new Canvas item IDs.

**Text too small or too big?** The app scales itself for high-resolution screens. To override:
`CCC_UI_SCALE=1.5 python3 -m ccc` (any value from 0.75 to 3).

---

## 5. Uploading files (Files tab)

Put the same handout, slides or answer key into several sections at once.

1. **1 Choose files:** click **Add files…** and pick one or more files.
2. Optionally type a **folder**, e.g. `Week 12/Handouts`. Canvas creates it in each course if it
   doesn't exist yet. Leave it blank for the top Files folder.
3. Choose visibility. **Publish files** (ticked) lets students see them right away; untick it to
   upload them hidden.
4. **2 Upload to:** tick the courses.
5. **3 Review and upload:** click **Preview plan**, run the dry run, then **Upload for real** and type
   the number to confirm.

Each upload is checked afterwards. The app reads the file back from Canvas and confirms its size
and its published/unpublished state.

**What it will never do:**
- **Overwrite or replace anything.** Canvas is always told "if the name is taken, rename, never
  overwrite". On top of that, a course that already has a file with the same name in that folder
  is skipped.
- **Upload the same file to the same course twice.** The history remembers each file by its
  contents, so renaming the file on your computer doesn't get around this.
- **Upload a file that changed after you previewed it.** If you edit the file in between, it is
  skipped and you're asked to preview again.

**Good to know:**
- **Size limit:** files up to 250 MB (change with `CCC_MAX_UPLOAD_MB`).
- **Hidden uploads:** Canvas has no "upload as hidden" option, so the app uploads, then immediately
  unpublishes. For about a second the file is technically visible.
- **Where the bytes go:** Canvas stores files on its own storage service (for hosted Canvas, a
  `*.inscloudgate.net` or Instructure address). The app sends the file only to such known
  addresses, and **never sends your token there**. If your school runs its own Canvas with its own
  storage, add that address to `CCC_EXTRA_UPLOAD_HOSTS`.

---

## 6. Publishing and unpublishing (Publish tab)

1. **1 Choose items:** pick a **course**, then **Assignments** or **Files**. Click items to tick them.
   The list shows each item's current status, and whether an assignment already has submissions.
2. **2 Review and apply:** click **Preview: Publish** or **Preview: Unpublish**. You see
   *Published → Unpublished* (or the reverse) for each item.
3. Run the dry run, then **Apply for real** and type the number to confirm.

**Skipped automatically, with the reason shown:**
- **Already in that state.**
- **Assignments that students have already submitted to, when unpublishing.** Canvas doesn't allow
  this, and it protects their work.
- **Quizzes and graded discussions.** Publish those from their own page in Canvas.
- **Anything that changed after your preview.**

**Checks on every change:**
- Only the published switch changes. Name, points, dates and content are untouched.
- For assignments, every submission and grade is fingerprinted before and checked after.
- The item is re-read to confirm the new state stuck.

**Undo:** History tab → **Publish / unpublish changes** → select a row → **Undo selected**.

**Heads-up:** publishing an assignment makes it visible right away, and Canvas may notify students
according to their notification settings. The preview reminds you.

---

## 7. Copying grade weighting (Grading tab)

Use this when several sections should share the same grade breakdown, for example
**Homework 20% · Labs 25% · Exams 55%**.

### How Canvas weighting works (30-second version)

- Canvas weights **assignment groups**, not single assignments. Each group gets a percentage of
  the final grade.
- Inside a group, assignments count by their **points**.
- Weighting must be switched on per course ("Weight final grade based on assignment groups").

### Steps

1. **Set it up once in Canvas**, in one course (the *source*). Create the groups, set their
   percentages, put the assignments in the right groups, and switch weighting on.
2. In the app, open the **Grading** tab (connect on the Send tab first).
3. **1 Copy weighting from:** pick the source course. Its groups and percentages are shown.
4. **2 Apply to:** tick the other sections.
5. **3 Review and apply:** click **Preview changes**. Each course lists exactly what will change,
   for example "Weight of 'Exams' 40% → 55%" or "Move 'Midterm Exam' Assignments → Exams".
6. Run the dry run, untick **Dry run**, click **Apply for real**, and type the number of courses to confirm.

Each finished course reports, for example, **"7 change(s) applied. 812 submission(s) verified unchanged."**

### What it changes, and what it never touches

| Can change | Never touched |
|---|---|
| Weighting on/off for the course | Any score, grade, or excused/late/missing status |
| A group's percentage | Any submission, file, comment or attempt |
| Create a missing group (with its %) | Assignment points, dates, names, descriptions, published state |
| Which group an assignment is in | Rubrics, group drop rules, announcements |
| | **Groups are never deleted** |

**Course totals will change.** That is the purpose of weighting: Canvas recalculates each
student's course percentage from the new weights. Individual assignment scores do not change.

### How things are matched

- **Groups** are matched by name (ignoring capitals and extra spaces). A group the target lacks is created.
- **Assignments** are matched by name. An assignment that exists only in the target course is left
  exactly where it is. The preview warns you if it sits in a 0% group and so won't count.
- When a name is **ambiguous**, nothing is guessed. Two groups called "Labs" makes the course
  skip; two assignments with the same name leave both of them unmoved.

### When a course is skipped (nothing is changed in it)

- **The totals wouldn't match.** It has an extra *weighted* group the source doesn't have (e.g.
  "Extra Credit 10%" would make 110%). Set that group to 0% or rename it in Canvas, then preview again.
- **The course changed after you previewed it.** Preview again, so you always approve what's really there.
- **It's the source course.**

### Safety net for every course

1. A **snapshot** of the course's weighting setup is saved to `~/.canvas_command_center/snapshots/`.
   It holds group names, percentages and which group each assignment was in. It contains **no
   student data**, and only you can read it.
2. A **fingerprint** of every submission and grade is taken.
3. The edits are made one at a time, and the run stops at the first problem.
4. The course is **re-read** and checked. Every submission and grade must be identical, every
   assignment's details must be identical, and the weighting must match. If anything differs, the
   app shows **VERIFY FAILED** and stops before touching the next course.

### Undo

**History** tab → **Grading changes** → select a row → **Undo selected (restore weighting)**. You get
a normal preview showing what will be put back, then Apply.

- Undo restores the percentages, which group each assignment was in, and the on/off switch.
- Groups the app *created* can't be deleted (deleting is never allowed), so Undo sets them to **0%**.
  They're empty and harmless, and you can delete them in Canvas if you like.

**Note:** the check flags *any* grade change made during those few seconds, including a TA grading
at the same time. If you see VERIFY FAILED, look at the course's gradebook history in Canvas.

---

## 8. How it keeps your courses safe

The full design is in [docs/SAFETY.md](docs/SAFETY.md). In short:

**Sending and uploading cannot change existing content.**
All Canvas traffic goes through one small client (`ccc/canvas_client.py`). It runs in one of three
locked-down modes, and each tab gets only the mode it needs. For sending and uploading, it:

- only sends `GET` (read) and `POST` (create). `PUT`, `PATCH` and `DELETE` are blocked in code;
- only `POST`s to exactly three addresses:
  `/api/v1/courses/<id>/discussion_topics` (new announcement),
  `/api/v1/courses/<id>/assignments` (new assignment), and
  `/api/v1/courses/<id>/files` (new file, which must say "rename, never overwrite");
- sends file contents only to Canvas's known storage servers, and never with your token;
- only talks to the one HTTPS Canvas address you entered, with TLS certificates checked;
- never follows redirects, and never sends your token to any other server.

**The Publish tab uses its own client** that can make exactly two edits: an assignment's
`published` switch, or a file's `locked` switch. Nothing else about the item can be sent.

**The Grading tab uses a separate, stricter client** that can do only the four weighting edits.
Each edit is pinned to an exact address and an exact set of fields, and every value is checked.
It cannot reach anything to do with scores, submissions, points or dates, and it can't delete anything.
See [section 7](#7-copying-grade-weighting-grading-tab) and [docs/SAFETY.md](docs/SAFETY.md).

**It cannot post the same thing twice.** Four independent guards:

| Guard | What it stops |
|---|---|
| **Local ledger** (SQLite) | Each *(item, target course)* pair can be recorded once. The app writes the record **before** posting, and the database itself rejects a second record, even from two windows at once. |
| **Canvas title check** | Before posting, it reads the target course. If an item with the same title is already there (even one you made by hand), that course is skipped. This runs at preview **and** again right before sending. |
| **No automatic retries** | If Canvas doesn't confirm a post (timeout, dropped Wi-Fi, server error), the app **stops the whole run** and marks that course "pending". It never guesses. |
| **One window at a time** | A second copy of the app refuses to start. |

**It cannot flood Canvas or your students.**

| Limit | Default |
|---|---|
| Wait between any two posts | 3 seconds |
| Max posts per click | 25 |
| Max posts per rolling hour (survives restarts) | 40 |

Also: dry run is on by default, every real send needs a typed confirmation, and only
courses where you are a teacher are listed.

---

## 9. What gets copied (and what doesn't)

**Announcements:** the title and message (HTML formatting kept). They are posted as
**published**, so they appear immediately and Canvas sends its usual notification.

- Students get an **email** if their Canvas notification setting for *Announcements* says so.
  Canvas's default is to notify. Teachers cannot force an email.
- **Unpublished courses do not email anyone.** The preview warns you about this.
- Attachments are **not** copied, only the text (the preview warns you).

**Assignments:** name, description, points, grading type, submission types, allowed file
extensions, and due / available-from / available-until dates. You choose whether it is
**published** (tick box in step 4).

- It goes into the target course's default assignment group.
- **Not copied:** rubrics, group settings, peer reviews, and course-specific grading schemes.
- **Refused** (they need Canvas's own *Copy to…* feature): quizzes, graded discussions,
  external-tool (LTI) assignments, and annotation assignments.

**Links inside the text:** if the text links to a file or page *inside the source course*,
students in other courses may not be able to open it. The preview warns you when it sees such links.

**Sections vs. courses:** this tool treats each Canvas **course** as a destination. That fits
the common setup where each section is its own course shell (e.g. "MA 121 A", "MA 121 B"). If
several sections share **one** course shell, use Canvas's built-in "Post to: sections" option instead.

---

## 10. If something goes wrong

| You see | What it means | What to do |
|---|---|---|
| **Skip: Already sent by this tool** | The History tab has a record of this exact send. | Nothing. It's protecting you. |
| **Skip: An item with this exact title already exists there** | The target course already has it. | Nothing. Rename the source item if you really want a second one. |
| **! Check: Outcome unknown** | Canvas didn't confirm (network dropped, timeout, server error). The app stopped. | Open that course in Canvas. **If the item is there:** you're done; the app keeps blocking it. **If it is not there:** run `python3 -m ccc release <ID>` (ID from the History tab), then send again. |
| **Failed: HTTP 403** | You lack permission in that course (e.g. a TA role). | Nothing was created. Fix the role or skip that course. Failed sends may be retried. |
| **Failed: HTTP 401** | Token invalid or expired. The run stops. | Make a new token. |
| **Failed: HTTP 429** | Canvas is throttling. The run stops. | Wait a few minutes. |
| **Not sent: Hourly limit reached** | 40 posts in the last 60 minutes. | Wait, or see [Settings](#12-settings). |
| "Another Canvas Command Center window is already open" | A second copy was started. | Use the open window. |
| Grading: **Skipped: Weights would total 110%...** | The course has an extra weighted group the source doesn't have. | Set it to 0% or rename it in Canvas, then preview again. |
| Grading: **Skipped: changed since the preview** | Someone edited the course after you previewed. | Preview again. |
| Grading: **Failed: Edit 5 of 7... was refused** | Canvas refused one edit, e.g. an assignment in a **closed grading period**. The run stopped. | The edits before it were applied. Fix the cause, then preview again to finish, or use **Undo**. |
| Grading: **! Check Canvas (may or may not have been applied)** | The connection dropped mid-edit. | Preview again. The preview reads the real state, so nothing is duplicated. |
| Grading: **VERIFY FAILED** | A score or submission differed after the change (usually someone grading at the same moment). The run stopped. | Check the gradebook history in Canvas. Undo is available. |
| Files: **Skip: A file named … is already in …** | That course already has a file with that name in that folder. | Nothing, or choose a different folder or file name. |
| Files: **Skip: already uploaded here by this tool** | The history shows this exact file (same contents) was already uploaded there. | Nothing. It's protecting you. |
| Files: **Failed: unrecognised server** | Canvas asked to store the file somewhere not on the known list. Nothing was sent there. | If your school hosts its own Canvas storage, add that host to `CCC_EXTRA_UPLOAD_HOSTS`. |
| Files: **! Check Canvas** | The connection dropped during an upload. The run stopped. | Look in that course's Files. If the file isn't there, use `python3 -m ccc release <ID>` and upload again. |
| Publish: **Skip: Students have already submitted work** | Canvas won't unpublish an assignment with submissions. | Nothing. Their work is protected. |
| Source: **does not use weighted assignment groups** | Weighting is off in the source course. | Turn it on in Canvas: Assignments → ⋮ → Assignment Groups Weight. |

---

## 11. Command reference

```bash
python3 -m ccc                    # open the app (real Canvas)
python3 -m ccc gui --simulate     # open the app against a fake Canvas
python3 -m ccc simulate           # narrated simulation in the terminal
python3 -m ccc simulate-grading   # narrated grade-weighting simulation
python3 -m ccc ledger             # print the send and grading history
python3 -m ccc release <ID>       # forget one history row (asks you to type RELEASE)
python3 -m unittest discover -s tests -v    # run the 103 tests
```

---

## 12. Settings

Environment variables (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `CANVAS_URL` | - | Pre-fills the Canvas address |
| `CANVAS_API_TOKEN` | - | Used if the token box is left empty |
| `CCC_MIN_SECONDS_BETWEEN_POSTS` | `3` | Spacing between posts |
| `CCC_MAX_POSTS_PER_RUN` | `25` | Max posts per click |
| `CCC_MAX_POSTS_PER_HOUR` | `40` | Max posts per rolling hour |
| `CCC_GRADING_MIN_SECONDS_BETWEEN_EDITS` | `0.5` | Spacing between grading edits |
| `CCC_GRADING_MAX_EDITS_PER_RUN` | `400` | Max grading edits per click |
| `CCC_GRADING_MAX_EDITS_PER_HOUR` | `1000` | Max grading edits per rolling hour |
| `CCC_MAX_UPLOAD_MB` | `250` | Largest file the Files tab will upload |
| `CCC_EXTRA_UPLOAD_HOSTS` | - | Extra allowed file-storage hosts (self-hosted Canvas only), comma-separated |
| `CCC_UPLOAD_MIN_SECONDS_BETWEEN` / `_MAX_PER_RUN` / `_MAX_PER_HOUR` | `3` / `50` / `100` | Upload limits |
| `CCC_PUBLISH_MIN_SECONDS_BETWEEN` / `_MAX_PER_RUN` / `_MAX_PER_HOUR` | `0.5` / `100` / `300` | Publish/unpublish limits |
| `CCC_HOME` | `~/.canvas_command_center` | Where the ledger, snapshots and lock file live |
| `CCC_UI_SCALE` | auto | Text and UI size, e.g. `1.4` |

Data on disk: `~/.canvas_command_center/ledger.db` (send and grading history, readable only by
you), `snapshots/` (weighting setups for Undo, no student data) and `app.lock`. **Do not delete `ledger.db`.** It is what remembers which courses already got each item.
(The Canvas title check would still catch most repeats, but the ledger is the primary guard.)

---

## 13. Tests and simulation

```bash
python3 -m unittest discover -s tests -v
```

There are **103 tests**, each described in plain English in [docs/TEST_CASES.md](docs/TEST_CASES.md).
They never touch the network, and the whole suite takes about 35 seconds.

- **Tests 1–40 (sending):** connection security, the ledger, the rate limits, the exact content
  posted, and end-to-end simulations including dropped connections and duplicate attempts.
- **Tests 41–70 (grading):** the edit allow-list, planning, applying, verification and undo. These
  include **randomized tests** that build 150 random course setups and inject failures at random
  moments. They check every time that grades and submissions are untouched.
- **Tests 71–100 (files and publishing):** the upload and publish allow-lists, every Canvas
  upload style, duplicate protection, lost connections, and a randomized mix of uploads and
  publish changes with injected failures.
- **Tests 101–103 (screen):** the scrolling fixes. They need a display and are skipped without one.

For an even longer randomized run (1,000 random setups per randomized test):
`CCC_RANDOM_ROUNDS=1000 python3 -m unittest discover -s tests -p test_grading.py`

---

## 14. Project layout

```
canvas_command_center/
├── README.md               ← you are here
├── docs/
│   ├── SAFETY.md           ← detailed safety design
│   ├── TEST_CASES.md       ← the 103 tests, explained
│   └── FEATURE_PROPOSALS.md ← ideas for future versions
├── ccc/
│   ├── canvas_client.py    ← the ONLY code that talks to Canvas (allow-list lives here)
│   ├── copier.py           ← preview + send logic
│   ├── grading.py          ← grade weighting: preview, apply, verify, undo
│   ├── files.py            ← uploading files: preview, upload, verify
│   ├── visibility.py       ← publish / unpublish: preview, apply, verify, undo
│   ├── gui_files.py        ← the Files and Publish tabs
│   ├── gui_grading.py      ← the Grading tab
│   ├── ledger.py           ← local send history / duplicate guard
│   ├── rate_limiter.py     ← spacing + caps
│   ├── lock.py             ← one-window-at-a-time lock
│   ├── config.py           ← limits and paths
│   ├── gui.py              ← the window
│   ├── simulator.py        ← fake Canvas for tests and simulation mode
│   ├── demo.py             ← narrated terminal simulation
│   └── __main__.py         ← command line
└── tests/                  ← 103 tests
```

---

## 15. FAQ

**Can it delete or replace my files?** No. Uploads always ask Canvas to rename on a name clash,
never overwrite, and DELETE can't be sent at all (tests 71, 74, 91).

**Scrolling over a drop-down used to switch courses. Is that fixed?** Yes. Drop-downs now ignore
the mouse wheel; click them to choose. Lists scroll one row per wheel notch, and scrollbars are
easier to see and only appear when a list is longer than its box (tests 101–103).

**Can it delete or edit my announcements?** No. The Send tab's client refuses every request
except reads and the two "create" calls (tests 5, 6 and 40). The Grading tab's client can make
only the four weighting edits (tests 41–44). Neither can delete anything.

**Can the Grading tab change a student's grade on an assignment?** No. It has no way to send
scores, and after each course it re-reads every submission and confirms that nothing changed
(tests 56, 63, 68, 69). Course *totals* change, because that's what weighting does.

**Can I set different weights per section?** Yes. Set up each pattern in a source course and
copy it to the sections that should share it. Any section can serve as the source for others.

**What if I click Send twice?** The button is disabled while sending, and a used plan cannot
be sent again. If you preview and send the same item again, every course shows *Skip*.

**Can I send the same announcement to the same course again on purpose?** Not directly. That's
the point. The history tracks each item by its Canvas ID, so renaming it doesn't help. Create a
*new* announcement in the source course (e.g. "Reminder: Week 12") and send that.

**Does it work with any Canvas school?** Yes, as long as API tokens are allowed (they usually are).

**Does anything go to the internet besides my Canvas?** No. The app only contacts the Canvas
address you type in.
