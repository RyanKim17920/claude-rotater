"""Loaded at start-up by the rota processes the E2E sandbox runs (via
``PYTHONPATH``), and only there.

With ``$ROTA_E2E_FAKE_API`` set, a request rota makes to Anthropic
(``api.anthropic.com``, ``platform.claude.com``) goes to the sandbox's fake
API server at that address instead, path, headers and body unchanged, so
rota's own request and parsing code runs as it does for real. Anything else
is left alone (the sandbox proxy setting keeps it off the network).
"""

import os

_FAKE = os.environ.get("ROTA_E2E_FAKE_API")

if _FAKE:
    import urllib.parse
    import urllib.request

    _HOSTS = {"api.anthropic.com", "platform.claude.com"}
    _real_urlopen = urllib.request.urlopen

    def _redirect(url):
        parts = urllib.parse.urlsplit(url)
        if parts.hostname not in _HOSTS:
            return None
        fake = urllib.parse.urlsplit(_FAKE)
        return urllib.parse.urlunsplit((fake.scheme, fake.netloc, parts.path, parts.query, ""))

    def urlopen(url, *args, **kwargs):
        if isinstance(url, urllib.request.Request):
            target = _redirect(url.full_url)
            if target is not None:
                url.full_url = target
                # Straight to the fake server: no proxy for it.
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                return opener.open(url, *args, **kwargs)
        elif isinstance(url, str):
            target = _redirect(url)
            if target is not None:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                return opener.open(target, *args, **kwargs)
        return _real_urlopen(url, *args, **kwargs)

    urllib.request.urlopen = urlopen


def _chain_next_sitecustomize():
    """This file shadows any other ``sitecustomize`` (Homebrew's Python
    ships one); run that one too."""
    import importlib.util
    import sys

    here = os.path.dirname(os.path.abspath(__file__))
    for entry in sys.path:
        if not entry or os.path.abspath(entry) == here:
            continue
        candidate = os.path.join(entry, "sitecustomize.py")
        if os.path.isfile(candidate):
            spec = importlib.util.spec_from_file_location("_next_sitecustomize", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return


_chain_next_sitecustomize()
