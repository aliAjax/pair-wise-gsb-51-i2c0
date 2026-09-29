"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS exceptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    reason TEXT NOT NULL,
                    expires_on TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    reviewed_by TEXT,
                    review_note TEXT,
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    invalidated_at TEXT,
                    invalidate_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_exceptions_one_pending
                    ON exceptions(record_id) WHERE state = 'pending';
                CREATE INDEX IF NOT EXISTS idx_exceptions_record ON exceptions(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_exceptions_expiry
                    ON exceptions(state, expires_at);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _exception_row(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def create_exception(
        self,
        record_id: int,
        reason: str,
        expires_on: str,
        expires_at: str,
        actor_id: str,
    ) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = connection.execute("SELECT id, version FROM records WHERE id=?", (record_id,)).fetchone()
            if record is None:
                connection.rollback()
                raise NotFound("记录不存在")
            expired_rows = connection.execute(
                """
                SELECT id, version FROM exceptions
                WHERE record_id=? AND state IN ('pending','approved') AND expires_at<=?
                """,
                (record_id, now),
            ).fetchall()
            for item in expired_rows:
                connection.execute(
                    "UPDATE exceptions SET state='expired',version=?,invalidated_at=?,invalidate_reason=? WHERE id=?",
                    (int(item["version"]) + 1, now, "有效期届满", item["id"]),
                )
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        record_id,
                        "exception_expired",
                        "system",
                        int(record["version"]),
                        json.dumps(
                            {"exception_id": item["id"], "reason": "有效期届满", "at": now},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
            pending = connection.execute(
                "SELECT id FROM exceptions WHERE record_id=? AND state='pending'",
                (record_id,),
            ).fetchone()
            if pending is not None:
                connection.rollback()
                raise Conflict("同一贷款已有待审例外")
            active = connection.execute(
                "SELECT id FROM exceptions WHERE record_id=? AND state='approved' AND expires_at>?",
                (record_id, now),
            ).fetchone()
            if active is not None:
                connection.rollback()
                raise Conflict("该贷款已有有效例外审定")
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO exceptions(record_id,state,version,reason,expires_on,expires_at,requested_by,created_at)
                    VALUES(?,?,1,?,?,?,?,?)
                    """,
                    (record_id, "pending", reason, expires_on, expires_at, actor_id, now),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("同一贷款只能保留一张待审例外") from exc
            exception_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (
                    record_id,
                    "exception_requested",
                    actor_id,
                    int(record["version"]),
                    json.dumps(
                        {
                            "exception_id": exception_id,
                            "state": "pending",
                            "reason": reason,
                            "expires_on": expires_on,
                            "expires_at": expires_at,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM exceptions WHERE id=?", (exception_id,)).fetchone()
            connection.commit()
        return self._exception_row(row)

    def review_exception(
        self,
        exception_id: int,
        expected_version: int,
        reviewer_id: str,
        decision: str,
        review_note: str,
    ) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM exceptions WHERE id=?", (exception_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("例外审定不存在")
            if row["state"] != "pending":
                connection.rollback()
                raise Conflict("仅待审例外可以复核")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            if decision == "approved" and row["expires_at"] <= now:
                connection.rollback()
                raise Conflict("例外有效期已过，不能复核通过")
            record = connection.execute("SELECT version FROM records WHERE id=?", (row["record_id"],)).fetchone()
            connection.execute(
                """
                UPDATE exceptions
                SET state=?,version=?,reviewed_by=?,review_note=?,reviewed_at=?
                WHERE id=?
                """,
                (decision, int(row["version"]) + 1, reviewer_id, review_note, now, exception_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (
                    int(row["record_id"]),
                    "exception_reviewed",
                    reviewer_id,
                    int(record["version"]),
                    json.dumps(
                        {
                            "exception_id": exception_id,
                            "state": decision,
                            "review_note": review_note,
                            "expires_on": row["expires_on"],
                            "expires_at": row["expires_at"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            result = connection.execute("SELECT * FROM exceptions WHERE id=?", (exception_id,)).fetchone()
            connection.commit()
        return self._exception_row(result)

    def expire_due_exceptions(self) -> List[Dict[str, Any]]:
        now = _now()
        expired = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT e.*, r.version AS record_version
                FROM exceptions e JOIN records r ON r.id=e.record_id
                WHERE e.state IN ('pending','approved') AND e.expires_at<=?
                ORDER BY e.id
                """,
                (now,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE exceptions SET state='expired',version=?,invalidated_at=?,invalidate_reason=? WHERE id=?",
                    (int(row["version"]) + 1, now, "有效期届满", row["id"]),
                )
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        int(row["record_id"]),
                        "exception_expired",
                        "system",
                        int(row["record_version"]),
                        json.dumps(
                            {"exception_id": int(row["id"]), "reason": "有效期届满", "at": now},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
                expired.append(self._exception_row(row))
            connection.commit()
        return expired

    def get_exception(self, exception_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM exceptions WHERE id=?", (exception_id,)).fetchone()
        if row is None:
            raise NotFound("例外审定不存在")
        return self._exception_row(row)

    def list_exceptions(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM exceptions WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [self._exception_row(row) for row in rows]

    def find_pending_exception(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM exceptions WHERE record_id=? AND state='pending' ORDER BY id DESC LIMIT 1",
                (record_id,),
            ).fetchone()
        return self._exception_row(row) if row is not None else None

    def get_active_exception(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM exceptions
                WHERE record_id=? AND state='approved' AND expires_at>?
                ORDER BY id DESC LIMIT 1
                """,
                (record_id, _now()),
            ).fetchone()
        return self._exception_row(row) if row is not None else None

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], invalidate_exception: Dict[str, Any] = None, required_exception_id: int = None) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            if required_exception_id is not None:
                exception_row = connection.execute(
                    "SELECT id FROM exceptions WHERE id=? AND record_id=? AND state='approved' AND expires_at>?",
                    (int(required_exception_id), record_id, now),
                ).fetchone()
                if exception_row is None:
                    connection.rollback()
                    raise Conflict("例外审定已失效，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            if invalidate_exception is not None:
                exception_rows = connection.execute(
                    "SELECT id, state, version FROM exceptions WHERE record_id=? AND state='approved'",
                    (record_id,),
                ).fetchall()
                reason = invalidate_exception.get("reason", "方案违约")
                for exception_row in exception_rows:
                    connection.execute(
                        "UPDATE exceptions SET state='invalidated',version=?,invalidated_at=?,invalidate_reason=? WHERE id=?",
                        (int(exception_row["version"]) + 1, now, reason, exception_row["id"]),
                    )
                    connection.execute(
                        "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                        (
                            record_id,
                            "exception_invalidated",
                            actor_id,
                            version,
                            json.dumps(
                                {"exception_id": int(exception_row["id"]), "reason": reason, "at": now, "by_action": action},
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            now,
                        ),
                    )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
