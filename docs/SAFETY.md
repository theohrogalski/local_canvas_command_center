# Safety design

This page explains *how* Canvas Command Center keeps its promises.

**Send tab** (announcements and assignments):

1. **It only adds.** It never changes or removes anything in any course.
2. **It adds each item to each course at most once.**

**Files tab** (uploads):

5. **Uploads only add.** They never overwrite, replace or delete a file.
6. **The same file is never uploaded to the same course twice, and the token never leaves Canvas.**

**Publish tab:**

7. **Only the published/unpublished switch changes**, never grades or content, and each change can be undone.

**Grading tab** (grade weighting):

3. **Only weighting changes. Scores, grades and submissions never do**, and this is verified after every course.
4. **Every grading change can be undone**, and nothing is ever deleted.

Each guarantee is enforced in code and covered by tests (test numbers in brackets).

Each tab gets a **separate client object** in its own mode, and each mode has its own allow-list:

| Mode | Used by | Can do |
|---|---|---|
| `send` | Send and Files tabs | Create announcements, assignments and files. No edits at all. |
| `publish` | Publish tab (and hiding a just-uploaded file) | Flip one assignment's or file's published state. |
| `grading` | Grading tab | The four weighting edits. |

No mode can send PATCH or DELETE [5, 43, 44, 66, 74].

---

## Promise 1: the Send tab only adds

### One gatekeeper

`ccc/canvas_client.py` → `SafeCanvasClient._request()` is the only function that sends
anything to Canvas. Every request passes these checks **before** any network activity:

| Check | Rule | Test |
|---|---|---|
| Method | Only `GET` and `POST`. Anything else raises `SafetyViolation`. | 5 |
| POST target | The path must exactly match `^/api/v1/courses/<digits>/discussion_topics$` or `^/api/v1/courses/<digits>/assignments$`. Those are Canvas's "create new" endpoints. Edit endpoints (`.../assignments/<id>`) are rejected. | 6 |
| Path shape | Only `/api/v1/` paths made of letters, digits, `_`, `-`, `/`. No `..`, no `%` encodings. | 7 |
| Host | Only the HTTPS host you connected to. Pagination links to other hosts are refused, so the token can't leak. | 1–3, 9 |
| Redirects | Never followed (a redirected POST could land somewhere unexpected). | 13 |
| TLS | Python's default verified context (certificate + hostname checks). | - |
| Token | Sent only in the `Authorization` header, never in URLs, logs or `repr()`. Never saved to disk. | 4, 10 |

### What a send actually posts

Announcement (`ccc/copier.py` → `announcement_form`), four fields only:

```
title, message, is_announcement=true, published=true
```

Assignment (`assignment_form`): `assignment[...]` fields for name, description, points,
grading type, submission types, allowed extensions, dates and published. Nothing that
references an ID in the source course is sent: no assignment group, rubric or grading standard. [26–28]

### End-to-end proof

Test 40 runs the full narrated simulation plus every action type. It then checks that the
fake Canvas received only `GET`s and allow-listed `POST`s, that every pre-existing item in
every course is still present and unchanged, and that no course holds any title twice.

---

## Promise 2: at most once per course

A "send" means: *this kind* of item, *this source item*, to *this target course*.

### Guard A: the ledger (`ccc/ledger.py`)

A local SQLite file with a `UNIQUE(kind, source_course, source_item, target_course)` constraint.

The send sequence for each course is:

```
1. ledger says "already sent / pending"?        → skip
2. Canvas already has an item with this title?  → skip
3. RESERVE in ledger (atomic, BEGIN IMMEDIATE)  → if someone else reserved it: skip
4. rate limiter: wait / or stop the run         → on stop, un-reserve (nothing was posted)
5. POST once
6. success → mark "sent" (+ new Canvas id)
   Canvas said no (4xx) → mark "failed" (retry allowed later, nothing was created)
   no clear answer (timeout, 5xx, dropped connection) → stays "pending", STOP the whole run
```

