"""Canvas Command Center - safely copy announcements and assignments between
Canvas courses (sections) you teach.

Safety model (see docs/SAFETY.md):
  * Only HTTP GET and POST are ever issued; POST only to two "create" endpoints.
  * Every send is reserved in a local SQLite ledger *before* it is posted, so the
    same item can never be sent to the same course twice.
  * Target courses are checked for an item with the same title before posting.
  * Posting is rate limited (min spacing, per-run cap, rolling per-hour cap).
  * POSTs are never retried automatically and redirects are never followed.
"""

__version__ = "1.0.0"
