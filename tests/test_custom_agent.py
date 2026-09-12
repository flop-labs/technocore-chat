# -*- coding: utf-8 -*-
import unittest
from scripts.custom_agent import TechnocoreAutonomousAgent


class TestTechnocoreAutonomousAgent(unittest.TestCase):
    def test_agent_initialization(self):
        agent = TechnocoreAutonomousAgent(nick="test-bot")
        self.assertEqual(agent.nick, "test-bot")
        self.assertTrue(agent.did.startswith("did:key:"))
        self.assertEqual(agent.nonce, 1)

    def test_payload_signing(self):
        agent = TechnocoreAutonomousAgent()
        sig = agent.sign_payload("Hello Technocore")
        self.assertIsInstance(sig, str)
        self.assertGreater(len(sig), 10)

    def test_run_step_structure(self):
        agent = TechnocoreAutonomousAgent()
        # Mocking api_get agar tidak melakukan panggilan jaringan aktual saat pengujian unit
        agent.api_get = lambda path: '{"messages": [], "next": 0}'
        
        res = agent.run_step()
        self.assertIn("status", res)
        self.assertEqual(res["status"], "active")
        self.assertIn("did", res)


if __name__ == "__main__":
    unittest.main()
