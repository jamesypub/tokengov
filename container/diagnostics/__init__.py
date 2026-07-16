"""tg diagnostics check-engine.

A reusable, read-only health-check engine: each check returns a typed
CheckResult; run_all() runs a selected set inside try/except (a raising
check becomes status="error", never aborts the run) and returns one wire
object. This single engine powers all diagnostics surfaces (the
GET /api/diagnostics endpoint, and later the CLI + web page) so they can
never disagree.

Nothing here mutates AWS or the DB — every AWS call is Get*/List*/
Describe* (audited by a unit test asserting the mocked boto3 stub only
receives read verbs).
"""
from diagnostics.model import CheckResult, run_all

__all__ = ["CheckResult", "run_all"]
