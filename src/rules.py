"""住房贷款纾困申请与履约跟踪领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Optional, Tuple

from .domain import (
    Actor,
    Conflict,
    PermissionDenied,
    ValidationError,
    boolean,
    choice,
    future_day,
    integer,
    is_expired,
    number,
    text,
    text_list,
)


INITIAL_STATE = "submitted"
CREATE_ROLES = {'intake_officer'}
EXCEPTION_CREATE_ROLES = {'intake_officer'}
EXCEPTION_REVIEW_ROLES = {'exception_reviewer'}
ACTION_ROLES = {
    'assess': {'intake_officer'},
    'approve': {'underwriter'},
    'activate': {'servicer'},
    'cure': {'servicer'},
    'default': {'servicer'},
}
TRANSITIONS = {'assess': {'submitted': 'assessed'}, 'approve': {'assessed': 'approved'}, 'activate': {'approved': 'active'}, 'cure': {'active': 'cured'}, 'default': {'active': 'defaulted'}}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES) | set(EXCEPTION_CREATE_ROLES) | set(EXCEPTION_REVIEW_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_create_exception(self, role: str) -> bool:
        return role == "admin" or role in EXCEPTION_CREATE_ROLES

    def role_can_review_exception(self, role: str) -> bool:
        return role == "admin" or role in EXCEPTION_REVIEW_ROLES

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

    @staticmethod
    def expires_at_for_day(expires_on: str) -> str:
        return "%sT23:59:59+00:00" % expires_on

    def validate_exception_request(self, record: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        if record["state"] != "assessed":
            raise Conflict("仅已完成偿付能力评估的方案可发起例外审定")
        if record["payload"].get("eligibility") is not False:
            raise Conflict("仅不满足偿付能力的方案需要例外审定")
        reason = text(data or {}, "reason")
        expires_on = future_day(data or {}, "expires_on")
        return {"reason": reason, "expires_on": expires_on, "expires_at": self.expires_at_for_day(expires_on)}

    def validate_exception_review(self, exception: Dict[str, Any], actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        if exception["state"] != "pending":
            raise Conflict("仅待审例外可以复核")
        if actor.user_id == exception["requested_by"]:
            raise PermissionDenied("发起人不能复核自己的例外申请")
        decision = choice(data or {}, "decision", ["approved", "rejected"])
        review_note = text(data or {}, "review_note")
        if is_expired(exception["expires_at"]):
            raise Conflict("例外有效期已过，不能复核通过")
        return {"decision": decision, "review_note": review_note}

    @staticmethod
    def exception_is_usable(exception: Optional[Dict[str, Any]]) -> bool:
        return bool(exception and exception["state"] == "approved" and not is_expired(exception["expires_at"]))

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any], exception: Dict[str, Any] = None) -> Tuple[str, Dict[str, Any], str]:
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
            exception_approved = False
            exception_id = None
            if not p.get("eligibility"):
                if not self.exception_is_usable(exception):
                    raise ValidationError("不符合偿付能力，必须先取得有效且复核通过的例外审定")
                exception_approved = True
                exception_id = exception["id"]
            changes["approved_program"] = p["program_type"]
            changes["approved_months"] = int(p["eligible_months"])
            changes["approved_payment"] = float(p["proposed_payment"])
            changes["exception_approved"] = exception_approved
            changes["exception_id"] = exception_id
            summary = "纾困方案批准（例外审定）" if exception_approved else "纾困方案批准"
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
