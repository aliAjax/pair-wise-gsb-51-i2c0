"""住房贷款纾困申请与履约跟踪领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, future_date, integer, number, text, text_list


INITIAL_STATE = "submitted"
CREATE_ROLES = {'intake_officer'}
EXCEPTION_REQUEST_ROLES = {'intake_officer'}
EXCEPTION_REVIEW_ROLES = {'exception_reviewer'}
ACTION_ROLES = {'assess': {'intake_officer'}, 'approve': {'underwriter'}, 'activate': {'servicer'}, 'cure': {'servicer'}, 'default': {'servicer'}}
TRANSITIONS = {'assess': {'submitted': 'assessed'}, 'approve': {'assessed': 'approved'}, 'activate': {'approved': 'active'}, 'cure': {'active': 'cured'}, 'default': {'active': 'defaulted'}}

# 例外状态：pending待审 approved复核通过 rejected复核驳回 voided失效(过期/方案违约)
EXCEPTION_TERMINAL_STATES = {'rejected', 'voided'}
EXCEPTION_VOID_REASONS = {'expired': '例外过期', 'defaulted': '方案违约'}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES) | set(EXCEPTION_REQUEST_ROLES) | set(EXCEPTION_REVIEW_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_request_exception(self, role: str) -> bool:
        return role == "admin" or role in EXCEPTION_REQUEST_ROLES

    def role_can_review_exception(self, role: str) -> bool:
        return role == "admin" or role in EXCEPTION_REVIEW_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        income = number(p, "monthly_income", 1)
        number(p, "monthly_expenses", 0)
        payment = number(p, "monthly_payment", 0)
        number(p, "arrears", 0)
        number(p, "hardship_factor", 0, 1)
        choice(p, "program_type", ["deferral", "reduction", "restructure"])
        integer(p, "requested_months", 1, 24)
        if p["monthly_expenses"] >= income:
            raise ValidationError("支出不能达到或超过收入")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        income = float(p["monthly_income"])
        disposable = income - float(p["monthly_expenses"])
        ratio = float(p["monthly_payment"]) / income
        months = min(int(p["requested_months"]), 12)
        if p["program_type"] == "deferral":
            proposed = 0.0
        elif p["program_type"] == "reduction":
            proposed = max(0.0, float(p["monthly_payment"]) - disposable * 0.4)
        else:
            proposed = max(float(p["monthly_payment"]) * 0.7, disposable * 0.25)
        p["disposable_income"] = round(disposable, 2)
        p["housing_ratio"] = round(ratio, 3)
        p["eligible_months"] = months
        p["proposed_payment"] = round(proposed, 2)
        p["risk_score"] = round(min(100.0, ratio * 60 + float(p["hardship_factor"]) * 40), 2)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"active", "approved", "assessed"} and item["payload"].get("borrower_id") == payload.get("borrower_id"):
                raise Conflict("该借款人已有处理中纾困申请")

    def validate_exception_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """经办人发起例外：必填理由与到期日。"""
        reason = text(payload or {}, "reason")
        if len(reason) < 5:
            raise ValidationError("例外理由至少需要5个字符")
        return {"reason": reason, "expires_at": future_date(payload or {}, "expires_at")}

    def validate_exception_review(self, payload: Dict[str, Any]) -> Tuple[bool, str]:
        """复核人审定：approve/reject，驳回须填写意见，通过时意见可选。"""
        decision = choice(payload or {}, "decision", ["approve", "reject"])
        if decision == "reject":
            note = text(payload or {}, "review_note")
        else:
            note = (payload or {}).get("review_note", "")
            note = note.strip() if isinstance(note, str) else ""
        return decision == "approve", note

    @staticmethod
    def exception_is_effective(exception: Dict[str, Any], today: str) -> bool:
        """复核通过、未被方案使用且未到期，方为有效例外。"""
        return (
            exception["state"] == "approved"
            and exception["consumed_record_version"] is None
            and exception["expires_at"] >= today
        )

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "assess":
            changes["assessment_note"] = text(data, "assessment_note")
            changes["eligibility"] = bool(float(p["housing_ratio"]) <= 0.8 and float(p["arrears"]) <= float(p["monthly_payment"]) * 6)
            summary = "偿付能力评估完成"
        elif action == "approve":
            changes["approved_program"] = p["program_type"]
            changes["approved_months"] = int(p["eligible_months"])
            changes["approved_payment"] = float(p["proposed_payment"])
            summary = "纾困方案批准"
        elif action == "activate":
            if not boolean(data, "borrower_ack"):
                raise ValidationError("借款人尚未确认方案")
            changes["borrower_ack"] = True
            summary = "纾困方案生效"
        elif action == "cure":
            if not boolean(data, "arrears_cleared"):
                raise ValidationError("欠款尚未清偿")
            changes["arrears_cleared"] = True
            summary = "贷款恢复正常"
        elif action == "default":
            changes["default_reason"] = text(data, "default_reason")
            summary = "纾困方案违约"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
