"""A throwaway world for the end-to-end tests: real ``claude``, real ``security``.

Everything runs in child processes whose environment is built here from
scratch, never inherited, so nothing in it can name the owner's setup:

- ``HOME`` is a fresh directory. ``/usr/bin/security`` reads the user
  keychain search list and the default keychain from
  ``$HOME/Library/Preferences/com.apple.security.plist``, so a sandbox HOME
  has its own list holding only a throwaway keychain file created here. The
  owner's login keychain is not on that list: an item it holds (the real
  ``Claude Code-credentials`` included) is invisible to every process
  started here, and every item written lands in the throwaway file. The
  fixture checks that before any test runs and checks after it that the
  owner's own search list is unchanged.
- ``CLAUDE_CONFIG_DIR``, ``ROTA_HOME`` and the profile registry live under
  the sandbox HOME.
- The network is pointed at a closed local port, so nothing a fake token
  is shown to ever leaves the machine.

Tearing the sandbox down deletes the keychain file, which removes every
item written during the test.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

SECURITY = "/usr/bin/security"
KEYCHAIN_PASSWORD = "rota-e2e"
DEAD_PROXY = "http://127.0.0.1:9"
HERE = Path(__file__).resolve().parent
SHIM_DIR = HERE / "shim"
DRIVE = HERE / "drive.py"


def real_claude() -> str | None:
    """The real Claude Code binary: ``$ROTA_E2E_CLAUDE`` or the first
    ``claude`` on PATH that is not our shim, symlinks resolved."""
    explicit = os.environ.get("ROTA_E2E_CLAUDE")
    if explicit:
        return explicit
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(entry) / "claude"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            resolved = candidate.resolve()
            if SHIM_DIR not in resolved.parents:
                return str(resolved)
    return None


def claude_service(config_dir: str | None) -> str:
    """The Keychain service Claude Code 2.1.x keeps its OAuth login under.

    Written out here, from the binary, rather than imported from rota: the
    tests check rota against Claude Code, not against itself. In the 2.1.302
    bundle::

        a = t ? "" : `-${sha256(r).digest("hex").substring(0, 8)}`
        return `Claude Code${OAUTH_FILE_SUFFIX}${e}${a}`   // e = "-credentials"

    where ``t`` is "CLAUDE_CONFIG_DIR unset" and ``r`` the NFC-normalized
    raw value. ``test_claude_naming`` confirms it against the live binary.
    """
    if not config_dir:
        return "Claude Code-credentials"
    digest = hashlib.sha256(unicodedata.normalize("NFC", config_dir).encode()).hexdigest()
    return f"Claude Code-credentials-{digest[:8]}"


@dataclass(frozen=True)
class FakeAccount:
    """One made-up account: what a login leaves behind, with no real secret."""

    tag: str

    @property
    def email(self) -> str:
        return f"{self.tag}@rota-e2e.invalid"

    @property
    def uuid(self) -> str:
        n = sum(map(ord, self.tag))
        return f"00000000-0000-4000-8000-{n:012x}"

    @property
    def org_uuid(self) -> str:
        n = sum(map(ord, self.tag))
        return f"00000000-0000-4000-9000-{n:012x}"

    @property
    def access_token(self) -> str:
        return f"sk-ant-oat01-ROTA-E2E-FAKE-{self.tag}"

    @property
    def refresh_token(self) -> str:
        return f"sk-ant-ort01-ROTA-E2E-FAKE-{self.tag}"

    def credential(self, *, suffix: str = "", expired: bool = False) -> dict:
        # Far from expiry unless asked, so nothing tries to refresh it.
        expires = time.time() - 3600 if expired else time.time() + 7 * 24 * 3600
        return {
            "claudeAiOauth": {
                "accessToken": self.access_token + suffix,
                "refreshToken": self.refresh_token + suffix,
                "expiresAt": int(expires * 1000),
                "scopes": ["user:inference", "user:profile", "user:sessions:claude_code"],
                "subscriptionType": "max",
                "rateLimitTier": "default_claude_max_20x",
            }
        }

    def oauth_account(self) -> dict:
        return {
            "accountUuid": self.uuid,
            "emailAddress": self.email,
            "organizationUuid": self.org_uuid,
            "organizationName": f"rota-e2e {self.tag}",
            "organizationRole": "admin",
            "displayName": self.tag,
        }


ACCOUNTS = (FakeAccount("alice"), FakeAccount("bob"), FakeAccount("carol"))


class SandboxError(RuntimeError):
    pass


class FakeApi:
    """Anthropic's OAuth endpoints for fake tokens, on a local port.

    ``/api/oauth/profile`` answers whose token it is (the token names the
    account, refreshed variants included), ``/api/oauth/usage`` returns
    ``usage[tag]`` (default: plenty left), and the token endpoint rotates a
    fake account's refresh token (or refuses, with ``refresh_ok`` False).
    ``requests`` records ``(path, tag)`` for each call. Off unless a test
    asks for it: rota then sees the network as down.
    """

    def __init__(self) -> None:
        import http.server
        import threading

        self.requests: list[tuple[str, str | None]] = []
        self.usage: dict[str, tuple[float, float]] = {}
        self.refresh_ok = True
        self.refreshes = 0
        api = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _account(self):
                auth = self.headers.get("Authorization", "")
                for account in ACCOUNTS:
                    if auth.startswith(f"Bearer {account.access_token}"):
                        return account
                return None

            def _send(self, code: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                account = self._account()
                api.requests.append((self.path, account.tag if account else None))
                if account is None:
                    return self._send(401, {"error": {"type": "authentication_error"}})
                if self.path.startswith("/api/oauth/profile"):
                    return self._send(200, {
                        "account": {"uuid": account.uuid, "email": account.email,
                                    "full_name": account.tag, "display_name": account.tag},
                        "organization": {"uuid": account.org_uuid,
                                         "name": f"rota-e2e {account.tag}",
                                         "organization_type": "claude_max"},
                    })
                if self.path.startswith("/api/oauth/usage"):
                    h5, d7 = api.usage.get(account.tag, (10.0, 10.0))
                    soon = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 3600))
                    later = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 86400))
                    return self._send(200, {
                        "five_hour": {"utilization": h5, "resets_at": soon},
                        "seven_day": {"utilization": d7, "resets_at": later},
                    })
                return self._send(404, {})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    body = {}
                grant = str(body.get("refresh_token") or "")
                account = next((a for a in ACCOUNTS if grant.startswith(a.refresh_token)), None)
                api.requests.append((self.path, account.tag if account else None))
                if account is None or not api.refresh_ok:
                    return self._send(400, {"error": "invalid_grant"})
                api.refreshes += 1
                n = api.refreshes
                return self._send(200, {
                    "access_token": f"{account.access_token}-r{n}",
                    "refresh_token": f"{account.refresh_token}-r{n}",
                    "expires_in": 28800,
                    "scope": "user:inference user:profile user:sessions:claude_code",
                    "account": {"uuid": account.uuid, "email_address": account.email},
                    "organization": {"uuid": account.org_uuid},
                })

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Sandbox:
    def __init__(self, root: Path, claude_bin: str) -> None:
        self.root = root
        self.claude_bin = claude_bin
        self.home = root / "home"
        self.tmp = root / "tmp"
        self.keychain = self.home / "Library" / "Keychains" / "rota-e2e.keychain-db"
        self.user = getpass.getuser()
        self.api: FakeApi | None = None

    def start_api(self) -> FakeApi:
        """Answer rota's Anthropic calls from :class:`FakeApi` from now on."""
        if self.api is None:
            self.api = FakeApi()
        return self.api

    # -- lifecycle -------------------------------------------------------------

    def create(self) -> "Sandbox":
        (self.home / "Library" / "Preferences").mkdir(parents=True)
        self.keychain.parent.mkdir(parents=True)
        (self.home / ".claude").mkdir()
        self.tmp.mkdir()
        self.security("create-keychain", "-p", KEYCHAIN_PASSWORD, str(self.keychain))
        self.security("list-keychains", "-d", "user", "-s", str(self.keychain))
        self.security("default-keychain", "-d", "user", "-s", str(self.keychain))
        self.security("set-keychain-settings", str(self.keychain))  # never auto-lock
        self.security("unlock-keychain", "-p", KEYCHAIN_PASSWORD, str(self.keychain))
        self.assert_isolated()
        return self

    def destroy(self) -> None:
        if self.api is not None:
            self.api.close()
        if self.keychain.exists():
            self.security("delete-keychain", str(self.keychain), check=False)
        shutil.rmtree(self.root, ignore_errors=True)

    def assert_isolated(self) -> None:
        """Refuse to go on unless the sandbox sees its own keychain only."""
        listed = self.security("list-keychains", "-d", "user").stdout
        default = self.security("default-keychain", "-d", "user").stdout
        found = [line.strip().strip('"') for line in listed.splitlines() if line.strip()]
        want = os.path.realpath(self.keychain)
        if [os.path.realpath(p) for p in found] != [want] or os.path.realpath(
            default.strip().strip('"')
        ) != want:
            raise SandboxError(
                f"sandbox keychain search list is not isolated: list={found!r} default={default!r}"
            )

    # -- environment -----------------------------------------------------------

    def env(self, *, config_dir: str | None = None, rota_home: str | None = None,
            shim: bool = False, **extra: str) -> dict[str, str]:
        path = [str(SHIM_DIR)] if shim else []
        path += [str(Path(self.claude_bin).parent), str(Path(sys.executable).parent),
                 "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        env = {
            "HOME": str(self.home),
            "USER": self.user,
            "LOGNAME": self.user,
            "PATH": os.pathsep.join(path),
            "TMPDIR": str(self.tmp),
            "LANG": "en_US.UTF-8",
            "TERM": "xterm-256color",
            "NO_COLOR": "1",
            "HTTPS_PROXY": DEAD_PROXY,
            "HTTP_PROXY": DEAD_PROXY,
            "https_proxy": DEAD_PROXY,
            "http_proxy": DEAD_PROXY,
            "NO_PROXY": "",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "ROTA_E2E_REAL_CLAUDE": self.claude_bin,
            "ROTA_E2E_SHIM_LOG": str(self.root / "shim.log"),
        }
        pythonpath = []
        if os.environ.get("ROTA_E2E_SRC"):
            # Run another checkout's rota (e.g. an older commit's worktree)
            # under the same harness: its src shadows the editable install.
            pythonpath.append(os.environ["ROTA_E2E_SRC"])
        if self.api is not None:
            env["ROTA_E2E_FAKE_API"] = self.api.url
            pythonpath.append(str(HERE / "net"))
        if pythonpath:
            env["PYTHONPATH"] = os.pathsep.join(pythonpath)
        if config_dir is not None:
            env["CLAUDE_CONFIG_DIR"] = config_dir
        if rota_home is not None:
            env["ROTA_HOME"] = rota_home
        env.update(extra)
        return env

    # -- tools -----------------------------------------------------------------

    def security(self, *args: str, check: bool = True, input: str | None = None):
        result = subprocess.run(
            [SECURITY, *args], env=self.env(), capture_output=True, text=True,
            input=input, timeout=30,
        )
        if check and result.returncode != 0:
            raise SandboxError(f"security {' '.join(args)}: rc={result.returncode} {result.stderr}")
        return result

    def keychain_get(self, service: str) -> str | None:
        result = self.security("find-generic-password", "-a", self.user, "-w", "-s", service,
                               check=False)
        if result.returncode == 44:
            return None
        if result.returncode != 0:
            raise SandboxError(f"keychain read {service!r}: {result.stderr}")
        return result.stdout.removesuffix("\n")

    def keychain_set(self, service: str, value: str) -> None:
        self.security("add-generic-password", "-U", "-a", self.user, "-s", service,
                      "-X", value.encode().hex())

    def keychain_services(self) -> list[str]:
        dump = self.security("dump-keychain", str(self.keychain)).stdout
        out = []
        for line in dump.splitlines():
            line = line.strip()
            if line.startswith('"svce"<blob>="'):
                out.append(line[len('"svce"<blob>="'):-1])
        return sorted(out)

    def config_dir(self, name: str) -> str:
        path = self.home / name
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def global_config_path(self, config_dir: str | None) -> Path:
        return Path(config_dir) / ".claude.json" if config_dir else self.home / ".claude.json"

    def read_global_config(self, config_dir: str | None) -> dict:
        try:
            return json.loads(self.global_config_path(config_dir).read_text())
        except FileNotFoundError:
            return {}

    def plant_login(self, account: FakeAccount, config_dir: str | None,
                    credential: dict | None = None) -> None:
        """Leave the sandbox the way ``claude auth login`` would for this
        account: its credential in the profile's Keychain item, its identity
        in the profile's ``.claude.json``."""
        self.keychain_set(claude_service(config_dir),
                          json.dumps(credential or account.credential()))
        cfg = self.read_global_config(config_dir)
        cfg["oauthAccount"] = account.oauth_account()
        cfg.setdefault("hasCompletedOnboarding", True)
        self.global_config_path(config_dir).write_text(json.dumps(cfg, indent=2))

    def live_token(self, config_dir: str | None) -> str | None:
        raw = self.keychain_get(claude_service(config_dir))
        if raw is None:
            return None
        return json.loads(raw).get("claudeAiOauth", {}).get("accessToken")

    def live_email(self, config_dir: str | None) -> str | None:
        return (self.read_global_config(config_dir).get("oauthAccount") or {}).get("emailAddress")

    # -- processes -------------------------------------------------------------

    def run(self, argv: list[str], *, env: dict[str, str], cwd: str | None = None,
            timeout: float = 90, input: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(argv, env=env, cwd=cwd or str(self.home), capture_output=True,
                              text=True, timeout=timeout, input=input,
                              stdin=None if input is not None else subprocess.DEVNULL)

    def claude(self, *args: str, config_dir: str | None = None) -> subprocess.CompletedProcess:
        return self.run([self.claude_bin, *args], env=self.env(config_dir=config_dir))

    def claude_status(self, config_dir: str | None) -> dict:
        result = self.claude("auth", "status", "--json", config_dir=config_dir)
        try:
            return json.loads(result.stdout)
        except ValueError as e:
            raise SandboxError(f"claude auth status: rc={result.returncode} "
                               f"{result.stdout!r} {result.stderr!r}") from e

    def rota_argv(self) -> list[str]:
        return [sys.executable, "-c", "from claude_rotater.entry import main; main()"]

    def rota(self, *args: str, config_dir: str | None = None, rota_home: str | None = None,
             cwd: str | None = None, **extra: str) -> subprocess.CompletedProcess:
        return self.run([*self.rota_argv(), *args],
                        env=self.env(config_dir=config_dir, rota_home=rota_home, **extra), cwd=cwd)

    def drive(self, *args: str, config_dir: str | None = None, rota_home: str | None = None,
              cwd: str | None = None, shim: bool = False, timeout: float = 120,
              **extra: str) -> subprocess.CompletedProcess:
        """Run one of ``drive.py``'s steps (rota code paths with no CLI verb)."""
        return self.run([sys.executable, str(DRIVE), *args],
                        env=self.env(config_dir=config_dir, rota_home=rota_home, shim=shim, **extra),
                        cwd=cwd, timeout=timeout)


def explain(result: subprocess.CompletedProcess) -> str:
    return (f"argv={result.args!r} rc={result.returncode}\n--- stdout\n{result.stdout}"
            f"\n--- stderr\n{result.stderr}")
