"""Who was on duty when: the timeline the costs view credits spend by.

Transcripts carry no account on their spend, so a slot is credited to the
account logged in then. The evidence, best first:

- **Switch logs.** ``Switched from account X to Y`` lines in ``rota.log``
  and in the log of the tool rota imported from (its ``rota: imported
  accounts and settings from <dir>`` line names it; claude-swap writes the
  same line format). Numbers are resolved to identities as of the line's
  time, following that log's ``Added``/``Removed`` lines, so a number
  reused for another account later is not confused with the old one.
- **Logins in transcripts.** Claude Code writes a ``credential_org``
  attachment (the organization of the login it holds) into a transcript
  when a session picks up credentials; it marks who was logged in from
  then on. Only used before the first switch line, where no log speaks.

Before the earliest evidence nobody is credited: that spend stays
unattributed rather than credited to a guess. An account no longer in the
profile keeps its identity (key ``x:<email>|<org>``) so its spend is still
its own.

The transcript scan reads only what was appended since the last one
(offsets in the shared cache), and only files touched since ``lo``.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from claude_rotater.paths import get_default_backup_root, get_shared_cache_dir

_logger = logging.getLogger(__name__)

Identity = tuple[str, str]  # (email, organization uuid); email "" when unknown
Span = tuple[float, float, str]  # [start, end) and the account key on duty

SCAN_FILENAME = "credential_orgs.json"
SCAN_SCHEMA = 1
LOGIN_LOOKBACK_S = 14 * 86400.0  # a login this long before the range may still hold in it
_CHUNK = 1 << 22
_NEEDLE = b'"credential_org"'

_LINE_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ - \w+ - (.*)$")
_SWITCH_RE = re.compile(r"^Switched from account (\S+) to (\d+)\s*$")
_ADDED_RE = re.compile(
    r"^Added account (\d+)(?:: (\S+@\S+))?(?: from [^:]+: (\S+@\S+))?(?: \(org: ([^)]*)\))?\s*$"
)
_REMOVED_RE = re.compile(r"^Removed account (\d+)(?:: (\S+))?")
_IMPORT_RE = re.compile(r"^rota: imported accounts and settings from (.+?) into ")


def removed_key(identity: Identity) -> str:
    return f"x:{identity[0]}|{identity[1]}"


def key_identity(key: str) -> Identity | None:
    """The identity a :func:`removed_key` names, None for a plain number."""
    if not key.startswith("x:"):
        return None
    email, _, org = key[2:].partition("|")
    return email, org


# -- what each data dir knows ----------------------------------------------------


def read_sequence(data_dir: Path) -> dict[str, Identity]:
    """``{number: identity}`` from a data dir's ``sequence.json``."""
    try:
        data = json.loads((data_dir / "sequence.json").read_text())
    except (OSError, ValueError):
        return {}
    accounts = data.get("accounts") if isinstance(data, dict) else None
    out: dict[str, Identity] = {}
    for number, acc in (accounts.items() if isinstance(accounts, dict) else ()):
        if isinstance(acc, dict) and isinstance(acc.get("email"), str):
            out[str(number)] = (acc["email"], acc.get("organizationUuid") or "")
    return out


@dataclass(frozen=True)
class LogLine:
    at: float
    kind: str  # "switch", "added", "removed", "import", "start"
    number: str = ""  # switch: the target; added/removed: the account
    frm: str = ""  # switch: the source ("" when not a number)
    email: str = ""
    org: str | None = None  # added: None when the line names none
    path: str = ""  # import: the source dir


def log_paths(data_dir: Path) -> list[Path]:
    """A dir's logs, oldest rotation first: ``x.log.3`` … ``x.log``."""
    try:
        names = os.listdir(data_dir)
    except OSError:
        return []
    found: list[tuple[str, int, str]] = []
    for name in names:
        m = re.fullmatch(r"(.+\.log)(?:\.(\d+))?", name)
        if m:
            found.append((m.group(1), -int(m.group(2) or 0), name))
    return [data_dir / name for _, _, name in sorted(found)]


