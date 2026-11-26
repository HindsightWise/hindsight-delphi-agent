"""AST boundary tests enforcing vertical-slice isolation (zero cross-feature sibling imports)."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

SRC_FEATURES = Path(__file__).resolve().parents[1] / "src" / "features"


class TestVerticalSliceBoundaries(unittest.TestCase):
    def test_zero_cross_feature_sibling_imports(self) -> None:
        feature_dirs = sorted(p for p in SRC_FEATURES.iterdir() if p.is_dir() and not p.name.startswith("__"))
        self.assertGreaterEqual(len(feature_dirs), 3)
        feature_names = {p.name for p in feature_dirs}

        violations: list[str] = []
        for fdir in feature_dirs:
            own_name = fdir.name
            for py_file in fdir.rglob("*.py"):
                tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module:
                        parts = node.module.split(".")
                        if len(parts) >= 3 and parts[0] == "src" and parts[1] == "features":
                            target = parts[2]
                            if target in feature_names and target != own_name:
                                violations.append(f"{py_file.name} imports sibling feature {target}")
                    elif isinstance(node, ast.Import):
                        for alias in node.names:
                            parts = alias.name.split(".")
                            if len(parts) >= 3 and parts[0] == "src" and parts[1] == "features":
                                target = parts[2]
                                if target in feature_names and target != own_name:
                                    violations.append(f"{py_file.name} imports sibling feature {target}")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
