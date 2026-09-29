"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, ValidationError, text
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.expire_due_exceptions()
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.expire_due_exceptions()
        record = self.repository.get(record_id)
        record["exceptions"] = self.repository.list_exceptions(record_id)
        active = self.repository.get_active_exception(record_id)
        record["active_exception"] = active
        return record

    def create_exception(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create_exception(actor.role):
            raise PermissionDenied("角色无权发起例外审定")
        record = self.repository.get(record_id)
        prepared = self.rules.validate_exception_request(record, data or {})
        return self.repository.create_exception(
            record_id=record_id,
            reason=prepared["reason"],
            expires_on=prepared["expires_on"],
            expires_at=prepared["expires_at"],
            actor_id=actor.user_id,
        )

    def review_exception(self, actor: Actor, exception_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_review_exception(actor.role):
            raise PermissionDenied("角色无权复核例外审定")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ValidationError("expected_version必须是整数")
        self.repository.expire_due_exceptions()
        exception = self.repository.get_exception(exception_id)
        prepared = self.rules.validate_exception_review(exception, actor, data or {})
        return self.repository.review_exception(
            exception_id=exception_id,
            expected_version=expected_version,
            reviewer_id=actor.user_id,
            decision=prepared["decision"],
            review_note=prepared["review_note"],
        )

    def list_exceptions(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.expire_due_exceptions()
        return self.repository.list_exceptions(record_id)

    def get_exception(self, actor: Actor, exception_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.expire_due_exceptions()
        return self.repository.get_exception(exception_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.repository.expire_due_exceptions()
        active_exception = self._action_exception(record, action)
        if active_exception is not None:
            if actor.user_id == active_exception["requested_by"]:
                raise PermissionDenied("发起人不能批准自己的例外申请对应方案")
            if active_exception.get("reviewed_by") and actor.user_id == active_exception["reviewed_by"]:
                raise PermissionDenied("复核人不能再作为审批人批准同一方案")
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {}, active_exception)
        invalidate_exception = None
        action_details = {"summary": summary, "input": data or {}, "from": record["state"], "to": new_state}
        if action == "default" and record["state"] == "active":
            invalidate_exception = {"reason": "方案违约"}
            approved = self.repository.get_active_exception(record_id)
            if approved is not None:
                invalidate_exception["id"] = approved["id"]
        elif action == "approve" and active_exception is not None:
            action_details["exception_id"] = active_exception["id"]
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details=action_details,
            invalidate_exception=invalidate_exception,
            required_exception_id=active_exception["id"] if active_exception is not None else None,
        )

    def _action_exception(self, record: Dict[str, Any], action: str) -> Optional[Dict[str, Any]]:
        if action != "approve" or record["payload"].get("eligibility") is not False:
            return None
        return self.repository.get_active_exception(record["id"])

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.expire_due_exceptions()
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