def read_log(path: Path) -> list[LogLine]:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    out: list[LogLine] = []
    started = False
    for raw in text.splitlines():
        if started and not any(w in raw for w in ("Switched from", "Added account",
                                              "Removed account", "imported accounts")):
            continue
        m = _LINE_RE.match(raw)
        if not m:
            continue
        try:
            at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        if not started:  # the file's first line: the tool was running from then
            out.append(LogLine(at, "start"))
            started = True
        msg = m.group(2)
        if s := _SWITCH_RE.match(msg):
            frm = s.group(1) if s.group(1).isdigit() else ""
            out.append(LogLine(at, "switch", s.group(2), frm))
        elif a := _ADDED_RE.match(msg):
            out.append(LogLine(at, "added", a.group(1), email=a.group(2) or a.group(3) or "",
                               org=a.group(4)))
        elif r := _REMOVED_RE.match(msg):
            out.append(LogLine(at, "removed", r.group(1), email=r.group(2) or ""))
        elif i := _IMPORT_RE.match(msg):
            out.append(LogLine(at, "import", path=i.group(1)))
    return out


class Directory:
    """Every identity seen anywhere, to resolve a bare org or email."""

    def __init__(self) -> None:
        self.by_org: dict[str, Identity] = {}
        self.by_email: dict[str, set[Identity]] = {}

    def add(self, identity: Identity) -> None:
        email, org = identity
        if not email:
            return
        if org and org != "personal":
            self.by_org.setdefault(org, identity)
        self.by_email.setdefault(email, set()).add(identity)

    def resolve(self, email: str, org: str | None) -> Identity | None:
        if org and org != "personal" and org in self.by_org:
            return self.by_org[org]
        if email:
            known = self.by_email.get(email, set())
            if org is not None:
                exact = [i for i in known if i[1] == ("" if org == "personal" else org)]
                if exact:
                    return exact[0]
            if len(known) == 1:
                return next(iter(known))
            return (email, "" if org in (None, "personal") else org)
        return ("", org) if org and org != "personal" else None


# -- switch logs -------------------------------------------------------------------


Switch = tuple[float, Identity | None, Identity | None]  # (at, to, from)


def unnamed(number: str) -> Identity:
    """A number of the profile's own that no list names: kept as the number."""
    return ("", f"#{number}")


def resolve_switches(lines: Sequence[LogLine], initial: dict[str, Identity],
                     fallback: dict[str, Identity], directory: Directory, *,
                     own: bool = False) -> list[Switch]:
    """A log's switches with both ends as identities as of each line.

    ``initial`` is the numbering the log starts from (the imported one);
    ``fallback`` names a number no line ever did (the dir's own list). In
    the profile's ``own`` log a number nothing names is still that number.
    """
    current: dict[str, Identity | None] = dict(initial)

    def who(number: str) -> Identity | None:
        if not number:
            return None
        if number in current:
            return current[number]
        return fallback.get(number, unnamed(number) if own else None)

    out: list[Switch] = []
    for line in lines:
        if line.kind == "added":
            current[line.number] = directory.resolve(line.email, line.org)
        elif line.kind == "removed":
            current[line.number] = None
        elif line.kind == "switch":
            out.append((line.at, who(line.number), who(line.frm)))
    return out


def legacy_dir(lines: Sequence[LogLine]) -> Path | None:
    for line in lines:
        if line.kind == "import" and line.path:
            return Path(os.path.expanduser(line.path))
    return None


def read_active(data_dir: Path) -> str | None:
    try:
        value = json.loads((data_dir / "sequence.json").read_text()).get("activeAccountNumber")
    except (OSError, ValueError, AttributeError):
        return None
    return str(value) if value is not None else None


