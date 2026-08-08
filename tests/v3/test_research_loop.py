import unittest
from app.autoresearch.research_loop import (
    fetch_pending_questions,
    mark_question_answered,
    run_research_loop_pass,
)


class TestResearchLoop(unittest.TestCase):
    def test_research_loop_interface(self):
        """Verify research loop helper functions do not crash and handle empty queues gracefully."""
        pending = fetch_pending_questions(limit=5)
        self.assertIsInstance(pending, list)

        res = run_research_loop_pass(limit=2)
        self.assertIn("pending_found", res)
        self.assertIn("processed", res)
        self.assertIn("answered", res)


if __name__ == "__main__":
    unittest.main()
