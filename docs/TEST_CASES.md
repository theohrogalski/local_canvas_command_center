# The 103 test cases

| Tests | Cover |
|---|---|
| 1–40 | Send tab (announcements and assignments) |
| 41–70 | Grading tab (grade weighting) |
| 71–100 | Files tab (uploads) and Publish tab (publish/unpublish) |
| 101–103 | The scrolling fixes, run in a hidden window (skipped if there's no display) |

Run them all (about 35 seconds, no network):

```bash
python3 -m unittest discover -s tests -v
```

Tests 31–40 are **simulations**. They run the real app logic against `ccc/simulator.py`, a
fake Canvas with five "MA 121" sections, a student-only course, sample announcements, and
assignments (including a quiz and an item that links to a source-course file). The fake
server records every request it receives, so tests can prove what was and wasn't sent.

## Connection and request safety (`tests/test_client.py`)

| # | Test | What it proves |
|---|---|---|
| 1 | Base URL is normalised | `https://School.Instructure.com/` → `https://school.instructure.com`; ports kept. |
| 2 | Plain HTTP rejected | `http://` addresses are refused, so the token is never sent unencrypted. |
| 3 | Paths, queries, credentials rejected | Only a bare `https://host` is accepted as the Canvas address. |
| 4 | Token validation | Empty tokens and tokens containing spaces or newlines (header injection) are refused. |
| 5 | PUT / PATCH / DELETE blocked | Any method other than GET/POST raises an error **before** the network is touched. |
| 6 | POST allow-list | Edit endpoints (`/assignments/5`, `/courses/1`, `/discussion_topics/9`, enrollments, accounts) are refused. Only the two create endpoints work. |
| 7 | Path traversal blocked | `..`, encoded slashes, non-API paths and `/api/v2` are refused. |
| 8 | Pagination works | All 5 courses are found across 3 pages of results. |
| 9 | Pagination can't leak the token | A "next page" link to another host is refused without contacting it. |
| 10 | Token handling | The token appears only in the Authorization header, never in URLs or `repr()`. |
| 11 | HTTP errors are definite | 401 and 403 errors are reported as "definitely not applied". |
| 12 | Ambiguous failures detected | A POST that gets a 5xx or a network error is flagged "outcome unknown". A failed GET is not. |
| 13 | Redirects not followed | A 302 response is treated as an error, not followed. |

## Ledger, rate limits, lock (`tests/test_ledger_and_limits.py`)

| # | Test | What it proves |
|---|---|---|
| 14 | Reserve creates a pending row | A send is recorded *before* it is posted. |
| 15 | Second reservation refused | The same item can't be reserved twice for the same course. |
| 16 | Sent row blocks resend | A completed send blocks forever. A *different* course is still allowed. |
| 17 | Failed row may be retried | A definite rejection (nothing created) can be tried again. |
| 18 | Survives restart | Closing and reopening the app keeps the history and the protection. |
| 19 | File is private | The ledger file is readable only by you (`0600`). |
| 20 | Release | After manual verification, a row can be released to allow a resend. |
| 21 | Concurrent race | 8 simultaneous attempts to reserve the same send produce exactly 1 winner. |
| 22 | Minimum spacing | The second post waits for the remainder of the 3 s gap. No wait if enough time passed. |
| 23 | Per-run cap | The (N+1)th post in a run is refused. A new run may continue. |
| 24 | Hourly cap survives restart | A "restarted" limiter still sees the last hour's posts. The window rolls over after 60 min. |
| 25 | One instance only | A second app instance cannot acquire the lock. |

## Exactly what gets posted (`tests/test_payloads.py`)

| # | Test | What it proves |
|---|---|---|
| 26 | Announcement payload is exact | Only title, message, `is_announcement=true`, `published=true`. Delays, locks and user IDs are dropped. |
| 27 | Assignment payload | Name, points, due date, submission types and extensions are mapped. IDs, assignment group, rubric, URLs and empty dates are not sent. |
| 28 | Publish flag | The "Publish assignment" option controls `assignment[published]`. |
| 29 | Unsupported types refused | Quizzes, graded discussions, external tools, annotations and untitled items are refused. |
| 30 | Source-link warning | Links into the source course (e.g. `/courses/101/files/...`) or attachments trigger a warning. Similar-looking IDs (`/courses/1010/`) don't. |

## End-to-end simulations (`tests/test_simulation.py`)

| # | Test | What it proves |
|---|---|---|
| 31 | Only your teaching courses | Courses where you are a student are not listed. |
| 32 | Dry run is harmless | Zero POSTs, zero ledger rows, zero emails. |
| 33 | Real send | Exactly one copy per target course, the source is untouched, emails go only to published courses, and every row is "sent". |
| 34 | Resend makes zero posts | A fresh preview shows all "Skip". Re-running an old, stale preview also posts nothing. |
| 35 | Existing title skipped | Section C already has Homework 7, so it is skipped. An item added in Canvas *between preview and send* is also caught (case and space insensitive). |
| 36 | Source and repeated targets | The source course is skipped, a course listed 3 times gets 1 post, and an empty selection is refused. |
| 37 | Network drop after create | Canvas creates the item but the reply is lost. The app stops at once, marks it pending, never retries, and the next attempt skips that course. Every course ends with exactly 1 copy. |
| 38 | Permission error | One course returns 403. It is marked failed, the others are sent, and a later retry sends only that course, once. |
| 39 | Rate limits in action | An oversized selection is refused at preview. The hourly cap stops a run cleanly (2 sent, 2 not sent, no stray reservations, spacing waits applied). |
| 40 | Nothing existing is ever modified | The full narrated simulation plus every action type: only GET and allow-listed POSTs were sent, every original item is unchanged, and no course has any title twice. |

## Grading: the edit allow-list (`tests/test_grading.py`)

| # | Test | What it proves |
|---|---|---|
| 41 | Only four edits, exact fields | The four weighting edits pass. Edits with any extra or different field are refused: points, due date, course name, group rules, renaming, a group with no weight, or a field sent twice. |
| 42 | Values validated | Weights must be numbers from 0 to 1000 (no negatives, `1e9` or text). IDs must be digits, the on/off switch must be `true`/`false`, and group names can't be blank or contain line breaks. |
| 43 | No path to grades or content | Changing a submission grade, bulk grade updates, creating announcements or assignments, enrollments, DELETE and PATCH are all refused **before** anything is sent. |
| 44 | Send tab can't edit | The Send tab's client can't make any of the four grading edits, and the grading service refuses to run with a send-mode client. |

## Grading: planning (preview is read-only)

| # | Test | What it proves |
|---|---|---|
| 45 | Source must be valid | A source with weighting off, or with two groups of the same name, is refused. |
| 46 | Safe order | A fresh course gets: create groups → move assignments → switch weighting on (last). |
| 47 | Existing groups | When groups already exist, only their percentages change. No new groups are made, and no switch flips. |
| 48 | Already matching | A course that already matches needs zero changes. |
| 49 | Totals must match | An extra 10% "Extra Credit" group skips the course (with the reason). Once it's 0%, the course proceeds. |
| 50 | Never guesses | Two groups called "Labs" → course skipped. Two assignments with the same name → neither is moved, with a warning. |
| 51 | Forgiving matching | Names match regardless of capitals and spaces. Assignments not in the source that won't count (0% group) are flagged. |
| 52 | Drop rules | Drop rules in the source are flagged in the preview and never sent. |
| 53 | Preview writes nothing | Previewing every course sends zero edits. A plan bigger than the per-run limit is refused. |

## Grading: applying, verifying, undoing (simulation)

| # | Test | What it proves |
|---|---|---|
| 54 | Dry run | Nothing changes and no snapshots are written. |
| 55 | Correct result, idempotent | Sections end with Homework 20 / Labs 25 / Exams 55, weighting on, and the right assignments moved. Assignments not in the source stay put. A second run makes zero edits. |
| 56 | Grades preserved | Every submission is identical before and after (deep comparison), as are all assignment details except the group. Each course reports "submissions verified unchanged". |
| 57 | Only allowed writes | Every write that reached Canvas matches the allow-list exactly. No group was deleted, and skipped, already-matching and source courses received **zero** writes. |
| 58 | Stale preview | If someone edits a course after the preview, that course is skipped and nothing is written to it. |
| 59 | Snapshot first | The snapshot exists even when the first edit fails. It is private (`0600`) and contains no scores, grades or student IDs. |
| 60 | Undo is exact | Undo restores the switch, every original weight, and every assignment's group. Created groups go to 0%, nothing is deleted, and grades are untouched. |
| 61 | Refused edit | A closed-grading-period refusal stops the run (the next course is not run) and grades are untouched. After fixing it, a re-run finishes the job, and Undo from the first snapshot restores the original. |
| 62 | Lost reply | Canvas creates a group but the reply is lost. The run stops as "may or may not". Re-running does **not** create a second group. |
| 63 | Verification works | If Canvas changes a score during the change, the result is VERIFY FAILED and later courses are untouched. The fingerprint catches score, grade, excused, missing, state, file and new-submission changes, but ignores the clock ticking. |
| 64 | Own budget, spaced | Grading edits are spaced out and counted separately, so a used-up announcement budget doesn't block grading (and vice versa). |
| 65 | Hourly cap | Hitting the hourly cap mid-course stops cleanly. Grades are untouched and the snapshot is available for Undo. |
| 66 | Send stays add-only | The Send tab's client still can't make any weighting edit. |
| 67 | Upgrade-safe | A ledger file from version 1.0 upgrades automatically without losing its rate-limit history. |

## Grading: randomized and fault-injection tests

Each runs **150 randomly generated setups** by default. Set `CCC_RANDOM_ROUNDS` for more.

| # | Test | What it proves |
|---|---|---|
| 68 | Random courses | Random groups (mixed-case names, extras, duplicates), random weights including totals that aren't 100%, drop rules, random assignments, and 0–6 students per course. Every setup is checked for: grades untouched, only allowed writes, no groups deleted, results matching the source, skipped courses fully untouched, a re-run making zero changes, and Undo restoring everything. |
| 69 | Random failures | At a random edit, it injects a lost reply, a dropped connection, 500/503, or a 403/400/429 refusal. Grades stay untouched, no duplicate groups appear, a second run converges, and Undo from the first snapshot restores the original. |
| 70 | Preview fidelity | The weighting read from the source matches it exactly, and previews never write anything. |

## Files and Publish: the allow-lists (`tests/test_files_publish.py`)

| # | Test | What it proves |
|---|---|---|
| 71 | Upload request fields | The upload request must have exactly the expected fields. `on_duplicate=overwrite` is refused, and so are extra fields, names with slashes, negative or huge sizes, odd content types, and `../` folder paths. |
| 72 | Known storage hosts only | Canvas, Instructure and inscloudgate storage hosts are accepted. Lookalike domains, plain http, odd ports, embedded usernames and fake S3 buckets are refused. `CCC_EXTRA_UPLOAD_HOSTS` adds exact hosts only. |
| 73 | Token stays home | The upload carries no Authorization header and no token anywhere. A bad host receives nothing, and other modes can't upload. |
| 74 | Publish mode is one switch | Only `assignment[published]` and file `locked` are allowed. Renaming, points, hidden, moving, weighting, creating, PATCH and DELETE are refused. Send and grading modes can't publish. |
| 75 | Folder paths | Paths are cleaned (spaces, slashes); `..`, `.`, line breaks, too-deep and too-long paths are refused. |
| 76 | Confirmation stays on Canvas | Upload confirmation follows only addresses on the Canvas host, with the token. Other hosts and non-API paths are refused. |

## Files: uploading (simulation)

| # | Test | What it proves |
|---|---|---|
| 77 | Dry run | Nothing is uploaded, no requests are made, no history rows are written. |
| 78 | One intact copy | Each selected course gets exactly one copy with identical bytes (SHA-256), in the requested sub-folder. Unselected courses get nothing. |
| 79 | Hidden uploads | Uploads are locked afterwards and verified, using only the `locked` field. |
| 80 | All reply styles | JSON, redirect-then-confirm, and 201-with-Location all work. |
| 81 | Never twice | Re-uploading the same file, even renamed, sends nothing. |
| 82 | Name clash skipped | A file with the same name (any case) already in that folder is skipped. A different folder is fine. |
| 83 | Edited after preview | A file changed on disk after the preview is skipped. |
| 84 | Lost reply | The file is stored but the reply is lost: the run stops and the row stays pending. A re-run uploads the rest, never a second copy. |
| 85 | Refusals | A refused upload request is recorded and retried later, for that course only. A bad token stops the run. |
| 86 | Unknown storage server | Refused before any bytes are sent; the run stops. |
| 87 | Unconfirmed upload | Held as pending and the run stops. |
| 88 | Ambiguous selections | Two different files with the same name are refused. The same file added twice is uploaded once. |
| 89 | Bad inputs | Missing files, bad folders, an empty selection and oversized files are refused at preview, with nothing sent. |
| 90 | Rate limits | Oversized plans are refused. The hourly cap stops cleanly with no stray reservation. Uploads have their own budget. |
| 91 | Nothing else changes | Assignments, announcements, groups, submissions and existing files are all untouched. Only allowed writes are sent, and no token goes to storage. |

## Publish: publish/unpublish (simulation)

| # | Test | What it proves |
|---|---|---|
| 92 | Smart skipping | Skips: already in that state, has submissions (unpublish), quizzes, and items that no longer exist. Duplicate selections are removed. Previews write nothing. |
| 93 | Assignments | Publish and unpublish work. Every submission and every other assignment field is identical, and only `assignment[published]` is sent. |
| 94 | Files | Publish and unpublish via `locked` only; names are unchanged. |
| 95 | Changed after preview | Skipped, nothing sent. |
| 96 | Failures | One refusal doesn't stop the others; an unclear result stops the run. |
| 97 | Verification | If Canvas says "OK" but the change didn't stick, the result is VERIFY FAILED and is recorded. |
| 98 | Undo | Each change is recorded and Undo restores the previous state. |
| 99 | Old ledgers | A ledger file from version 1 upgrades, keeping its rows and their duplicate protection. Upgrading twice is harmless. |
| 100 | Randomized | 60 random mixes of uploads and publish changes, with failures injected at random. Every time: no duplicate files, no grade changes, only allowed writes, and no token sent to storage. |

## The scrolling fixes (`tests/test_gui_scroll.py`)

| # | Test | What it proves |
|---|---|---|
| 101 | Drop-downs ignore the wheel | Turning the mouse wheel over the course drop-downs (Send, Grading and Publish tabs) never changes the selected course. |
| 102 | One row per notch | Linux and Windows wheel events move lists one row per notch (two notches = two rows). A tiny macOS movement still moves one row. |
| 103 | Scrollbars only when needed | A scrollbar is hidden when the list fits and appears when it overflows. |

## Also verified manually

- The GUI was driven end to end in simulation mode:
  - Send tab: connect, preview, dry run, real send, blocked resend, refused quiz.
  - Grading tab: preview, dry run, apply, History, Undo.
  - Files tab: two files to five sections, unpublished, all verified.
  - Publish tab: publish a draft, then Undo from History.
  - Closing the window mid-run: it waits for the current post to finish, then stops (3 of 4 sent, none lost).
- **Sabotage checks.** Deliberately broken builds confirm the tests catch real bugs:

  | Bug introduced | Tests that fail |
  |---|---|
  | Duplicate guards disabled | 33–35, 37–40 |
  | Grading allow-list disabled | 41–43 |
  | Assignment move also resets points | 14 tests, including 56, 57, 68, 69 |
  | Grade verification disabled | 56, 63 |
  | Groups always created, never matched | 14 tests, including 46–48, 62, 68, 69 |
  | Stale-preview check weakened | 58 |
  | Uploads allowed to overwrite | 71, 78, 91, 100 |
  | Token sent to the storage server | 73, 84, 86, 91, 100 |
  | Storage-host check disabled | 72, 73, 86 |
  | File history check disabled | 81 (the name check still blocks the duplicate) |
  | Scroll fixes removed | 101, 102 |
