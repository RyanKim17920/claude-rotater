# rota

Multi-account rotation for Claude Code. Keep several Claude accounts logged
in, see every account's 5-hour and 7-day usage in one live TUI, and let rota
switch to the account with the most quota left before you hit a limit. Works
with the Claude Code CLI and the VS Code extension, on macOS, Linux and
Windows.

## Features

- Live TUI: one line per account with its 5h, 7d and per-model (e.g. Fable)
  usage, a bar whose lighter tint runs out to where the window is projected
  to be at its reset, a countdown to each reset and one forecast (`ok`, `~7d
  limit in 15h`); the highlighted account's 5h/7d charts below, and long-term history
  behind `h`. Estimates are marked by `~`, a lighter tint or a dotted line
  and coloured by outcome: green under the threshold, yellow close, red over.
- Costs behind `$`, priced by [ccusage](https://github.com/ryoppippi/ccusage)
  from your local transcripts and run only when you open the page. Spend is
  credited to the account rota had on duty at the time (approximate: other
  machines and chats still on an old login are not seen). One chart, cycled
  with ←/→: all history, 30 days, the last week and the last day, bars by
  model with dashed lines at round dollar values (the top one at or above
  the tallest bar), then each account's ~$ per 1% of its 5h and of its 7d
  window over time. That figure is sampled every few points the reading
  climbs (4 for 5h, 6 for 7d: whole-percent readings are too coarse for
  less), the spend over each climb summed over every profile that holds the
  account, so a busy day gives dozens of points; they are kept, and fold
  into one a day after five weeks. Each account gets its own series (the
  median per column over a faint quartile spread) and a robust trendline,
  flat and "steady" unless the change stands out from its noise; a falling
  line reads "limit shrinking" (a 1% buys less). Climbs with no local spend
  (other machines, claude.ai) are hollow points or left out, and a trend on
  little data says it is provisional and gives its change over the days it
  saw, not stretched to 4 weeks. Without `ccusage`, `bunx` or `npx` the page says so.
- Automatic switching while the TUI is open, or headless with `rota auto`.
  By default (`pace`) it spreads usage while every account's weekly quota is
  safe, moving only when the 5-hour window gets tight and keeping a fresh
  5-hour backup, and concentrates on an account once the pace its weekly
  headroom needs before its reset leads the rest by `autoswitch.paceMargin`
  (1.5x), unless your own burn would use it up anyway. Every verdict reads
  like `switch → #2: #2 resets in 9h with 40% left (needs 4.4%/h) …`. Other
  strategies: `expiring-first`, `best` or `consume-first`
  (`autoswitch.strategy`). Below the threshold a move is confirmed by two
  readings. It moves early when the
  active account would be over the threshold (or the limit) by its next
  reading, judged from that account's own burn and its largest recent jump
  per reading; such a move skips the cooldown but not the hourly cap, and
  with nowhere to go rota says "no account with headroom". It fails over only when
  the active login is dead (never on unknown usage such as a 429), restores
  the marked account's own login if another account's login appears in its
  place (or stops and asks for a re-login), and pauses after
  `autoswitch.maxSwitchesPerHour` (default 4) switches in an hour.
  Switches take Claude Code's own credential locks, so they never collide
  with a token refresh, and the active token is never refreshed while a
  claude is running.
- One auto engine per data dir: a second rota on the same data dir is
  view-only, shows the engine's own verdict and forecast, and says when the
  engine runs older code.
- Log accounts in from the TUI without touching the account you are using,
  including a fresh login for the active account. Idle accounts' tokens are
  refreshed ahead of expiry only when `accounts.proactiveTokenRefresh` is on.
- `rota run <account>`: run Claude Code as another account in one terminal
  only, so two accounts can work in parallel.
- Reads live usage from Claude Code's statusline, so the active account is
  not polled while you work. Every account is still polled (every 10 min,
  every 5 min while its usage climbs), so use on another machine or on
  claude.ai shows up too.

## Install

```bash
uv tool install git+https://github.com/RyanKim17920/claude-rotater
rota
```

The command is `rota`. It can sit beside an installed upstream `cswap`: the
two never share data or Keychain items.

## Use

```bash
rota                           # the TUI: accounts, logins, settings, live auto-switch
rota switch [num|email]        # rotate, or switch to one account
rota switch --strategy best    # switch to the account with the most quota left
rota status [--json]           # the current account
rota run <num|email> [-- ...]  # run Claude Code as an account, this terminal only
rota auto                      # headless auto-switch loop (see rota auto -h)
```

Add accounts in the TUI: `a` asks how on the bottom line (`b` log in with
the browser, `c` use the account Claude Code is logged in with now, `t`
paste a token); `l` and `t` jump straight to logging in and pasting a token.
Nothing pops up: every question (`y` / `n`, `esc` cancels), text entry and
result shows on that one line. Do not use `/logout` to change accounts; it can
revoke the stored refresh token.

### Keys

| Key | Action |
|-----|--------|
| `enter` | switch to the highlighted account |
| `a` | add an account: log in, use the current login, or paste a token |
| `l` | log an account in (or re-log one marked *re-login needed*) |
| `t` | add a setup-token or API key |
| `d` | turn an account off or on for rotation |
| `x` | remove an account |
| `m` | auto or manual: in manual rota never switches on its own, and with `autoswitch.manualLimitGuard` (on by default) still steps in at a hard limit; the header shows which (click it to flip too) |
| `y` / `Y` | copy the highlighted account's line / every account's line as plain text |
| `f` | refresh usage now |
| `h` / `esc` | history: past 5h peaks and the 7d curve across weeks, and back |
| `$` / `esc` | costs: today / 7d / 30d, one chart cycled with `←`/`→` (all, 30 days, week, day, $ per 1% of 5h and of 7d per account with its trendline), per-account cost and ~$ per 1%, per-model cost and trend (`r` reprices today, `y`/`Y` copy); needs `ccusage`, or Node/Bun to fetch it |
| `?` / `esc` | every key, grouped, in place of the charts, and back |
| `s` | set up or remove the statusline feed |
| `c` | settings (`↑`/`↓` select, `←`/`→` change, digits or `enter` exact value, `esc` back) |
| `ctrl+t` | theme |
| `q` / `ctrl+c` | quit |

rota takes the mouse, so dragging does not select text. `y` and `Y` copy
through `pbcopy` (macOS), `wl-copy` or `xclip`/`xsel` (Linux), or else the
terminal's OSC 52. To select with the mouse anyway, hold Option (most macOS
terminals) or Shift (most others) while dragging.