Because the reservation (step 3) happens **before** the POST (step 5), a crash, power cut or
network drop at any moment leaves a row that blocks a repeat. The worst case is "not sent and
needs manual release". It is never "sent twice". [14–18, 20, 37]

The database constraint holds even when two processes race. Eight threads reserving the same
send produce exactly one winner. [21]

### Guard B: the Canvas title check

Before previewing **and** again right before each POST, the app reads the target course's
announcements or assignments. If an item with the same title is there (ignoring case and
extra spaces), the course is skipped. This covers items created by hand, from another
computer, or before the ledger existed. [35]

### Guard C: no automatic retries

A POST is attempted once. An *ambiguous* failure (the request may have reached Canvas) stops
the run immediately, and the remaining courses are reported "Not sent". [12, 37]

A *definite* rejection (`4xx`: Canvas refused, nothing created) is recorded as `failed`. The
run continues for other courses, except `401` (bad token) and `429` (throttled), which stop it.
A failed send may be retried later. [11, 38]

### Guard D: one app at a time

`ccc/lock.py` holds an OS file lock (`flock` on Linux/macOS, `msvcrt` on Windows). A second
window refuses to start. [25]

### Guard E: the GUI

- **Dry run** is on by default.
- **Send** is disabled until a preview exists, and while any task runs.
- Changing the item, courses or options throws the preview away.
- A preview built for an old selection is discarded on arrival.
- A preview is consumed by one send.
- A real send requires typing the number of target courses.

---

## Rate limits (`ccc/rate_limiter.py`)

| Limit | Default | Persisted across restarts? | Test |
|---|---|---|---|
| Minimum spacing between POSTs | 3 s | yes (uses ledger timestamps) | 22 |
| POSTs per click | 25 (also refused at preview time if exceeded) | n/a | 23, 39 |
| POSTs per rolling 60 min | 40 | yes | 24, 39 |

Every POST *attempt* is logged in the ledger's `post_attempts` table, so closing and reopening
the app does not reset the hourly limit. Limits can be lowered with environment variables
(see README §9).

---

## Data stored on your computer

| File | Contents | Permissions |
|---|---|---|
| `~/.canvas_command_center/ledger.db` | Send history: item titles, course IDs, timestamps, new item IDs. No token, no student data. | `0600` (owner only) [19] |
| `~/.canvas_command_center/app.lock` | Empty lock file | - |

The directory is created with `0700` permissions.

---

## Known limits (by design)

- **Email delivery is controlled by Canvas and each student's notification preferences.** The
  app creates a published announcement, which is the standard trigger. It cannot force an email.
- **Releasing a pending row** (`python3 -m ccc release <ID>`) is deliberately a separate,
  typed-confirmation command, not a GUI button.
- **The ledger tracks items by Canvas ID, not by title.** Renaming the source item (or the copy
  in a target course) does not make it sendable again. To send a deliberate second copy,
  create a new item in the source course.


---

## Promise 3: grading changes weighting only, never grades

### The grading allow-list (`ccc/canvas_client.py` → `GRADING_RULES`)

In grading mode, a write is sent only if its method, its path **and its exact set of form
fields** match one of these four rules, and every value passes validation [41, 42]:

| Edit | Request | The only field allowed | Value check |
|---|---|---|---|
| Weighting on/off | `PUT /api/v1/courses/<id>` | `course[apply_assignment_group_weights]` | `true` / `false` |
| Create a group | `POST /api/v1/courses/<id>/assignment_groups` | `name`, `group_weight` | printable name ≤ 255 chars; 0–1000, ≤ 4 decimals |
| Set a group's weight | `PUT /api/v1/courses/<id>/assignment_groups/<id>` | `group_weight` | 0–1000, ≤ 4 decimals |
| Move an assignment | `PUT /api/v1/courses/<id>/assignments/<id>` | `assignment[assignment_group_id]` | digits only |

Everything else is refused before the network is touched, including:
- extra fields such as `assignment[points_possible]`, group `rules`, or renaming;
- any submissions or grades endpoint;
- `DELETE` and `PATCH` [43].

### Verification after every course (`ccc/grading.py` → `_execute_course`)

