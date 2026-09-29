import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


INELIGIBLE_DATA = {
    'monthly_income': 18000.0,
    'monthly_expenses': 9000.0,
    'monthly_payment': 17000.0,
    'arrears': 120000.0,
    'hardship_factor': 0.8,
    'program_type': 'reduction',
    'requested_months': 9,
}


class ExceptionReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.requester = Actor("officer-1", "intake_officer")
        self.reviewer = Actor("reviewer-1", "exception_reviewer")
        self.approver = Actor("underwriter-1", "underwriter")
        self.record = self.service.create(self.requester, "MORT-EX-1", INELIGIBLE_DATA)
        self.record = self.service.act(
            self.requester,
            self.record["id"],
            self.record["version"],
            "assess",
            {"assessment_note": "偿债比例过高"},
        )
        self.assertFalse(self.record["payload"]["eligibility"])
        self.expires_on = (date.today() + timedelta(days=14)).isoformat()

    def tearDown(self):
        self.temp.cleanup()

    def request_exception(self):
        return self.service.create_exception(
            self.requester,
            self.record["id"],
            {"reason": "借款人已补充稳定租金收入证明", "expires_on": self.expires_on},
        )

    def test_independent_exception_review_is_required_before_approval(self):
        with self.assertRaises(ValidationError):
            self.service.act(
                self.approver,
                self.record["id"],
                self.record["version"],
                "approve",
                {"exception_approved": True},
            )

        exception = self.request_exception()
        self.assertEqual(exception["state"], "pending")
        self.assertEqual(exception["requested_by"], "officer-1")
        self.assertIsNone(exception["reviewed_by"])

        with self.assertRaises(Conflict):
            self.request_exception()
        with self.assertRaises(PermissionDenied):
            self.service.act(
                self.requester, self.record["id"], self.record["version"], "approve", {}
            )
        with self.assertRaises(PermissionDenied):
            self.service.review_exception(
                self.requester, exception["id"], exception["version"],
                {"decision": "approved", "review_note": "自己复核"},
            )

        exception = self.service.review_exception(
            self.reviewer, exception["id"], exception["version"],
            {"decision": "approved", "review_note": "补充材料可覆盖暂时性偿付缺口"},
        )
        self.assertEqual(exception["state"], "approved")
        self.assertEqual(exception["reviewed_by"], "reviewer-1")
        with self.assertRaises(PermissionDenied):
            self.service.act(
                Actor("officer-1", "admin"), self.record["id"], self.record["version"], "approve", {}
            )
        with self.assertRaises(PermissionDenied):
            self.service.act(
                Actor("reviewer-1", "admin"), self.record["id"], self.record["version"], "approve", {}
            )

        detail = self.service.get_record(self.requester, self.record["id"])
        self.assertEqual(detail["active_exception"]["id"], exception["id"])
        self.assertEqual(detail["active_exception"]["expires_on"], self.expires_on)

        approved = self.service.act(
            self.approver, self.record["id"], self.record["version"], "approve", {}
        )
        self.assertEqual(approved["state"], "approved")
        self.assertTrue(approved["payload"]["exception_approved"])
        self.assertEqual(approved["payload"]["exception_id"], exception["id"])

        timeline = self.service.timeline(self.requester, self.record["id"])
        actions = [event["action"] for event in timeline]
        self.assertIn("exception_requested", actions)
        self.assertIn("exception_reviewed", actions)
        review = next(event for event in timeline if event["action"] == "exception_reviewed")
        self.assertEqual(review["actor_id"], "reviewer-1")
        self.assertEqual(review["details"]["state"], "approved")
        self.assertEqual(review["details"]["expires_on"], self.expires_on)
        approve_event = next(event for event in timeline if event["action"] == "approve")
        self.assertEqual(approve_event["actor_id"], "underwriter-1")
        self.assertEqual(approve_event["details"]["exception_id"], exception["id"])

    def test_rejected_exception_allows_a_new_request_and_blocks_approval(self):
        exception = self.request_exception()
        rejected = self.service.review_exception(
            self.reviewer, exception["id"], exception["version"],
            {"decision": "rejected", "review_note": "证明材料不足"},
        )
        self.assertEqual(rejected["state"], "rejected")

        replacement = self.request_exception()
        self.assertEqual(replacement["state"], "pending")
        with self.assertRaises(ValidationError):
            self.service.act(
                self.approver, self.record["id"], self.record["version"], "approve", {}
            )

    def test_expired_exception_is_recorded_and_cannot_be_used(self):
        exception = self.request_exception()
        exception = self.service.review_exception(
            self.reviewer, exception["id"], exception["version"],
            {"decision": "approved", "review_note": "同意短期例外"},
        )
        with self.service.repository._connect() as connection:
            connection.execute(
                "UPDATE exceptions SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (exception["id"],),
            )
            connection.commit()

        expired = self.service.get_exception(self.requester, exception["id"])
        self.assertEqual(expired["state"], "expired")
        self.assertEqual(expired["invalidate_reason"], "有效期届满")
        with self.assertRaises(ValidationError):
            self.service.act(
                self.approver, self.record["id"], self.record["version"], "approve", {}
            )
        timeline = self.service.timeline(self.requester, self.record["id"])
        event = next(event for event in timeline if event["action"] == "exception_expired")
        self.assertEqual(event["actor_id"], "system")
        self.assertEqual(event["details"]["exception_id"], exception["id"])

    def test_default_invalidates_approved_exception(self):
        exception = self.request_exception()
        self.service.review_exception(
            self.reviewer, exception["id"], exception["version"],
            {"decision": "approved", "review_note": "同意短期例外"},
        )
        record = self.service.act(
            self.approver, self.record["id"], self.record["version"], "approve", {}
        )
        record = self.service.act(
            Actor("servicer-1", "servicer"), record["id"], record["version"],
            "activate", {"borrower_ack": True},
        )
        record = self.service.act(
            Actor("servicer-1", "servicer"), record["id"], record["version"],
            "default", {"default_reason": "连续两期未还款"},
        )
        self.assertEqual(record["state"], "defaulted")
        updated = self.service.get_exception(self.requester, exception["id"])
        self.assertEqual(updated["state"], "invalidated")
        self.assertEqual(updated["invalidate_reason"], "方案违约")
        timeline = self.service.timeline(self.requester, self.record["id"])
        event = next(event for event in timeline if event["action"] == "exception_invalidated")
        self.assertEqual(event["actor_id"], "servicer-1")
        self.assertEqual(event["details"]["by_action"], "default")
