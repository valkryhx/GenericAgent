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
        research_agent = phases[0]['agents'][0]
        self.assertEqual('research', research_agent['toolProfile'])
        self.assertIn('web_search', research_agent['capabilities'])
        # The host owns the profile list: the plan must never guess a concrete tool name.
        self.assertNotIn('requiredTools', research_agent)
        self.assertNotIn('mcp__tavily__tavily_search', str(draft.plan))
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


    def test_explicit_workflow_opt_in_executes_instead_of_only_planning(self):
        # Real case wf_622266234f1345359d4e5f999758b922: "/workflow 写3个python
        # demo 并检验" was classified planning, so the only job was a planner and
        # the run "succeeded" having written nothing but PLAN.md. The ink UI strips
        # the "/workflow " prefix, so the planner learns about the opt-in from
        # context["activation"]; an explicit opt-in must always execute.
        task = "分别用python写3个demo：1.hello word程序 2. 1-100内的质数 3. html展示你好二字 然后检验结果"
        draft = WorkflowPlanner().plan(task, {"activation": {"action": "requested", "mode": "explicit"}})
        self.assertTrue(draft.validation["ok"], draft.validation)
        self.assertEqual("general", draft.classification["taskType"])
        labels = [agent["label"] for phase in draft.plan["phases"] for agent in phase["agents"]]
        self.assertEqual(["execute-task", "verify-result"], labels)
        self.assertEqual("authoring", draft.plan["phases"][0]["agents"][0]["toolProfile"])
        # The deliverable shape is unknown here, so nothing may guess a test
        # runner or a concrete tool name.
        self.assertNotIn("python_unittest", str(draft.plan))
        self.assertNotIn("mcp__", str(draft.plan))
        self.assertTrue(draft.script)

    def test_unrecognised_task_without_explicit_opt_in_keeps_planning_template(self):
        # The keyword classifier alone must not turn a question into execution:
        # only an explicit workflow opt-in changes the fallback shape.
        draft = WorkflowPlanner().plan("分别用python写3个demo然后检验")
        self.assertEqual("planning", draft.classification["taskType"])
        self.assertEqual(["planner"], [agent["label"] for phase in draft.plan["phases"] for agent in phase["agents"]])

if __name__ == '__main__':
    unittest.main()
