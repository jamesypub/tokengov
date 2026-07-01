"""
#643: high-water-mark helpers for timestamp-based CDC ingestion.

A stream records how far it has consumed a source in
`sync_state.last_synced_through` (UTC). The next run queries events
strictly after that watermark and advances it — in the SAME
transaction as the upserts — so a crash before commit re-runs the
window (a set/replace of that window's rows, not a blind +=) and no
data is lost or double-counted.

#761: the original `metrics_aggregator` watermark stream was retired
with that job (#725 — CUR/`cur_spend_sync` is the spend source now).
The dead `METRICS_STREAM` default constant is removed; these helpers
are generic, so `name` is now required (no current caller, but the
SyncState table + this CDC primitive are kept for any future
watermarked stream).
"""
from __future__ import annotations
from datetime import datetime
from sqlalchemy.orm import Session

from db.models import SyncState


def get_watermark(db: Session, name: str):
    """Return the last_synced_through datetime for a stream, or
    None when the stream has never run (first-ever ingestion)."""
    row = (
        db.query(SyncState)
        .filter(SyncState.name == name)
        .first()
    )
    return row.last_synced_through if row else None


def set_watermark(
    db: Session, through: datetime, name: str
) -> None:
    """Advance (upsert) a stream's watermark. Caller MUST do this
    inside the same transaction/session as the rows the watermark
    accounts for, so the advance and the writes commit atomically
    (advance-after-write; crash → re-run the window)."""
    row = (
        db.query(SyncState)
        .filter(SyncState.name == name)
        .first()
    )
    if row:
        row.last_synced_through = through
    else:
        db.add(SyncState(name=name, last_synced_through=through))
    db.flush()
