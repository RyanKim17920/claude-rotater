"""The sdist must pack an allowlist, never whatever lies in the checkout."""

import tomllib
from pathlib import Path


def test_sdist_packs_only_allowlisted_paths():
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    sdist = config["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert set(sdist["include"]) == {
        "/src",
        "/tests",
        "/docs",
        "/scripts",
        "/CHANGELOG.md",
        "/uv.lock",
    }
