# Feature proposals

Three review agents studied the code and docs, each from a different angle:
- teaching workflow and communication;
- grading, assessment and insight;
- safety, reliability, usability and operations.

This page merges their proposals into one ranked list. **None of these is built yet.** Each one
would follow the existing pattern: preview → dry run → typed confirmation, an exact allow-list,
verification, and undo where possible.

Effort: **S** = about a day, **M** = a few days, **L** = a week or more. Canvas API details marked
*(verify)* were flagged as uncertain by the agents. Check them against a test course before building.

---

## Recommended next (best value for the effort)

### 1. Section Compare / Drift Checker (read-only) - S/M effort, High value
*Proposed independently by two agents.* Pick a reference section and others. One grid shows what
is **missing or different** in each section:
- announcements, assignments, pages and discussions;
- points, due dates, published state, group weights and drop rules;
- late policy.

Each difference links to the tab that fixes it ("Send missing…").

- **API:** GET only:
  - `/courses/:id/assignments`
  - `/courses/:id/assignment_groups`
  - `/courses/:id/discussion_topics`
  - `/courses/:id/pages`
  - `/courses/:id/late_policy`
  - `/courses/:id`
- **Safety:** runs on a new GET-only `report` client mode, so it physically cannot write. No
  student data is involved.

### 2. Scheduled announcements - S effort, High value
Add a "Post at" date and time to the Send tab, so announcements go out before class.
- **API:** the endpoint already allowed, plus `delayed_post_at`. Students are notified when it posts.
- **Safety:**
  - the announcement form grows by exactly one field, which must be a date in the future
    (no more than about 180 days ahead);
  - the preview shows the course's time zone;
  - the title check must also see delayed announcements that haven't posted yet *(verify)*.
- **Undo** would need one tightly limited delete rule: only items this app created, and only
  before they go live.

### 3. Saved section groups and a filter box - S effort, High value
Save a group such as "My MA 121 sections" and tick it in one click, and type to filter long
course and item lists. Groups only pre-tick boxes; every safety step still applies.

### 4. Per-section variables and date offsets - S/M effort, High value
Placeholders like `{{section}}`, `{{room}}` and `{{meeting_day}}`, plus a day offset per section
(e.g. the Tue/Thu section is due one day later), filled in when an item is copied.
- **No new endpoints.** It only changes the text and dates locally before the existing POSTs.
- **Safety:** sending is blocked if any `{{…}}` is left unfilled, and the preview shows each
  section's final text and dates.

### 5. Separate history per Canvas address - S effort, High value (correctness)
Today the history doesn't record *which* Canvas server a send went to. Schools' `*.beta` and
`*.test` copies of Canvas reuse real course IDs. So a practice send on the test server would block
the real send on production, and Undo snapshots would get mixed together. Fix: keep one history
folder per Canvas address. This doesn't affect anyone who only ever uses one Canvas address.

### 6. Gradebook snapshot and change diff (read-only to Canvas) - S/M effort, High value
"Snapshot now" saves a timestamped local copy of every score in the chosen sections. "Compare"
shows exactly which scores changed between two snapshots, which helps settle grade disputes and
explain a VERIFY FAILED result. It reuses the existing submission fingerprint fields.
- **Privacy:** the most sensitive file the app would keep.
  - Stored: student IDs and names, scores, grades and late/missing status only.
  - Owner-only permissions, and old snapshots are deleted automatically.
  - The docs must say plainly that the files are protected by file permissions, not encryption.

### 7. Missing and at-risk dashboard (read-only) - M effort, High value
Missing and late work, and low scores, across all sections in one sortable table.
- **API:** `GET /students/submissions?student_ids[]=all` and
  `GET /enrollments?type[]=StudentEnrollment`.
- **Safety:** data is kept in memory only. CSV export is opt-in, with a warning that it contains grades.

