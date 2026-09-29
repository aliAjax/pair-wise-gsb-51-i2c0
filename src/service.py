"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, integer, text, today_iso
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
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    # ---- 例外审定 ----------------------------------------------------------

    def request_exception(self, actor: Actor, record_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        """经办人对评估不通过的贷款发起例外，填写理由与到期日。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_request_exception(actor.role):
            raise PermissionDenied("角色无权发起例外申请")
        record = self.repository.get(record_id)
        if record["state"] != "assessed":
            raise Conflict("只有评估完成的贷款才能发起例外申请")
        if record["payload"].get("eligibility"):
            raise Conflict("该贷款满足偿付能力，无需例外申请")
        validated = self.rules.validate_exception_request(payload or {})
        self.repository.sweep_expired_exceptions(today_iso())
        return self.repository.create_exception(record_id, validated["reason"], validated["expires_at"], actor.user_id)

    def review_exception(self, actor: Actor, exception_id: int, expected_version: int,
                         payload: Dict[str, Any]) -> Dict[str, Any]:
        """另一名复核人审定例外；发起人不能批准/驳回自己的申请。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_review_exception(actor.role):
            raise PermissionDenied("角色无权复核例外申请")
        approved, note = self.rules.validate_exception_review(payload or {})
        self.repository.sweep_expired_exceptions(today_iso())
        return self.repository.review_exception(
            exception_id=exception_id,
            expected_version=int(expected_version),
            reviewer_id=actor.user_id,
            approved=approved,
            review_note=note,
        )

    def list_exceptions(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.sweep_expired_exceptions(today_iso())
        return self.repository.list_exceptions(record_id)

    def get_exception(self, actor: Actor, exception_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.sweep_expired_exceptions(today_iso())
        exception = self.repository.get_exception(exception_id)
        result = dict(exception)
        result["history"] = self.repository.exception_history(exception_id)
        return result

    # ---- 业务动作 ----------------------------------------------------------

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)

        consume_exception_id = None
        if action == "approve" and not record["payload"].get("eligibility"):
            # 评估不满足偿付能力：必须凭另一人复核通过且在有效期内的例外，审批人才能放行
            self.repository.sweep_expired_exceptions(today_iso())
            exception_id = integer(data or {}, "exception_id", minimum=1)
            exception = self.repository.get_exception(exception_id)
            if exception["record_id"] != record_id:
                raise Conflict("例外不属于本笔贷款")
            if not self.rules.exception_is_effective(exception, today_iso()):
                if exception["state"] != "approved":
                    raise Conflict("例外状态为%s，无法据此批准" % exception["state"])
                if exception["consumed_record_version"] is not None:
                    raise Conflict("该例外已被使用，无法重复放行")
                raise Conflict("例外已于%s过期" % exception["expires_at"])
            consume_exception_id = exception_id

        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})

        void_reason = "defaulted" if action == "default" else None
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
            consume_exception_id=consume_exception_id,
            void_approved_reason=void_reason,
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