Per course, in this order:

1. **Re-read and compare with the preview.** If anything you were shown differs, including a
   "before" value, the course is skipped and nothing is written to it [58].
2. **Fingerprint all submissions:** a SHA-256 hash over every submission's id, student,
   assignment, score, grade, entered score/grade, excused, workflow state, submitted time,
   attempt, late/missing status, points deducted, submission type, graded time and attached file IDs.
   If submissions can't be read, nothing is changed.
3. **Save the snapshot** (see Promise 4).
4. **Apply the edits one at a time** through the rate limiter. The order is: create groups → move
   assignments → set weights → switch weighting on. So weighting is switched on only once everything
   is in place [46].
5. **Re-read everything.** The submission fingerprint must be identical, every assignment's
   non-group details must be identical, and a fresh preview must show zero remaining changes.
   Otherwise the result is **VERIFY FAILED** and the whole run stops [56, 63].

`seconds_late` is deliberately **not** part of the fingerprint. Canvas keeps increasing it for
missing work as time passes, so it would cause false alarms. Late/missing status and deductions are checked [63].

### Failure handling

| What happens | Result |
|---|---|
| Canvas refuses an edit (4xx, e.g. closed grading period) | That course stops; the run stops. The message says how many edits were applied. Preview again to finish, or Undo [61]. |
| The connection drops / times out / 5xx | Reported as "may or may not have been applied"; the run stops. Because every plan is computed from what is *actually* in Canvas, previewing again never duplicates anything, including groups [62, 69]. |
| Rate limit reached | Stops cleanly; snapshot kept for Undo [65]. |

### When a course is skipped instead of changed

- A group exists in the target but not the source **and carries weight**, so the totals wouldn't
  match. The app never deletes it or sets it to 0% on its own [49].
- Two target groups share a matching name (ambiguous) [50].
- The source course has weighting off, or duplicate group names. This is refused at preview [45].

---

## Promise 4: grading changes can be undone

Before the first edit to a course, `save_snapshot()` writes
`~/.canvas_command_center/snapshots/course<ID>-<time>.json` (permissions `0600`). It holds the
weighting switch, every group's id, name and weight, and every assignment's id, name and group.
It holds **no student data** [59].

**Undo** (History tab) builds a normal preview from the snapshot:
- the weighting switch, every original group's weight, and every assignment's group are put back;
- groups created afterwards are set to 0%, because nothing is ever deleted [60].

Undo itself is snapshotted and verified like any other change, so it can also be undone.

---

## How rigorously grading is tested

- **Specific scenarios (tests 41–67):** the allow-list, planning edge cases, apply/verify/undo,
  refused edits, lost replies, stale previews, Canvas-side grade changes, rate limits, and
  ledger upgrades.
- **Randomized (tests 68–70).** These generate 150 random course setups per test (configurable).
  The setups vary group names in mixed case, extra groups, duplicate names, totals that aren't
  100%, random weights, drop rules, random assignments and 0–6 students. Every setup is checked for:
  - grades and submissions identical;
  - only allow-listed writes sent;
  - no group deleted;
  - the result matching the source;
  - a second run making zero changes;
  - Undo restoring every original setting.
- **Fault injection (test 69).** At a random edit, it injects a lost reply, a dropped connection,
  500/503 errors, or a 403/400/429 refusal. It then checks that grades are intact, no duplicate
  groups exist, a second run finishes the job, and Undo returns to the original.
- **The tests themselves were checked by sabotage.** Each of five deliberate bugs was caught:
  - allow-list disabled;
  - a move that also resets points;
  - verification disabled;
  - groups always created;
  - stale check weakened.

### Known limits

- **Drop rules** ("drop lowest 1") are not copied. The preview warns when the source has them.
- **Grading periods** with their own weights are not changed by this tool.
- Assignments are matched by **name**. Give assignments the same names across sections for the
  best results; unmatched ones are left where they are and reported.


---

## Promises 5 and 6: uploads only add, once per course, and the token stays home

### The upload request (`check_upload_preflight`)

