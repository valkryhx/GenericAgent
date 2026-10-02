import unittest

from workflow_activation import WorkflowActivationState, resolve_workflow_activation


class WorkflowActivationTest(unittest.TestCase):
    def test_explicit_workflow_is_requested(self):
        decision = resolve_workflow_activation('/workflow search and write html')
        self.assertEqual(('explicit', 'requested'), (decision.mode, decision.action))

    def test_search_write_verify_is_recommended(self):
        task = '\u5148\u641c\u7d22\u591a\u4e2a\u6765\u6e90\uff0c\u518d\u751f\u6210 html \u6587\u4ef6\u5e76\u9a8c\u8bc1'
        decision = resolve_workflow_activation(task)
        self.assertEqual(('semantic', 'recommended'), (decision.mode, decision.action))
        self.assertEqual({'search', 'artifact', 'verification'}, set(decision.matched_signals))

    def test_simple_and_plan_only_requests_are_not_activated(self):
        self.assertEqual('none', resolve_workflow_activation('explain MCP').action)
        task = '\u5236\u5b9a\u5b9e\u65bd\u8ba1\u5212\uff0c\u53ea\u89c4\u5212\u4e0d\u8981\u6267\u884c'
        decision = resolve_workflow_activation(task)
        self.assertEqual('none', decision.action)
        self.assertTrue(decision.plan_only)

    def test_session_mode_is_explicit_and_resets_per_turn(self):
        state = WorkflowActivationState()
        self.assertEqual('session', state.handle('/workflow on').mode)
        self.assertEqual('requested', state.handle('generate report').action)
        self.assertEqual('none', state.handle('/workflow off').action)
        self.assertEqual('none', state.handle('generate report').action)


if __name__ == '__main__':
    unittest.main()
