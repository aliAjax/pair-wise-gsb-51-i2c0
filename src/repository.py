"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound
from .rules import EXCEPTION_VOID_REASONS


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
                    expires_at TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    reviewed_by TEXT,
                    review_decision TEXT,
                    review_note TEXT NOT NULL DEFAULT '',
                    reviewed_at TEXT,
                    consumed_record_version INTEGER,
                    void_reason TEXT,
                    voided_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_exceptions_record ON exceptions(record_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_exceptions_open
                    ON exceptions(record_id) WHERE state = 'pending'
                        OR (state = 'approved' AND consumed_record_version IS NULL);
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(audit_events)").fetchall()}
            if "exception_id" not in columns:
                connection.execute("ALTER TABLE audit_events ADD COLUMN exception_id INTEGER")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_audit_exception ON audit_events(exception_id)")

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _exception_row(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def _insert_audit(self, connection: sqlite3.Connection, record_id: int, actor_id: str, action: str,
                      version: int, details: Dict[str, Any], now: str,
                      exception_id: Optional[int] = None) -> None:
        connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at,exception_id) VALUES(?,?,?,?,?,?,?)",
            (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now, exception_id),
        )

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                self._insert_audit(connection, record_id, actor_id, "created", 1, {"state": state}, now)
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

    # ---- 例外审定 ----------------------------------------------------------

    def create_exception(self, record_id: int, reason: str, expires_at: str, actor_id: str) -> Dict[str, Any]:
        """发起例外。同一贷款只允许存在一张待审/通过中的例外（部分唯一索引兜底）。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM records WHERE id=?", (record_id,)).fetchone() is None:
                connection.rollback()
                raise NotFound("记录不存在")
            open_row = connection.execute(
                "SELECT id,state FROM exceptions WHERE record_id=? "
                "AND (state='pending' OR (state='approved' AND consumed_record_version IS NULL))",
                (record_id,),
            ).fetchone()
            if open_row is not None:
                connection.rollback()
                raise Conflict("该贷款已存在待审或有效的例外申请，待其结束后再发起")
            try:
                cursor = connection.execute(
                    "INSERT INTO exceptions(record_id,state,version,reason,expires_at,requested_by,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (record_id, "pending", 1, reason, expires_at, actor_id, now, now),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("该贷款已有待审例外") from exc
            exception_id = int(cursor.lastrowid)
            self._insert_audit(
                connection, record_id, actor_id, "exception_requested", 1,
                {"summary": "例外申请发起", "exception_id": exception_id, "state": "pending",
                 "reason": reason, "expires_at": expires_at, "requested_by": actor_id}, now,
                exception_id=exception_id,
            )
            row = connection.execute("SELECT * FROM exceptions WHERE id=?", (exception_id,)).fetchone()
            connection.commit()
        return self._exception_row(row)

    def get_exception(self, exception_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM exceptions WHERE id=?", (exception_id,)).fetchone()
        if row is None:
            raise NotFound("例外申请不存在")
        return self._exception_row(row)

    def list_exceptions(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM exceptions WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [self._exception_row(row) for row in rows]

    def review_exception(self, exception_id: int, expected_version: int, reviewer_id: str,
                         approved: bool, review_note: str) -> Dict[str, Any]:
        """另一名复核人审定；发起人不得复核自己的申请。"""
        now = _now()
        new_state = "approved" if approved else "rejected"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM exceptions WHERE id=?", (exception_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("例外申请不存在")
            exception = self._exception_row(row)
            if exception["requested_by"] == reviewer_id:
                connection.rollback()
                raise Conflict("发起人不能复核自己的例外申请")
            if exception["state"] != "pending":
                connection.rollback()
                raise Conflict("例外申请当前状态为%s，无法审定" % exception["state"])
            if int(exception["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE exceptions SET state=?,version=?,reviewed_by=?,review_decision=?,review_note=?,reviewed_at=?,updated_at=? WHERE id=?",
                (new_state, version, reviewer_id, "approve" if approved else "reject", review_note, now, now, exception_id),
            )
            self._insert_audit(
                connection, exception["record_id"], reviewer_id,
                "exception_approved" if approved else "exception_rejected", version,
                {"summary": "例外复核通过" if approved else "例外复核驳回", "exception_id": exception_id,
                 "state": new_state, "reviewed_by": reviewer_id, "review_note": review_note,
                 "expires_at": exception["expires_at"]}, now,
                exception_id=exception_id,
            )
            result = connection.execute("SELECT * FROM exceptions WHERE id=?", (exception_id,)).fetchone()
            connection.commit()
        return self._exception_row(result)

    def sweep_expired_exceptions(self, today: str) -> List[Dict[str, Any]]:
        """把到期日早于今天的待审/有效例外置为失效(过期)。惰性触发，结果进时间线。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM exceptions WHERE state IN ('pending','approved') "
                "AND consumed_record_version IS NULL AND expires_at < ? ORDER BY id",
                (today,),
            ).fetchall()
            expired = [self._exception_row(row) for row in rows]
            for exception in expired:
                version = int(exception["version"]) + 1
                connection.execute(
                    "UPDATE exceptions SET state='voided',version=?,void_reason='expired',voided_at=?,updated_at=? WHERE id=?",
                    (version, now, now, exception["id"]),
                )
                self._insert_audit(
                    connection, exception["record_id"], "system", "exception_voided", version,
                    {"summary": "例外过期，自动失效", "exception_id": exception["id"], "state": "voided",
                     "void_reason": "expired", "void_reason_label": EXCEPTION_VOID_REASONS["expired"],
                     "expires_at": exception["expires_at"], "requested_by": exception["requested_by"],
                     "reviewed_by": exception["reviewed_by"]}, now,
                    exception_id=exception["id"],
                )
            connection.commit()
        return expired

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str,
               action: str, details: Dict[str, Any], consume_exception_id: Optional[int] = None,
               void_approved_reason: Optional[str] = None) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            current = self._row(row)
            if int(current["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1

            consumed = None
            if consume_exception_id is not None:
                exc_row = connection.execute("SELECT * FROM exceptions WHERE id=?", (consume_exception_id,)).fetchone()
                if exc_row is None:
                    connection.rollback()
                    raise NotFound("例外申请不存在")
                consumed = self._exception_row(exc_row)
                if consumed["record_id"] != record_id or consumed["state"] != "approved" \
                        or consumed["consumed_record_version"] is not None:
                    connection.rollback()
                    raise Conflict("该例外不可用于批准本笔方案")
                connection.execute(
                    "UPDATE exceptions SET consumed_record_version=?,updated_at=? WHERE id=?",
                    (version, now, consumed["id"]),
                )
                self._insert_audit(
                    connection, record_id, actor_id, "exception_consumed", version,
                    {"summary": "例外用于批准本笔方案", "exception_id": consumed["id"],
                     "reviewed_by": consumed["reviewed_by"], "expires_at": consumed["expires_at"]}, now,
                    exception_id=consumed["id"],
                )

            # 方案违约：该贷款已通过的例外（含用于批准本方案的那张）一并失效
            if void_approved_reason:
                open_rows = connection.execute(
                    "SELECT * FROM exceptions WHERE record_id=? AND state='approved'",
                    (record_id,),
                ).fetchall()
                label = EXCEPTION_VOID_REASONS.get(void_approved_reason, void_approved_reason)
                for item in [self._exception_row(r) for r in open_rows]:
                    exc_version = int(item["version"]) + 1
                    connection.execute(
                        "UPDATE exceptions SET state='voided',version=?,void_reason=?,voided_at=?,updated_at=? WHERE id=?",
                        (exc_version, void_approved_reason, now, now, item["id"]),
                    )
                    self._insert_audit(
                        connection, record_id, actor_id, "exception_voided", exc_version,
                        {"summary": "%s，例外自动失效" % label, "exception_id": item["id"], "state": "voided",
                         "void_reason": void_approved_reason, "void_reason_label": label,
                         "expires_at": item["expires_at"], "reviewed_by": item["reviewed_by"]}, now,
                        exception_id=item["id"],
                    )

            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            action_details = dict(details)
            if consumed is not None:
                action_details["exception_id"] = consumed["id"]
                action_details["exception_reviewed_by"] = consumed["reviewed_by"]
                action_details["exception_expires_at"] = consumed["expires_at"]
            self._insert_audit(connection, record_id, actor_id, action, version, action_details, now)
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

    def exception_history(self, exception_id: int) -> List[Dict[str, Any]]:
        exception = self.get_exception(exception_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events WHERE exception_id=? ORDER BY id", (exception_id,)
            ).fetchall()
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
