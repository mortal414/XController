from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


repo_root = Path(__file__).resolve().parents[1]
loaded = sys.modules.get("XController")
loaded_file = Path(str(getattr(loaded, "__file__", "") or ""))
if loaded is not None and repo_root not in loaded_file.parents and loaded_file != repo_root / "__init__.py":
    for module_name in list(sys.modules):
        if module_name == "XController" or module_name.startswith("XController."):
            sys.modules.pop(module_name, None)

# The repository may be checked out into a directory whose name is not
# ``XController`` (for example a CI or audit worktree).  Load the package from
# this checkout explicitly so an unrelated editable install cannot be tested.
loaded = sys.modules.get("XController")
loaded_file = Path(str(getattr(loaded, "__file__", "") or ""))
if loaded is None or (repo_root not in loaded_file.parents and loaded_file != repo_root / "__init__.py"):
    spec = importlib.util.spec_from_file_location(
        "XController",
        repo_root / "__init__.py",
        submodule_search_locations=[str(repo_root)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load XController from the current checkout")
    package = importlib.util.module_from_spec(spec)
    sys.modules["XController"] = package
    spec.loader.exec_module(package)

sys.modules["__init__"] = sys.modules["XController"]
assert Path(sys.modules["XController"].__file__).resolve() == (repo_root / "__init__.py").resolve()
