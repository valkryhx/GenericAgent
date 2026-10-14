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


    def test_session_mode_ignores_conversational_turns(self):
        state = WorkflowActivationState()
        state.handle('/workflow on')
        for text in ('wondeful!', 'wonderful', '\u597d\u7684', 'thanks'):
            with self.subTest(text=text):
                decision = state.handle(text)
                self.assertEqual(('none', 'none'), (decision.mode, decision.action))

    def test_session_mode_still_orchestrates_substantive_tasks(self):
        state = WorkflowActivationState()
        state.handle('/workflow on')
        for text in ('generate report', '\u91cd\u6784\u8fd9\u4e2a\u6a21\u5757'):
            with self.subTest(text=text):
                self.assertEqual('requested', state.handle(text).action)

    def test_explicit_slash_workflow_unaffected_by_conversational_gate(self):
        decision = resolve_workflow_activation('/workflow generate report')
        self.assertEqual(('explicit', 'requested'), (decision.mode, decision.action))
if __name__ == '__main__':
    unittest.main()
