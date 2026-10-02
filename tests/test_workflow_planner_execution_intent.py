import unittest

from workflow_planner import WorkflowPlanner


class WorkflowPlannerExecutionIntentTest(unittest.TestCase):
    def test_search_write_html_generates_executable_mixed_plan(self):
        task = '\u4f7f\u7528 tavily \u641c\u7d22\u5218\u56fd\u6881\uff0c\u7136\u540e\u5199\u4e00\u4e2a html \u4ecb\u7ecd\u9875\u5e76\u9a8c\u8bc1'
        draft = WorkflowPlanner().plan(task)
        self.assertTrue(draft.validation['ok'], draft.validation)
        self.assertEqual('mixed', draft.classification['taskType'])
        self.assertEqual('may_write', draft.classification['readWriteMode'])
        phases = draft.plan['phases']
        roles = [agent.get('role') for phase in phases for agent in phase['agents']]
        labels = [agent['label'] for phase in phases for agent in phase['agents']]
        self.assertEqual(['research', 'implementation', 'verification'], roles)
        self.assertEqual(['research-sources', 'write-html', 'verify-html'], labels)
        self.assertIn('mcp__tavily__tavily_search', phases[0]['agents'][0]['requiredTools'])
        self.assertTrue(phases[1]['agents'][0]['writeScope'])
        self.assertTrue(draft.plan['verification']['checks'])
        self.assertNotIn('python_unittest', str(draft.plan['verification']))

    def test_plan_only_stays_planning_and_single_question_is_not_execution(self):
        planner = WorkflowPlanner()
        planning = planner.plan('\u5236\u5b9a\u4e00\u4e2a\u5b9e\u65bd\u8ba1\u5212\uff0c\u53ea\u89c4\u5212\u4e0d\u8981\u6267\u884c')
        simple = planner.plan('\u89e3\u91ca\u5218\u56fd\u6881')
        self.assertEqual('planning', planning.classification['taskType'])
        self.assertEqual('planner', planning.plan['phases'][0]['agents'][0]['label'])
        self.assertEqual('planning', simple.classification['taskType'])
        self.assertFalse(simple.classification['needsMcp'])


if __name__ == '__main__':
    unittest.main()
