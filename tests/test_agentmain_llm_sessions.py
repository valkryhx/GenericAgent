import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from agentmain import GenericAgent  # noqa: E402


class AgentMainLLMSessionsTest(unittest.TestCase):
    def test_failed_mixin_config_is_not_left_as_default_client(self):
        backend = type("Backend", (), {"history": [], "name": "native", "model": "gpt-test"})()
        client = type("Client", (), {"backend": backend, "last_tools": ""})()
        mykeys = {
            "mixin_config": {"llm_nos": ["missing"]},
            "native_oai_config": {"name": "native"},
        }
        agent = GenericAgent.__new__(GenericAgent)
        agent.llm_no = 0
        globals_ref = GenericAgent.load_llm_sessions.__globals__

        with patch.dict(globals_ref, {
            "reload_mykeys": lambda: (mykeys, True),
            "resolve_client": lambda _cfg_name: client,
            "MixinSession": type("FailingMixinSession", (), {"__init__": lambda self, *_args, **_kwargs: (_ for _ in ()).throw(Exception("missing mixin"))}),
        }):
            agent.load_llm_sessions()

        self.assertEqual(agent.llmclients, [client])
        self.assertIs(agent.llmclient, client)
        self.assertFalse(isinstance(agent.llmclient, dict))


if __name__ == "__main__":
    unittest.main()
