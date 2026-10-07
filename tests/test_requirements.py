"""The Docker images install requirements.txt only, so it must list every runtime import."""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _third_party_imports() -> set[str]:
    names: set[str] = set()
    for path in [*ROOT.joinpath("app").glob("*.py"), ROOT / "ui" / "app.py"]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return {name for name in names if name not in sys.stdlib_module_names and name != "app"}


def _runtime_requirements() -> set[str]:
    packages = set()
    for line in ROOT.joinpath("requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            packages.add(line.split("[")[0].split(">")[0].split("<")[0].split("=")[0].strip().lower())
    return packages


def test_requirements_txt_lists_every_runtime_import():
    missing = _third_party_imports() - _runtime_requirements()
    assert not missing, f"requirements.txt is missing runtime packages: {sorted(missing)}"
