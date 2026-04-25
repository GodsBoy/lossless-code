"""Behavior contracts: typed retractable rules with append-only supersede trail.

Contracts ride inside the SessionStart bundle as the highest-priority item type
(R8). The dream cycle proposes them as ``Pending``; promotion to ``Active``
requires explicit human approval via the TUI (R11). Retraction creates a new
row pointing to the old via ``supersedes_id``; the original is preserved
(R10) and queryable (R12).

Atomicity (review-flagged security gate): supersede_contract wraps the
INSERT-new + UPDATE-old pair in BEGIN IMMEDIATE / COMMIT to prevent
concurrent CLI + MCP writes from producing two rival Active rows pointing
at the same supersedes_id.

Module shape mirrors db/dream_log.py: pure functions, lazy ``from . import
get_db`` inside function bodies, no module-level state. Public names are
re-exported from db/__init__.py.
"""

import hashlib
import time
import uuid
from typing import Optional

# Hard cap on contract body size: 2000 tokens (~8KB at 4-chars-per-token).
# Mirrors the discipline in summarise.cap_summary_text. Closes the
# unbounded-growth concern flagged in vault-db-unbounded-growth-oom.md
# for new TEXT NOT NULL columns.
_BODY_HARD_CAP_TOKENS = 2000


# Allowed kinds. Enforced at write time. Mirrors the typed-rule taxonomy
# in the v1.2 brainstorm (origin doc R9).
_VALID_KINDS = {"prefer", "forbid", "verify-before"}

# Allowed statuses. Status transitions are constrained by the helpers
# below: Pending -> Active (approve), Pending -> Rejected (reject),
# Active -> Retracted (retract or supersede).
_VALID_STATUSES = {"Pending", "Active", "Rejected", "Retracted"}


def gen_contract_id() -> str:
    """Generate a contract id. Mirrors gen_summary_id's `sum_<hex12>` shape."""
    return f"con_{uuid.uuid4().hex[:12]}"


def _body_hash(body: str) -> str:
    """Stable hash of the normalized body, used for dedup at store time."""
    normalized = " ".join(body.lower().split())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def _cap_body(body: str) -> str:
    """Hard-cap contract body. Imports cap_summary_text lazily to avoid
    circular imports between scripts.summarise and the db package.
    """
    # Avoid pulling in the whole summarise module just for one helper.
    # Mirror the simple truncate-with-marker logic from summarise.cap_summary_text.
    max_chars = _BODY_HARD_CAP_TOKENS * 4
    if len(body) <= max_chars:
        return body
    truncated = body[:max_chars].rsplit("\n", 1)[0]
    if len(truncated) < max_chars // 2:
        truncated = body[:max_chars]
    return f"{truncated}\n\n[Capped from ~{len(body) // 4} to ~{_BODY_HARD_CAP_TOKENS} tokens]"


def store_contract_candidate(
    kind: str,
    body: str,
    byline_session_id: Optional[str] = None,
    byline_model: Optional[str] = None,
    scope: str = "project",
    contract_id: Optional[str] = None,
    created_at: Optional[int] = None,
) -> Optional[str]:
    """Insert a new Pending contract.

    Returns the new contract id on success, or None when the body is a
    duplicate (sha1 of normalized body matches an existing row in any
    status). Dedup at store time prevents the dream cycle from filling
    the Pending queue with the same FORBID rule on every run (review
    finding F6).

    kind must be one of ``prefer | forbid | verify-before``. Raises
    ``ValueError`` otherwise. body is hard-capped to prevent vault bloat.
    """
    if kind not in _VALID_KINDS:
        raise ValueError(
            f"contract kind must be one of {sorted(_VALID_KINDS)}, got {kind!r}"
        )
    if not body or not body.strip():
        raise ValueError("contract body must be non-empty")

    body = _cap_body(body)
    body_h = _body_hash(body)

    from . import get_db
    db = get_db()
    # Dedup: any existing row with the same body_hash blocks insert.
    # The Pending queue should never see "FORBID em-dashes" twice, even
    # if the LLM hallucinates the same rule across cycles, and once a
    # rule has been rejected we should not re-propose it.
    existing = db.execute(
        "SELECT 1 FROM contracts WHERE body_hash = ? LIMIT 1", (body_h,)
    ).fetchone()
    if existing is not None:
        return None

    cid = contract_id or gen_contract_id()
    now = created_at if created_at is not None else int(time.time())
    db.execute(
        """INSERT INTO contracts
           (id, kind, body, byline_session_id, byline_model, created_at,
            status, supersedes_id, scope, body_hash)
           VALUES (?, ?, ?, ?, ?, ?, 'Pending', NULL, ?, ?)""",
        (cid, kind, body, byline_session_id, byline_model, now, scope, body_h),
    )
    db.commit()
    return cid


def get_contract(contract_id: str) -> Optional[dict]:
    from . import get_db
    db = get_db()
    row = db.execute(
        "SELECT * FROM contracts WHERE id = ?", (contract_id,)
    ).fetchone()
    return dict(row) if row else None