### Statusline step

Claude Code hands your statusline command the active account's usage on
every refresh. Pass it to rota so the auto-switcher reacts at once and polls
that account only every `pollSpareSeconds` to check the numbers. Each chat
reports its own last response, so rota dates a reading by that response,
ignores a chat's repeated renders and keeps the highest reading per window.
In a statusline script:

```bash
input=$(cat)
rota feed <<<"$input" >/dev/null 2>&1 &
```

or put `rota feed --tee` in front of an existing command:
`"command": "rota feed --tee | ~/.claude/statusline.sh"`. With no statusline
yet, `rota feed --line` is one: it prints the model and 5h/7d usage.
`rota feed` never fails, so it cannot break your statusline.
It also exits quietly without recording anything when the data dir it feeds
(`ROTA_HOME` in the command, else `~/.rota`) does not exist; the TUI header
then says `feed: not set up`, and `s` sets the statusline up for the TUI's own
data dir.

## Data and profiles

rota keeps everything in `~/.rota`: accounts, settings, usage cache and,
except on macOS, credential backups. On macOS the backups are Keychain items
under the service `rota`.

Set `ROTA_HOME` to run a separate profile with its own accounts and
settings. Its Keychain service is `rota-<8 hex>` (the start of the SHA-256
of the directory's absolute path), so profiles never collide.
Usage history belongs to the account, not the profile: it lives in
`~/.rota/cache` (or `$ROTA_SHARED_HOME/cache`) for every profile, so an
account two profiles hold is polled by one of them, counts its requests
once, and shows the same forecast in both. Each profile still switches only
its own Claude config dir's login.
Two settings on the Profile section of the config page (`c`) suit such a
profile: `run.shareDefault` (off: `rota run` keeps the main `~/.claude`
settings, CLAUDE.md and skills out of its sessions unless told `--share`)
and `accounts.oauthOnly` (on: API-key accounts are never switched to, by
hand or automatically).

On first run, if the data dir does not exist yet and an upstream claude-swap
store does (`~/.claude-swap-backup`, or `~/.local/share/claude-swap` on
Linux), rota copies its accounts, settings and usage cache, and copies the
macOS Keychain backups to rota's service. The upstream store and its
Keychain items are never moved, changed or deleted. A `ROTA_HOME` profile
imports only when `ROTA_IMPORT_FROM` names the directory to copy from. If
the source store kept its Keychain items under a service other than
`claude-swap` (an upstream profile, for instance), name it in
`ROTA_IMPORT_KEYCHAIN_SERVICE`. To start empty instead, create the data dir
first.

To uninstall, run `uv tool uninstall claude-rotater`, delete `~/.rota`, and
on macOS delete the `rota` items in Keychain Access.

## License

MIT. Based on claude-swap by realiti4.
