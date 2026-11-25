"""The sdist must pack an allowlist, never whatever lies in the checkout."""

import re
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
        "/README.md",
        "/uv.lock",
    }


def test_pypi_description_has_no_relative_links():
    """PyPI serves no repo files or heading ids: every link must be absolute."""
    root = Path(__file__).resolve().parent.parent
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    hook = config["tool"]["hatch"]["metadata"]["hooks"]["fancy-pypi-readme"]
    assert [f["path"] for f in hook["fragments"]] == ["README.md"]
    text = (root / "README.md").read_text(encoding="utf-8")
    for sub in hook["substitutions"]:
        text = re.sub(sub["pattern"], sub["replacement"], text)
    targets = re.findall(r"\]\(([^)\s]+)", text) + re.findall(r'src="([^"]+)"', text)
    assert targets
    relative = [t for t in targets if not re.match(r"(https?:|mailto:)", t)]
    assert relative == []
