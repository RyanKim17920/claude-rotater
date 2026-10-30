"""rota never writes the CLI's own API-key login: no managed-key Keychain
item, no ``primaryApiKey``, no Console login. That login signs a profile
out of its subscription; rota hands keys over through its apiKeyHelper
only (api_key_mode), and leaves a profile on its login when it can't."""

from __future__ import annotations

import ast
from pathlib import Path

import claude_rotater
from claude_rotater import login

SRC = Path(claude_rotater.__file__).parent
WRITERS = {"set_password", "add_generic_password", "add_password"}


def _modules():
    for path in sorted(SRC.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def test_no_keychain_write_names_the_managed_key_item():
    for path, tree in _modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            names = {_name(a) for a in ast.walk(node) if isinstance(a, (ast.Name, ast.Attribute))}
            literals = {a.value for a in ast.walk(node) if isinstance(a, ast.Constant)}
            if names & WRITERS or _name(node.func) in WRITERS:
                assert "CLAUDE_CODE_MANAGED_KEYCHAIN_SERVICE" not in names, f"{path}:{node.lineno}"
                assert "Claude Code" not in literals, f"{path}:{node.lineno}"


def test_primary_api_key_is_never_set():
    for path, tree in _modules():
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Subscript):
                    key = target.slice
                    assert not (isinstance(key, ast.Constant) and key.value == "primaryApiKey"), (
                        f"{path}:{node.lineno}"
                    )
            if isinstance(node, ast.Call) and _name(node.func) in {"setdefault", "update"}:
                for arg in [*node.args, *(k.value for k in node.keywords)]:
                    if isinstance(arg, ast.Constant):
                        assert arg.value != "primaryApiKey", f"{path}:{node.lineno}"
                    if isinstance(arg, ast.Dict):
                        keys = [k.value for k in arg.keys if isinstance(k, ast.Constant)]
                        assert "primaryApiKey" not in keys, f"{path}:{node.lineno}"


def test_only_claude_ai_logins_are_staged():
    assert login.LOGIN_METHODS == ("claudeai",)