### 8. Token in the OS keychain - M effort, High value
An opt-in "Remember on this computer" option, using macOS Keychain, Linux Secret Service or Windows
Credential Manager, and never a plain file. It also replaces the README's environment-variable
suggestion, which in practice puts the token in plain text in a shell profile. A 401 error would
lead straight to reconnecting.

---

## Valuable, but higher risk or effort

| Feature | Effort / value | Main concern |
|---|---|---|
| **Message students via Canvas Inbox**, e.g. "everyone who hasn't submitted HW 7", one private message each (`POST /conversations`, `bulk_message`) | M / High | Can't be recalled and reaches students directly. Needs strict recipient checks, a typed confirmation of the student count, and once-a-day de-duplication. |
| **Copy pages** (syllabus, lab instructions) | S–M / Med-High | Canvas quietly accepts duplicate page titles, so the title check is essential. Create pages unpublished. |
| **Copy ungraded discussions** | S / Med | Must never carry announcement or assignment fields. May notify students. |
| **Calendar events to sections** | M / Med | The endpoint isn't course-scoped, so `context_code` must be strictly checked. Notification defaults *(verify)*. |
| **Copy a whole module and its items** | L / High | Many steps per course. Each item must match exactly one item in the target. |
| **Extensions / accommodations** (due-date overrides per student) | L / High | Sensitive (disability-related) data. Undo needs a delete limited to overrides this app created. Classic and New Quizzes use different APIs *(verify)*. |
| **Copy late/missing policy** | M / Med | Canvas **recalculates existing late grades** when the policy changes, which conflicts with "grades never change". It would need a declared-change verification mode. |
| **Copy rubrics** | M / Med | The nested request format is poorly documented *(verify)*. Only for assignments with no rubric and no graded work. |
| **Grade distribution comparison** | S / Med | Aggregates only. Could live inside the dashboard (item 7). |

---

## Usability and operations

- **One-click launchers and `ccc doctor`** (M, High for non-programmers): a double-clickable icon on
  Windows, Mac and Linux, plus a check of Python, Tk, folder permissions and certificates that
  prints fixes.
- **Exportable audit report** (S, Med-High): CSV or HTML of everything sent, uploaded, published or
  reweighted, for a chair or co-instructor. Titles, IDs and dates only, no student data.
- **Accessibility** (M, Med-High):
  - space/Enter now toggle courses on the Send tab and the new tabs; extend this to the Grading tab;
  - add keyboard shortcuts and a visible focus ring;
  - darken the green and amber status colours, which were reported below the WCAG AA contrast ratio;
  - add a high-contrast option.
- **Command-line mode for power users** (M, Med): `ccc plan …` and `ccc send … --confirm N`, using
  the same safety path, with dry run as the default and the token never on the command line.
- **Pending-row reconciler** (S-M, Med): at startup, list "outcome unknown" rows and offer a read-only
  "Check in Canvas" button that marks them sent or offers release.

---

## Weaknesses the agents found in the current code

| Finding | Status |
|---|---|
| Closing the window mid-run closed the ledger under the running task, so the result of the in-flight request was lost (it failed safe as "pending"). | **Fixed in this version.** Closing now waits for the current step to finish and be recorded. |
| "Already running" was printed to the terminal only, so it was invisible when the app was started from an icon. | **Fixed in this version** (a dialog is shown). |
| Course ticking was mouse-only. | **Partly fixed:** space bar toggles on the Send, Files and Publish tabs. The Grading tab is still to do. |
| History isn't separated per Canvas address (beta/test vs production). | Open. See proposal 5. |
| `ccc release` will also delete rows that are confirmed **sent**. It should refuse those unless forced. | Open. Small fix. |
| The table of rate-limit timestamps is never pruned (it grows slowly; harmless but untidy). | Open. Small fix. |
| The README suggests keeping the token in an environment variable, which tends to end up in a plain-text shell profile. | Open. See proposal 8. |
| No automatic backup of `ledger.db`. If it's lost, only the Canvas title check guards against duplicates. | Open. Add a rotating daily copy. |
| A one-course send is confirmed by typing just "1". | Open. Consider also typing the first word of the title. |
