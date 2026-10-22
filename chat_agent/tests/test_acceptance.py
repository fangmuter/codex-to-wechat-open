import unittest

from chat_agent.acceptance import run_offline_acceptance


class OfflineAcceptanceTests(unittest.TestCase):
    def test_default_pre_live_matrix_passes(self):
        report = run_offline_acceptance()
        self.assertTrue(report.passed, report.checks)
        self.assertTrue(all(report.checks.values()))
        self.assertEqual(report.metrics["batch"]["model_calls"], 100)
        self.assertEqual(report.metrics["restarts"]["model_calls"], 1)
        self.assertEqual(report.metrics["network"]["missed_count"], 2)


if __name__ == "__main__":
    unittest.main()
