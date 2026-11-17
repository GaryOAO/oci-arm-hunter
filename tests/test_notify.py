import unittest
from unittest.mock import Mock
from scripts.notify import notify


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.env = dict(RESULT="created", STATE="RUNNING", INSTANCE_ID="private-instance-id",
                        SHAPE="VM.Standard.A1.Flex", OCPUS="1", MEMORY_GB="6", REGION="us-sanjose-1",
                        GITHUB_REPOSITORY="GaryOAO/oci-arm-hunter", GITHUB_RUN_ID="123")

    def test_create_and_deduplicate_even_closed_issue_on_later_page(self):
        request = Mock(side_effect=[[[]], {"html_url": "https://github.com/example/issues/1"}])
        notify(self.env, request)
        payload = request.call_args.args[1]
        self.assertIn("@GaryOAO", payload["body"])
        self.assertNotIn(self.env["INSTANCE_ID"], payload["body"])
        request = Mock(return_value=[[], [{"state": "closed", "body": payload["body"], "html_url": "existing"}]])
        self.env["RESULT"] = "existing"
        self.assertEqual(notify(self.env, request), "existing")
        request.assert_called_once()

    def test_no_notification_without_confirmed_running_instance(self):
        for changes in ({"RESULT": "no_capacity"}, {"STATE": "PROVISIONING"},
                        {"STATE": "STOPPED"}, {"INSTANCE_ID": ""}):
            request = Mock()
            notify(dict(self.env, **changes), request)
            request.assert_not_called()

    def test_failed_notification_can_retry_next_run(self):
        request = Mock(side_effect=[[[]], RuntimeError("GitHub unavailable")])
        with self.assertRaises(RuntimeError):
            notify(self.env, request)
        self.env["RESULT"] = "existing"
        request = Mock(side_effect=[[[]], {"html_url": "retried"}])
        self.assertEqual(notify(self.env, request), "retried")

    def test_failed_listing_does_not_risk_duplicate_issue(self):
        request = Mock(side_effect=RuntimeError("GitHub unavailable"))
        with self.assertRaises(RuntimeError):
            notify(self.env, request)
        request.assert_called_once()
