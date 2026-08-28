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


if __name__ == "__main__":
    unittest.main()
