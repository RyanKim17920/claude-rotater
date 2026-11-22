"""A stand-in for the CLI's ``auth login``, run in a pseudo-terminal by the
login_pty tests. It prints what the real one prints (the URL wrapped in an
OSC 8 hyperlink, then the paste prompt) and then behaves by FAKE_LOGIN_MODE:

- ``callback``: the browser finishes the sign-in by itself; succeed.
- ``code``: wait for a pasted code; ``good-code`` succeeds, anything else is
  refused and asked for again.
- ``fail``: the sign-in fails; exit 1 with the CLI's error line.
- ``raw``: hold the terminal in raw mode like the real CLI; ctrl+c exits 130.
- ``hang``: start a grandchild (its pid goes to FAKE_LOGIN_PIDFILE) and wait
  forever, ignoring ctrl+c, so only a signal ends it.
- ``stubborn``: like hang, but SIGTERM is ignored too.
- ``wrap``: print the URL hard-wrapped across lines, then succeed on a code.
- ``noisy``: colour codes and cursor moves around every line, then succeed.

It never contacts anything; the URL is fake.
"""

import os
import signal
import subprocess
import sys
import time

URL = "https://example.invalid/oauth/authorize?code=true&client_id=fake&state=abc123"
PROMPT = "Paste code here if prompted > "


def out(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def hyperlink(url: str) -> str:
    return f"\x1b]8;;{url}\x1b\\{url}\x1b]8;;\x1b\\"


def banner(mode: str) -> None:
    out("Opening browser to sign in…\n")
    if mode == "wrap":
        out("If the browser didn't open, visit:\n")
        out(URL[:30] + "\n" + URL[30:60] + "\n" + URL[60:] + "\n")
    elif mode == "noisy":
        out("\x1b[2K\x1b[1G\x1b[33mIf the browser didn't open, visit:\x1b[39m\n")
        out("\x1b[4m" + hyperlink(URL) + "\x1b[24m\n")
    else:
        out("If the browser didn't open, visit:\n" + hyperlink(URL) + "\n")
    out(PROMPT)


def succeed() -> None:
    out("\nSigning in…\n")
    time.sleep(0.05)
    out("Login successful.\n")
    sys.exit(0)


def main() -> None:
    mode = os.environ.get("FAKE_LOGIN_MODE", "callback")
    banner(mode)
    if mode == "callback":
        time.sleep(0.2)
        succeed()
    if mode == "fail":
        time.sleep(0.1)
        sys.stderr.write("\nCouldn't log in: Request failed with status code 400\n")
        sys.exit(1)
    if mode == "raw":
        import tty

        tty.setraw(0)
        while True:
            ch = os.read(0, 1)
            if ch in (b"\x03", b""):
                sys.exit(130)
    if mode in ("hang", "stubborn"):
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if mode == "stubborn":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        pidfile = os.environ.get("FAKE_LOGIN_PIDFILE")
        if pidfile:
            with open(pidfile, "w") as f:
                f.write(str(child.pid))
        while True:
            time.sleep(1)
    # code, wrap, noisy: read pasted codes
    for line in sys.stdin:
        code = line.strip()
        if code == "good-code":
            succeed()
        out("Invalid code. Paste the code from the page.\n" + PROMPT)
    sys.exit(1)


if __name__ == "__main__":
    main()
