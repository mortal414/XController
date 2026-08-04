from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
import sys
import unittest

try:
    importlib.import_module("XController")
except ModuleNotFoundError as exc:
    if exc.name != "XController":
        raise
    repo_root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "XController",
        repo_root / "__init__.py",
        submodule_search_locations=[str(repo_root)],
    )
    if spec is None or spec.loader is None:
        raise
    module = importlib.util.module_from_spec(spec)
    sys.modules["XController"] = module
    spec.loader.exec_module(module)


class ModuleDocstringTests(unittest.TestCase):
    def test_split_adapter_modules_have_non_empty_docstrings(self) -> None:
        modules = [
            importlib.import_module("XController.adapter"),
            importlib.import_module("XController._adapter_read"),
            importlib.import_module("XController._adapter_runtime"),
            importlib.import_module("XController._adapter_write"),
        ]
        for module in modules:
            with self.subTest(module=module.__name__):
                self.assertIsInstance(module.__doc__, str)
                self.assertTrue(module.__doc__.strip())


if __name__ == "__main__":
    unittest.main()
