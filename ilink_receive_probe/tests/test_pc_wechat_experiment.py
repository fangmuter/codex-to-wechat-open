import unittest

from ilink_receive_probe.probe.pc_wechat_experiment import (
    PcWeChatDependencyExperiment,
    PcWeChatObservation,
    PcWeChatState,
)


def observation(state, send_state):
    return PcWeChatObservation(
        state=state, receive_state="RECEIVED", send_state=send_state,
        http_status=200, response_keys=("ret",), account_match=True,
    )


class PcWeChatExperimentTests(unittest.TestCase):
    def test_matrix_requires_all_manual_states(self):
        matrix = PcWeChatDependencyExperiment()
        matrix.record(observation(PcWeChatState.LOGGED_IN.value, "ACKED_API"))
        self.assertEqual(matrix.verdict(), "PC_WECHAT_LOGIN_DEPENDENCY_INCONCLUSIVE")
        with self.assertRaises(ValueError):
            matrix.record(observation(PcWeChatState.LOGGED_IN.value, "ACKED_API"))

    def test_confirmed_dependency_requires_logged_out_failure_and_relogin_recovery(self):
        matrix = PcWeChatDependencyExperiment()
        matrix.record(observation(PcWeChatState.LOGGED_IN.value, "ACKED_API"))
        matrix.record(observation(PcWeChatState.LOGGED_OUT.value, "SESSION_PREPARE_FAILED"))
        matrix.record(observation(PcWeChatState.RELOGGED_IN.value, "ACKED_API"))
        self.assertEqual(matrix.verdict(), "PC_WECHAT_LOGIN_DEPENDENCY_CONFIRMED")

    def test_equal_results_are_not_observed_dependency(self):
        matrix = PcWeChatDependencyExperiment()
        for state in PcWeChatState:
            matrix.record(observation(state.value, "ACKED_API"))
        self.assertEqual(matrix.verdict(), "PC_WECHAT_LOGIN_DEPENDENCY_NOT_OBSERVED")

    def test_logged_in_and_exited_success_are_sufficient_for_no_dependency(self):
        matrix = PcWeChatDependencyExperiment()
        matrix.record(observation(PcWeChatState.LOGGED_IN.value, "ACKED_API"))
        matrix.record(observation(PcWeChatState.LOGGED_OUT.value, "ACKED_API"))
        self.assertEqual(matrix.verdict(), "PC_WECHAT_LOGIN_DEPENDENCY_NOT_OBSERVED")


if __name__ == "__main__":
    unittest.main()
