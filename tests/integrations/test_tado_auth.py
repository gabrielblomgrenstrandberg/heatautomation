import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from heatautomation.integrations import tado_auth
from heatautomation.integrations.tado import TadoService, TokenFile
from tests.integrations.test_tado import Response, Session, tokens


class TadoAuthorizationCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "token.json"
        self.store = TokenFile(self.path)
        self.session = Session()
        self.now = 0.0
        self.output = io.StringIO()
        self.errors = io.StringIO()

    def run_command(self, *args):
        def create_service(house, store):
            return TadoService(house, store, session=self.session,
                               monotonic=lambda: self.now)

        def sleep(seconds):
            self.now += seconds

        with patch("heatautomation.integrations.tado.config.TADO_ENABLED", True), \
                patch.object(tado_auth, "TadoService", side_effect=create_service), \
                patch.object(tado_auth.time, "sleep", side_effect=sleep), \
                contextlib.redirect_stdout(self.output), \
                contextlib.redirect_stderr(self.errors):
            return tado_auth.main(["--token-file", str(self.path), *args])

    @staticmethod
    def authorization():
        return Response({
            "device_code": "private-device-code", "verification_uri": "https://example.test",
            "user_code": "ABCD", "interval": 5, "expires_in": 100,
        })

    def test_polls_until_approved_and_saves_token_without_logging_secrets(self):
        self.session.responses = [
            self.authorization(), Response({"error": "authorization_pending"}, 400),
            Response({"error": "slow_down"}, 400), tokens(refresh="private-refresh-token"),
        ]
        self.assertEqual(self.run_command(), 0)
        self.assertEqual(self.store.load(), "private-refresh-token")
        self.assertEqual(self.now, 20)
        self.assertEqual(len(self.session.calls), 4)
        self.assertIn("ABCD", self.output.getvalue())
        self.assertNotIn("private-device-code", self.output.getvalue())
        self.assertNotIn("private-refresh-token", self.output.getvalue())
        self.assertEqual(self.errors.getvalue(), "")

    def test_existing_token_is_not_replaced_or_sent(self):
        self.store.save("existing")
        self.assertEqual(self.run_command(), 0)
        self.assertEqual(self.store.load(), "existing")
        self.assertEqual(self.session.calls, [])

    def test_reauthorization_replaces_token_only_after_success(self):
        self.store.save("existing")
        self.session.responses = [self.authorization(), tokens(refresh="new")]
        self.assertEqual(self.run_command("--reauthorize"), 0)
        self.assertEqual(self.store.load(), "new")

    def test_failed_reauthorization_preserves_token_and_reports_failure(self):
        self.store.save("existing")
        self.session.responses = [self.authorization(), Response({"error": "access_denied"}, 400)]
        self.assertEqual(self.run_command("--reauthorize"), 1)
        self.assertEqual(self.store.load(), "existing")
        self.assertIn("authorization failed", self.errors.getvalue())

    def test_expired_authorization_reports_failure(self):
        response = self.authorization()
        response.data["expires_in"] = 2
        self.session.responses = [response]
        self.assertEqual(self.run_command(), 1)
        self.assertIn("expired", self.errors.getvalue())
        self.assertFalse(self.path.exists())

    def test_disabled_integration_reports_failure_without_network(self):
        with patch("heatautomation.integrations.tado.config.TADO_ENABLED", False), \
                contextlib.redirect_stderr(self.errors):
            result = tado_auth.main(["--token-file", str(self.path)])
        self.assertEqual(result, 1)
        self.assertIn("disabled", self.errors.getvalue())
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