Mark = tuple[float, Identity | None]  # from ``at`` on, this account was on duty


def _log_switches(lines: Sequence[LogLine], initial: dict[str, Identity],
                  fallback: dict[str, Identity], directory: Directory,
                  active: Identity | None, *, own: bool) -> tuple[list[Switch], list[Mark]]:
    """A dir's switches, and a mark at its log's first line: whoever its
    first switch left (``active`` when it never switched) was on duty from
    when the tool started, as it was watching."""
    for line in lines:
        if line.kind == "added" and line.email:
            directory.add(directory.resolve(line.email, line.org) or (line.email, ""))
    switches = resolve_switches(lines, initial, fallback, directory, own=own)
    starts = [line.at for line in lines if line.kind == "start"]
    if not starts:
        return switches, []
    holder = switches[0][2] if switches else active
    return switches, [(min(starts), holder)]


def profile_switches(data_dir: Path, directory: Directory) -> tuple[list[Switch], list[Mark]]:
    """Every switch the profile's own log and its imported tool's log
    record (oldest first), and when each tool started watching."""
    own = sorted((line for p in log_paths(data_dir) for line in read_log(p)),
                 key=lambda line: line.at)
    seq = read_sequence(data_dir)
    for identity in seq.values():
        directory.add(identity)
    legacy = legacy_dir(own)
    legacy_seq: dict[str, Identity] = {}
    switches: list[Switch] = []
    marks: list[Mark] = []
    if legacy is not None and os.path.realpath(legacy) != os.path.realpath(data_dir):
        old = sorted((line for p in log_paths(legacy) for line in read_log(p)),
                     key=lambda line: line.at)
        legacy_seq = read_sequence(legacy)
        for identity in legacy_seq.values():
            directory.add(identity)
        more, started = _log_switches(old, {}, legacy_seq, directory, None, own=False)
        switches += more
        marks += started
    active = read_active(data_dir)
    # rota keeps the imported numbering; a number its own log later added
    # or removed meant the imported account until then
    renumbered = {line.number for line in own if line.kind in ("added", "removed")}
    initial = {n: i for n, i in legacy_seq.items() if n in renumbered}
    holder = (seq.get(active) or unnamed(active)) if active else None
    more, started = _log_switches(own, initial, seq, directory, holder, own=True)
    switches += more
    marks += started
    switches.sort(key=lambda s: s[0])
    return switches, marks


# -- logins in transcripts -----------------------------------------------------------


def _scan_file(path: str, offset: int) -> tuple[int, list[tuple[float, str]]]:
    """``(new offset, [(at, org)])``: the ``credential_org`` lines from
    ``offset`` to the last complete line."""
    found: list[tuple[float, str]] = []
    consumed = offset
    try:
        fh = open(path, "rb")
    except OSError:
        return offset, found
    with fh:
        fh.seek(offset)
        carry = b""
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            buf = carry + chunk
            end = buf.rfind(b"\n")
            if end < 0:
                carry = buf
                continue
            done, carry = buf[:end + 1], buf[end + 1:]
            i = done.find(_NEEDLE)
            while i >= 0:
                start = done.rfind(b"\n", 0, i) + 1
                stop = done.find(b"\n", i)
                found.extend(_parse_login(done[start:stop]))
                i = done.find(_NEEDLE, stop)
            consumed += len(done)
    return consumed, found


def _parse_login(raw: bytes) -> list[tuple[float, str]]:
    try:
        entry = json.loads(raw)
        attachment = entry.get("attachment") or {}
        org = attachment.get("organizationUuid")
        stamp = entry.get("timestamp")
        if attachment.get("type") != "credential_org" or not org or not stamp:
            return []
        at = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return []
    return [(at, str(org))]


