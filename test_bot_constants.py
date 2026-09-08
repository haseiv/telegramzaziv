import ast
import unittest
from pathlib import Path


class BotModuleConstantsTests(unittest.TestCase):
    def test_required_module_constants(self):
        src = (Path(__file__).resolve().parent / "bot.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        names = set()
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
        self.assertIn("DEFAULT_CALL_TEXT", names)
        self.assertIn("SCHEDULE_CHANGE_HEADER", names)

    def test_call_mentions_are_named_and_batched_by_five(self):
        src = (Path(__file__).resolve().parent / "bot.py").read_text(encoding="utf-8")
        self.assertIn("MENTIONS_PER_MESSAGE = 5", src)
        self.assertIn("{mark} {label}", src)
        self.assertIn("tg://user?id=", src)
        self.assertIn("allowed_updates", src)


if __name__ == "__main__":
    unittest.main()