Step 1 of a Canvas upload asks Canvas where to put the file. In `send` mode,
`POST /api/v1/courses/<id>/files` is allowed only with exactly these fields [71]:

| Field | Rule |
|---|---|
| `name`, `size`, `content_type`, `on_duplicate` | Required. |
| `name` | A plain file name: no slashes, at most 255 characters. |
| `size` | Digits, up to the size cap. |
| `content_type` | Must look like `type/subtype`. |
| `on_duplicate` | Must be **`rename`**. `overwrite` is refused. |
| `parent_folder_path` | Optional. A cleaned path, so no `..`, no control characters, at most 10 levels. |

### The bytes (`upload_file_data`)

Step 2 sends the file to the storage server Canvas named:
- **Known hosts only.** The host must be the Canvas host itself, `*.instructure.com`,
  `*.inscloudgate.net`, or Instructure's S3 upload buckets, plus any hosts you list in
  `CCC_EXTRA_UPLOAD_HOSTS`. Lookalikes such as `inscloudgate.net.evil.com`, plain http, odd ports and
  embedded usernames are refused **before a single byte is sent** [72, 73, 86].
- **No token.** The request carries no `Authorization` header at all [73, 91, 100].
- **No redirects followed, no retries.**

### Confirmation (`confirm_upload`)

Step 3 handles all three ways Canvas replies: JSON, a 3xx redirect, or 201 with a Location
header [80]. Any follow-up request goes only to the Canvas host; any other address is refused [76].

### Duplicate protection for files

| Guard | How it works |
|---|---|
| **Ledger** | Keyed by the file's **contents** (SHA-256) plus the target course. The same file under a new name is still blocked [81]. |
| **Name check** | A course that already has a file with that name in that folder (any capitalisation) is skipped [82]. |
| **Changed-file check** | The file is re-hashed right before uploading. If it changed after the preview, it is skipped [83]. |
| **Failures** | A refused upload request is definite and may be retried [85]. A lost reply after the bytes were sent stays *pending* and stops the run, so a re-run never duplicates [84, 87]. |

**After each upload:** the file is read back. Its size and its published/unpublished state must
match, otherwise the result is VERIFY FAILED and the run stops.

**Hidden uploads:** these are uploaded, then immediately locked with the one `publish`-mode edit.
If locking fails, the result says plainly that students may see the file [79].

---

## Promise 7: Publish changes one switch only

The `publish` mode allows exactly [74]:

| Request | The only field allowed |
|---|---|
| `PUT /api/v1/courses/<id>/assignments/<id>` | `assignment[published]` (`true` / `false`) |
| `PUT /api/v1/files/<id>` | `locked` (`true` / `false`) |

**Per item:**
1. **Skip when there's nothing to do or it isn't allowed:**
   - already in that state;
   - an assignment with student submissions when unpublishing, which Canvas also refuses;
   - quizzes and graded discussions.
   [92]
2. **Skip anything that changed after the preview** [95].
3. **Assignments:** fingerprint every submission before the change and compare it after [93].
4. **Re-read the item** to confirm the new state. If it didn't stick, the result is VERIFY FAILED
   and the run stops [97].
5. **Record the change** in `visibility_changes`. History → Undo builds the reverse change, with a
   preview [98].

A refusal of one item doesn't stop the others. An unclear result (timeout, server error) stops the
run [96].

---

## Fixes from review (this version)

| Area | Fix | Test |
|---|---|---|
| **Scrolling** | Turning the mouse wheel over a course drop-down used to **switch the selected course**: Tk's default behaviour, verified in the app. Drop-downs now ignore the wheel. | 101 |
| **Scrolling** | Lists moved 5 rows per notch, more than a whole list at large text sizes. Now 1 row per notch. | 102 |
| **Scrolling** | Scrollbars are clearly visible and appear only when needed. | 103 |
| **Closing** | Closing the window during a run used to close the ledger under the running task. It now waits for the current step to finish and be recorded. | Checked manually |
| **Old ledgers** | Ledgers from earlier versions upgrade in one all-or-nothing transaction, keeping every row and its protection. | 99 |