def transcript_logins(projects_dirs: Iterable[str], lo: float,
                      cache_path: Path | None = None) -> list[tuple[float, str]]:
    """Every ``(at, org)`` login transcripts under ``projects_dirs`` record
    at or after ``lo``, oldest first. Reads only what grew since the cached
    scan; files untouched since ``lo`` are skipped."""
    cache_path = cache_path or get_shared_cache_dir() / SCAN_FILENAME
    try:
        cached = json.loads(cache_path.read_text())
        files = cached.get("files", {}) if cached.get("schemaVersion") == SCAN_SCHEMA else {}
    except (OSError, ValueError, AttributeError):
        files = {}
    changed = False
    out: list[tuple[float, str]] = []
    for root in projects_dirs:
        for dirpath, _, names in os.walk(root):
            for name in names:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if st.st_mtime < lo:
                    continue
                entry = files.get(path)
                offset, events = (entry[0], entry[1]) if entry else (0, [])
                if st.st_size < offset:  # rewritten: start over
                    offset, events = 0, []
                if st.st_size > offset:
                    offset, more = _scan_file(path, offset)
                    events = [*events, *([round(a, 3), o] for a, o in more)]
                    files[path] = [offset, events]
                    changed = True
                out.extend((a, o) for a, o in events if a >= lo)
    if changed:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"schemaVersion": SCAN_SCHEMA, "files": files}))
            os.replace(tmp, cache_path)
        except OSError as e:
            _logger.warning(f"costs: could not save the login scan: {e!r}")
    out.sort()
    return out


# -- the timeline --------------------------------------------------------------------


def all_identities(directory: Directory, data_dirs: Iterable[Path]) -> None:
    """Teach ``directory`` every account any profile on this machine holds."""
    for d in data_dirs:
        for identity in read_sequence(d).values():
            directory.add(identity)


def build_spans(
    switches: Sequence[Switch],
    logins: Sequence[tuple[float, Identity | None]],
    key: dict[Identity, str],
    lo: float,
    hi: float,
) -> list[Span]:
    """Who was on duty over ``[lo, hi)``.

    From the first switch on, the switch logs alone: each target holds
    until the next switch. Before it, each earlier mark (a transcript
    login, a tool starting to watch) holds until the next mark or the first
    switch. Before the earliest evidence, and wherever the account is
    unknown, nobody: that time is left out.
    """
    first_switch = switches[0][0] if switches else float("inf")
    marks: list[tuple[float, Identity | None]] = [
        (at, who) for at, who in logins if at < first_switch]
    marks += [(at, to) for at, to, _ in switches]
    marks.sort(key=lambda m: m[0])
    spans: list[Span] = []
    for i, (at, who) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else hi
        a, b = max(at, lo), min(end, hi)
        if who is None or b <= a:
            continue
        name = key.get(who) or (who[1][1:] if who[1].startswith("#") else removed_key(who))
        if spans and spans[-1][2] == name and spans[-1][1] == a:
            spans[-1] = (spans[-1][0], b, name)
        else:
            spans.append((a, b, name))
    return spans


def profile_keys(data_dir: Path) -> dict[Identity, str]:
    """``{identity: number}`` for the accounts the profile holds now."""
    return {identity: number for number, identity in read_sequence(data_dir).items()}


def timeline(
    data_dir: Path,
    projects_dirs: Iterable[str],
    lo: float,
    hi: float,
    *,
    other_dirs: Iterable[Path] = (),
    cache_path: Path | None = None,
) -> list[Span]:
    """The profile's duty spans over ``[lo, hi)`` from every source of
    evidence (see the module doc)."""
    directory = Directory()
    all_identities(directory, [get_default_backup_root(), *other_dirs])
    switches, logins = profile_switches(data_dir, directory)
    first_switch = switches[0][0] if switches else hi
    if first_switch > lo:
        for at, org in transcript_logins(projects_dirs, lo - LOGIN_LOOKBACK_S, cache_path):
            if at < first_switch:
                logins.append((at, directory.resolve("", org)))
    return build_spans(switches, logins, profile_keys(data_dir), lo, hi)
