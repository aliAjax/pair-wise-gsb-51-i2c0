import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


# 住房占比 7000/8000=0.875 > 0.8，欠款 60000 > 月付*6=42000 -> 评估不通过
INELIGIBLE_DATA = {'monthly_income': 8000.0, 'monthly_expenses': 5000.0, 'monthly_payment': 7000.0,
                   'arrears': 60000.0, 'hardship_factor': 0.5, 'program_type': 'reduction',
                   'requested_months': 9, 'borrower_id': 'B-DEFAULT'}
ELIGIBLE_DATA = {'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0,
                 'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction',
                 'requested_months': 9, 'borrower_id': 'B-ELIGIBLE'}

OFFICER = Actor("officer-1", "intake_officer")
REVIEWER = Actor("reviewer-1", "exception_reviewer")
REVIEWER2 = Actor("reviewer-2", "exception_reviewer")
UNDERWRITER = Actor("underwriter-1", "underwriter")
SERVICER = Actor("servicer-1", "servicer")

TOMORROW = (date.today() + timedelta(days=1)).isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


class ExceptionReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _assessed_ineligible(self, reference="MORT-30001"):
        data = dict(INELIGIBLE_DATA, borrower_id="B-" + reference)
        record = self.service.create(OFFICER, reference, data)
        record = self.service.act(OFFICER, record["id"], record["version"], "assess", {"assessment_note": "收入不稳定"})
        self.assertFalse(record["payload"]["eligibility"])
        return record

    def _approved_exception(self, record):
        exception = self.service.request_exception(OFFICER, record["id"],
                                                   {"reason": "借款人刚获得新工作，收入将恢复", "expires_at": TOMORROW})
        exception = self.service.review_exception(REVIEWER, exception["id"], exception["version"],
                                                  {"decision": "approve", "review_note": "情况属实"})
        return exception

    def test_exception_full_flow_unlocks_approval(self):
        record = self._assessed_ineligible()
        # 没有有效例外：审批人无法放行
        with self.assertRaises(ValidationError):
            self.service.act(UNDERWRITER, record["id"], record["version"], "approve", {})

        exception = self._approved_exception(record)
        self.assertEqual(exception["state"], "approved")
        self.assertEqual(exception["requested_by"], "officer-1")
        self.assertEqual(exception["reviewed_by"], "reviewer-1")

        # 凭例外批准方案
        record = self.service.act(UNDERWRITER, record["id"], record["version"], "approve",
                                  {"exception_id": exception["id"]})
        self.assertEqual(record["state"], "approved")

        detail = self.service.get_exception(REVIEWER, exception["id"])
        self.assertIsNotNone(detail["consumed_record_version"])
        actions = [event["action"] for event in detail["history"]]
        self.assertEqual(actions, ["exception_requested", "exception_approved", "exception_consumed"])

    def test_originator_cannot_review_own_exception(self):
        record = self._assessed_ineligible()
        exception = self.service.request_exception(OFFICER, record["id"],
                                                   {"reason": "经办人自己想放行的理由", "expires_at": TOMORROW})
        # 经办人角色无权复核（权限优先）
        with self.assertRaises(PermissionDenied):
            self.service.review_exception(OFFICER, exception["id"], exception["version"], {"decision": "approve"})
        # 即使用管理员角色，同一用户也不能审定自己发起的申请
        admin_actor = Actor("officer-1", "admin")
        with self.assertRaises(Conflict):
            self.service.review_exception(admin_actor, exception["id"], exception["version"],
                                          {"decision": "approve"})
        # 另一个人（管理员角色）可以审定
        reviewed = self.service.review_exception(Actor("boss-1", "admin"), exception["id"], exception["version"],
                                                 {"decision": "approve", "review_note": "管理员复核"})
        self.assertEqual(reviewed["state"], "approved")

    def test_only_one_open_exception_per_loan(self):
        record = self._assessed_ineligible()
        exception = self.service.request_exception(OFFICER, record["id"],
                                                   {"reason": "第一张待审例外的充分理由", "expires_at": TOMORROW})
        with self.assertRaises(Conflict):
            self.service.request_exception(OFFICER, record["id"],
                                           {"reason": "同一贷款再次发起例外", "expires_at": TOMORROW})
        # 驳回后可以重新发起
        self.service.review_exception(REVIEWER, exception["id"], exception["version"],
                                      {"decision": "reject", "review_note": "材料不足"})
        new_exc = self.service.request_exception(OFFICER, record["id"],
                                                 {"reason": "补充材料后重新发起例外", "expires_at": TOMORROW})
        self.assertEqual(new_exc["state"], "pending")

    def test_expired_exception_blocks_approval(self):
        record = self._assessed_ineligible()
        exception = self.service.request_exception(OFFICER, record["id"],
                                                   {"reason": "例外到期日不能早于今天", "expires_at": TOMORROW})
        # 直接构造一张到期日已过的待审例外（模拟等待审定时到期）
        with self.service.repository._connect() as connection:
            connection.execute("UPDATE exceptions SET expires_at=? WHERE id=?", (YESTERDAY, exception["id"]))
        with self.assertRaises(Conflict):
            self.service.review_exception(REVIEWER, exception["id"], exception["version"],
                                          {"decision": "approve", "review_note": "尝试放行"})
        # 惰性扫描后已自动失效
        detail = self.service.get_exception(REVIEWER, exception["id"])
        self.assertEqual(detail["state"], "voided")
        self.assertEqual(detail["void_reason"], "expired")
        with self.assertRaises(Conflict):
            self.service.act(UNDERWRITER, record["id"], record["version"], "approve",
                             {"exception_id": exception["id"]})

    def test_exception_expires_after_approval_before_use(self):
        record = self._assessed_ineligible()
        exception = self._approved_exception(record)
        # 模拟到期日已过（直接构造过期的已通过例外）
        with self.service.repository._connect() as connection:
            connection.execute("UPDATE exceptions SET expires_at=? WHERE id=?", (YESTERDAY, exception["id"]))
        with self.assertRaises(Conflict):
            self.service.act(UNDERWRITER, record["id"], record["version"], "approve",
                             {"exception_id": exception["id"]})
        detail = self.service.get_exception(REVIEWER, exception["id"])
        self.assertEqual(detail["state"], "voided")
        self.assertEqual(detail["void_reason"], "expired")

    def test_default_voids_exceptions(self):
        # 方案违约：用于批准本方案的例外失效，未使用的已通过例外也一并失效
        record = self._assessed_ineligible()
        exception = self._approved_exception(record)
        record = self.service.act(UNDERWRITER, record["id"], record["version"], "approve",
                                  {"exception_id": exception["id"]})
        record = self.service.act(SERVICER, record["id"], record["version"], "activate", {"borrower_ack": True})

        # 直接构造一张已通过、尚未使用的例外（正常流程下的开口已被唯一约束限制，这里模拟遗留开口）
        with self.service.repository._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO exceptions(record_id,state,version,reason,expires_at,requested_by,reviewed_by,"
                "review_decision,created_at,updated_at) VALUES(?,'approved',1,?,?,?,'reviewer-9','approve',?,?)",
                (record["id"], TOMORROW, "officer-9", "遗留的未使用例外理由", TOMORROW, TOMORROW),
            )
            unused_id = int(cursor.lastrowid)

        self.service.act(SERVICER, record["id"], record["version"], "default", {"default_reason": "再次断供"})

        for exc_id in (exception["id"], unused_id):
            detail = self.service.get_exception(REVIEWER, exc_id)
            self.assertEqual(detail["state"], "voided")
            self.assertEqual(detail["void_reason"], "defaulted")

        timeline = self.service.timeline(OFFICER, record["id"])
        void_events = [e for e in timeline if e["action"] == "exception_voided"]
        self.assertEqual(len(void_events), 2)
        self.assertTrue(all("方案违约" in e["details"]["summary"] for e in void_events))

    def test_rejected_exception_cannot_unlock_approval(self):
        record = self._assessed_ineligible()
        exception = self.service.request_exception(OFFICER, record["id"],
                                                   {"reason": "理由不充分的例外", "expires_at": TOMORROW})
        self.service.review_exception(REVIEWER, exception["id"], exception["version"],
                                      {"decision": "reject", "review_note": "不符合例外政策"})
        with self.assertRaises(Conflict):
            self.service.act(UNDERWRITER, record["id"], record["version"], "approve",
                             {"exception_id": exception["id"]})

    def test_exception_from_other_loan_rejected(self):
        record_a = self._assessed_ineligible("MORT-30011")
        record_b = self._assessed_ineligible("MORT-30012")
        exception = self._approved_exception(record_a)
        with self.assertRaises(Conflict):
            self.service.act(UNDERWRITER, record_b["id"], record_b["version"], "approve",
                             {"exception_id": exception["id"]})

    def test_exception_requires_assessment(self):
        record = self.service.create(OFFICER, "MORT-30020", INELIGIBLE_DATA)
        with self.assertRaises(Conflict):
            self.service.request_exception(OFFICER, record["id"],
                                           {"reason": "尚未评估就申请例外", "expires_at": TOMORROW})

    def test_eligible_loan_needs_no_exception(self):
        record = self.service.create(OFFICER, "MORT-30021", ELIGIBLE_DATA)
        record = self.service.act(OFFICER, record["id"], record["version"], "assess", {"assessment_note": "达标"})
        with self.assertRaises(Conflict):
            self.service.request_exception(OFFICER, record["id"],
                                           {"reason": "达标贷款无需例外", "expires_at": TOMORROW})
        # 达标贷款照常批准，不需要例外
        record = self.service.act(UNDERWRITER, record["id"], record["version"], "approve", {})
        self.assertEqual(record["state"], "approved")

    def test_exception_payload_validation(self):
        record = self._assessed_ineligible()
        with self.assertRaises(ValidationError):
            self.service.request_exception(OFFICER, record["id"], {"reason": "太短", "expires_at": TOMORROW})
        with self.assertRaises(ValidationError):
            self.service.request_exception(OFFICER, record["id"],
                                           {"reason": "到期日无效的例外理由", "expires_at": "not-a-date"})
        with self.assertRaises(ValidationError):
            self.service.request_exception(OFFICER, record["id"],
                                           {"reason": "到期日必须晚于今天", "expires_at": YESTERDAY})

    def test_exception_permissions(self):
        record = self._assessed_ineligible()
        # underwriter 不能发起例外
        with self.assertRaises(PermissionDenied):
            self.service.request_exception(UNDERWRITER, record["id"],
                                           {"reason": "审批人不该自己发起例外", "expires_at": TOMORROW})
        exception = self.service.request_exception(OFFICER, record["id"],
                                                   {"reason": "权限测试用例外", "expires_at": TOMORROW})
        # 经办人不能复核例外
        with self.assertRaises(PermissionDenied):
            self.service.review_exception(Actor("officer-2", "intake_officer"), exception["id"], exception["version"],
                                          {"decision": "approve"})
        # 复核人不能代替审批人批准方案
        with self.assertRaises(PermissionDenied):
            self.service.act(REVIEWER, record["id"], record["version"], "approve",
                             {"exception_id": exception["id"]})

    def test_stale_exception_version_rejected(self):
        record = self._assessed_ineligible()
        exception = self.service.request_exception(OFFICER, record["id"],
                                                   {"reason": "并发审定测试例外", "expires_at": TOMORROW})
        self.service.review_exception(REVIEWER, exception["id"], exception["version"],
                                      {"decision": "reject", "review_note": "先驳回"})
        with self.assertRaises(Conflict):
            self.service.review_exception(REVIEWER2, exception["id"], 1, {"decision": "approve"})

    def test_timeline_shows_exception_detail(self):
        record = self._assessed_ineligible()
        exception = self._approved_exception(record)
        self.service.act(UNDERWRITER, record["id"], record["version"], "approve",
                         {"exception_id": exception["id"]})
        timeline = self.service.timeline(REVIEWER, record["id"])
        actions = [event["action"] for event in timeline]
        self.assertIn("exception_requested", actions)
        self.assertIn("exception_approved", actions)
        self.assertIn("exception_consumed", actions)
        requested = next(e for e in timeline if e["action"] == "exception_requested")
        self.assertEqual(requested["details"]["requested_by"], "officer-1")
        self.assertEqual(requested["details"]["expires_at"], TOMORROW)
        approved = next(e for e in timeline if e["action"] == "exception_approved")
        self.assertEqual(approved["details"]["reviewed_by"], "reviewer-1")