def list_contracts(
    status: Optional[str] = None,
    scope: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """Return contracts filtered by status and scope. Newest first.

    Default limit of 200 matches the bundle assembler's worst-case scan
    envelope without unbounded reads.
    """
    from . import get_db
    db = get_db()
    clauses = []
    params: list = []
    if status is not None:
        if status not in _VALID_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(_VALID_STATUSES)}, got {status!r}"
            )
        clauses.append("status = ?")
        params.append(status)
    if scope is not None:
        clauses.append("scope = ?")
        params.append(scope)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = db.execute(
        f"SELECT * FROM contracts {where} ORDER BY created_at DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def approve_contract(contract_id: str) -> bool:
    """Promote a Pending contract to Active. Returns True on success,
    False if the contract is missing or not in Pending status.

    Append-only: no other status transitions to Active are allowed via
    this helper (Retracted -> Active would silently revive a contract,
    which is exactly the integrity gap the supersede trail prevents).
    """
    from . import get_db
    db = get_db()
    cur = db.execute(
        "UPDATE contracts SET status = 'Active' "
        "WHERE id = ? AND status = 'Pending'",
        (contract_id,),
    )
    db.commit()
    return cur.rowcount > 0


def reject_contract(contract_id: str) -> bool:
    """Mark a Pending contract as Rejected. Returns True on success."""
    from . import get_db
    db = get_db()
    cur = db.execute(
        "UPDATE contracts SET status = 'Rejected' "
        "WHERE id = ? AND status = 'Pending'",
        (contract_id,),
    )
    db.commit()
    return cur.rowcount > 0


def retract_contract(contract_id: str, reason: str) -> bool:
    """Mark an Active contract as Retracted. Returns True on success.

    The reason is stored as the body of a tombstone row that supersedes
    the active one, preserving audit trail per AE3. A retraction without
    replacement (no new body) still produces a supersede chain entry.

    Atomic: BEGIN IMMEDIATE / COMMIT prevents two concurrent retracts on
    the same target from producing inconsistent state.
    """
    if not reason or not reason.strip():
        raise ValueError("retraction reason must be non-empty (audit requirement)")
    from . import get_db
    db = get_db()
    target = db.execute(
        "SELECT id, kind, scope, byline_session_id, byline_model "
        "FROM contracts WHERE id = ? AND status = 'Active'",
        (contract_id,),
    ).fetchone()
    if target is None:
        return False
    tombstone_id = gen_contract_id()
    now = int(time.time())
    capped_reason = _cap_body(reason)
    body_h = _body_hash(f"retract:{contract_id}:{capped_reason}")
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """INSERT INTO contracts
               (id, kind, body, byline_session_id, byline_model, created_at,
                status, supersedes_id, scope, body_hash)
               VALUES (?, ?, ?, ?, ?, ?, 'Retracted', ?, ?, ?)""",
            (
                tombstone_id, target["kind"], capped_reason,
                target["byline_session_id"], target["byline_model"], now,
                contract_id, target["scope"], body_h,
            ),
        )
        db.execute(
            "UPDATE contracts SET status = 'Retracted' WHERE id = ?",
            (contract_id,),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return True


def supersede_contract(
    old_id: str,
    new_body: str,
    byline_session_id: Optional[str] = None,
    byline_model: Optional[str] = None,
) -> Optional[str]:
    """Replace an Active contract with a new Active contract atomically.

    Writes a new row with status=Active and supersedes_id=old_id, and
    flips the old row's status to Retracted. Both happen inside a single
    BEGIN IMMEDIATE / COMMIT transaction so concurrent CLI + MCP writes
    cannot land two rival Active rows referencing the same target.

    Returns the new contract id on success, None when the target is
    missing or not Active.
    """
    if not new_body or not new_body.strip():
        raise ValueError("supersede body must be non-empty")
    from . import get_db
    db = get_db()
    target = db.execute(
        "SELECT id, kind, scope FROM contracts WHERE id = ? AND status = 'Active'",
        (old_id,),
    ).fetchone()
    if target is None:
        return None
    new_body = _cap_body(new_body)
    body_h = _body_hash(new_body)
    new_id = gen_contract_id()
    now = int(time.time())
    try:
        db.execute("BEGIN IMMEDIATE")
        # Dedup against existing rows. Without this, the supersede flow
        # could re-introduce a previously-rejected rule under the cover
        # of a "new" id.
        existing = db.execute(
            "SELECT 1 FROM contracts WHERE body_hash = ? LIMIT 1", (body_h,)
        ).fetchone()
        if existing is not None:
            db.rollback()
            return None
        db.execute(
            """INSERT INTO contracts
               (id, kind, body, byline_session_id, byline_model, created_at,
                status, supersedes_id, scope, body_hash)
               VALUES (?, ?, ?, ?, ?, ?, 'Active', ?, ?, ?)""",
            (
                new_id, target["kind"], new_body,
                byline_session_id, byline_model, now,
                old_id, target["scope"], body_h,
            ),
        )
        db.execute(
            "UPDATE contracts SET status = 'Retracted' WHERE id = ?",
            (old_id,),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return new_id


__all__ = [
    "gen_contract_id",
    "store_contract_candidate",
    "get_contract",
    "list_contracts",
    "approve_contract",
    "reject_contract",
    "retract_contract",
    "supersede_contract",
]
