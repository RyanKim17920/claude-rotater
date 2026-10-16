# claude-swap

Multi-account switcher for Claude Code. Easily switch between multiple Claude accounts without logging out, or let it switch for you before you hit a rate limit. Track usage for every account in a live dashboard, and run accounts in parallel. Works with both the Claude Code CLI and the VS Code extension.

## Installation

### Using uv (recommended)

```bash
uv tool install claude-swap
```

### Using pipx

```bash
pipx install claude-swap
```

### From source

```bash
git clone https://github.com/realiti4/claude-swap.git
cd claude-swap
uv sync
uv run cswap -h
```

### Updating

```bash
uv tool upgrade claude-swap
# or
pipx upgrade claude-swap
```

## Usage

Everything interactive happens in the TUI: run `cswap` on its own. The few other commands are for scripts:

```bash
cswap                           # the TUI: accounts, logins, settings, live auto-switch
cswap switch [num|email]        # rotate, or switch to one account
cswap status [--json]           # the current account
cswap run <num|email> [-- ...]  # run Claude Code as an account, this terminal only
cswap auto                      # headless auto-switch loop
```

### Add accounts

Open `cswap` and:

- press `a` to store the account Claude Code is logged in with right now, or
- press `l` to log another account in. It runs `claude auth login` in a separate, temporary profile, so the account you are using is never touched.

Do not run `/logout` to change accounts: current Claude Code may revoke the refresh token stored for the account you are leaving.

### Switch accounts

Rotate to the next account:

```bash
cswap switch
```

Or switch to a specific account:

```bash
cswap switch 2
cswap switch user@example.com
```

Not sure which one? The TUI shows every account's 5-hour and 7-day usage and reset times at a glance (`enter` switches to the highlighted one). Or let claude-swap auto-pick by remaining quota — `cswap switch --strategy best` (most quota left) or `--strategy next-available` (skip rate-limited accounts).

**Note:** You usually don't need to restart — on Linux/Windows the new account is picked up automatically, and on macOS after the Keychain cache expires. To apply it instantly, restart Claude Code or reopen the VS Code extension tab. See [Tips](#tips) for the per-platform details.

### Automatic switching

Let claude-swap watch your usage and switch for you. When the active account's 5-hour or 7-day window reaches the threshold (default 90%), it switches to the account with the most quota left — before you hit the limit, and safe to run while Claude Code is working:

```bash
cswap auto                     # foreground loop
cswap auto --threshold 80      # switch earlier
cswap auto --model Fable       # also switch when the Fable weekly limit is hit
cswap auto --once              # single check-and-switch, for cron/scripts
cswap auto --dry-run           # log what it would do, never switch
cswap auto --strategy consume-first   # burn the soonest-resetting account first
```

<details>
<summary>How it behaves & advanced usage</summary>

- Runs safely alongside Claude Code: switches take the same credential locks Claude Code uses, so a swap never collides with a token refresh.
- A cooldown (default 5 min) and a hysteresis margin stop it flip-flopping near the threshold: a proactive switch only lands on an account that's below the threshold *and* better than the current one by the margin — a candidate that clears the margin is always taken, but two accounts hovering at the line never ping-pong. When every account is exhausted it keeps checking on a bounded slow cadence, waking sooner for an imminent reset.
- **Strategies** (`--strategy`, or *Strategy* on the TUI's settings page): `best` (default) stays put until the active account nears its limit, then moves to the account with the most quota left. `consume-first` proactively keeps you on the account whose **weekly window resets soonest** — use-it-or-lose-it — switching to a sooner-resetting account (with room to spare) even below the threshold, so perishable weekly quota isn't wasted.
- Usage polling is adaptive and event-driven: each account is polled about every ten minutes (`autoswitch.pollSpareSeconds`), sooner when its burn projects a threshold crossing (never faster than every 3 minutes), once right after each known window reset, and never above an hourly budget per account (slower after 429s). Between polls the loop sleeps. With the [statusline feed](#live-usage-from-claude-codes-statusline) set up, the active account is not polled at all while Claude Code is reporting its usage.
- It fails safe: if a usage check errors it keeps trusting the last-known numbers while retries back off, and an expired token on an idle machine makes it hold rather than fail over (Claude Code refreshes the token on your next message).
- An account whose refresh token has died is quarantined and reported (*re-login needed*) until you log it in again: press `l` on its row in the TUI. API-key accounts are never rotated onto unless you pass `--include-api-key-accounts`.
- To hold an account out of rotation yourself — a work account you don't want touched, one you're resting — press `d` on it in the TUI (again to put it back). Disabled accounts are skipped by auto-switch, bare `cswap switch`, and the `best` / `next-available` strategies, but stay fully managed and remain a valid explicit `cswap switch <num|email>` target.
- By default only the account-wide 5h/7d windows drive switching. If you work on one model and hit its **weekly per-model limit** first (e.g. Fable), add `--model Fable` (or set *Model* on the TUI's settings page) to fold that model's window into the decision, so it switches off an account whose model quota is spent even while its 5h/7d windows still have room.
  - **Model names** are Anthropic's own per-model `display_name`s, matched case-insensitively (e.g. `Fable`, `Opus`); several are comma-separated, and `all` takes every per-model window an account reports.
- The headless loop also keeps idle accounts logged in, like the TUI does: each stored account that is neither active nor used by a `cswap run` session has its token refreshed shortly before it expires (not with `--once` or `--dry-run`).

For cron/systemd timers, `--once` reports the outcome in its exit code (`0` switched, `1` error, `2` nothing to do, `3` blocked — no viable target), and `--json` emits one JSON event per line:

```bash
*/5 * * * * cswap auto --once --json >> ~/.cswap-auto.log 2>&1
```

Defaults like the threshold and cooldown come from the TUI's settings page; flags override them (see [Configuration](#configuration)).

</details>

### Run multiple accounts at the same time (session mode)

Launch Claude Code as a specific account in the current terminal only — every other terminal and the VS Code extension stay on your default account, so two accounts can work in parallel.

```bash
cswap run 2                     # launch Claude Code as account 2, here only
cswap run user@example.com      # by email
cswap run 2 -- --resume         # everything after '--' is forwarded to claude
cswap run 2 --share-history     # share your chat history with this account too
cswap run 2 --require-session   # refuse rather than run plain claude if 2 is the default login
```

Sessions use your normal `~/.claude` setup (settings, CLAUDE.md, skills, MCP servers, etc.), but each account keeps its own chat history — pass `--share-history` if you want your accounts to continue the same conversations.

Running the account that is already your default login launches plain `claude` on that login instead of a session (a second copy of the active credential would go stale). Scripts that need the isolation guaranteed can pass `--require-session`, which refuses in that case instead.
  
A session refreshes its own copy of the account's token, so once it exits, the credential it rotated is captured back into the account's stored backup before a switch or usage check uses that backup. While a session is still running, `cswap switch` refuses to move the default login onto its account if the stored backup has already fallen behind (activating it could only fail); exit the session first, or pick another account. While a session runs, its account's usage is read with the session's own credential and never refreshed by cswap; a read the server refuses shows as token expired, and is not requested again, until the session renews the credential on its next call.

<details>
<summary>Sharing details — MCP servers & chat history</summary>

- With `--share-history`, a session started under one account shows up in `--resume` under the others, and nothing already saved is lost.
- User-scope MCP servers (`claude mcp add -s user`) are mirrored from your default profile on every launch — manage them there; changes made inside a session don't persist. Definitions are copied as-is (including inline `env`/`headers` values), but MCP OAuth logins are not — HTTP servers may ask you to authenticate once per profile via `/mcp`.
- `--no-share` turns sharing off and removes the mirrored MCP config (profiles that never mirrored are left alone).

</details>

### Interactive dashboard (TUI)

Run `cswap` on its own for the full-screen dashboard. It is one page: a compact list of every account with live 5h/7d usage, burn rate and the estimated time to the switch threshold, a usage plot for the highlighted account, and a status line for the auto-switcher. Works on macOS, Linux, and Windows.

<img src="assets/tui-watch.png" width="760" alt="cswap — live 5h/7d usage bars for every account, with reset times and the active account marked">

- **Auto-switch is always on while the TUI is open.** It runs the same engine as `cswap auto`, live, with the same guards: the threshold, hysteresis, the cooldown (shared with any `cswap auto` running beside it), and quarantine of dead tokens. There is no dry-run in the TUI; to keep an account out of rotation, disable it. You only need `cswap auto` for headless use.
- **Keys:** `enter` switch to the highlighted account, `l` log an account in, `a` add the current login, `t` add a token, `d` disable / enable, `x` remove, `f` refresh now, `c` settings, `ctrl+t` theme, `q` quit.
- **Log in (`l`):** runs `claude auth login` in a separate, temporary profile, so the active account is never touched, then stores the new login (or replaces the stored credential of an account you already manage). On a row marked *re-login needed* it pre-fills that account's address. The temporary profile and its keychain item are always deleted afterwards.
- **Idle accounts stay logged in.** While the TUI is open, every account that is neither active nor in use by a `cswap run` session has its token refreshed shortly before it expires. A row shows `login 2d` when the login itself (which only a new login renews) ends within `ui.loginWarnDays` days.
- **Settings page (`c`):** every setting, grouped into *Auto-switch*, *Polling* and *Appearance*. `↑`/`↓` move, `←`/`→` (or `-`/`+`) change the value in place (numbers step and stop at their range, choices cycle, toggles flip), `enter` or a digit types an exact value (`enter` saves, `esc` cancels), `ctrl+r` resets a key to its default, and `esc` goes back. Changes are saved at once and apply right away (for example, a new threshold restarts the auto-switcher with it); the bottom line shows the selected key's full help, range and default.

### Refresh expired tokens

In the TUI, press `l` on the account's row. The new login replaces the stored credential of the account you already manage; it never creates a duplicate.

## Tips

- **Do you need to restart after switching?** Usually not. On **Linux and Windows**, credentials are stored in a file and Claude Code re-reads them whenever that file changes, so the new account takes effect on your next message — no restart needed. On **macOS**, credentials live in the Keychain, which Claude Code caches for about 30 seconds; a running session picks up the switch once that cache expires. Restart Claude Code (or close and reopen the VS Code extension tab) only if you want the change to apply instantly.
- **Continuing sessions after switching:** You can keep using the same Claude Code session after switching — run `cswap switch` in any terminal and carry on. If you'd prefer a clean start, close and reopen Claude Code (or the VS Code extension tab) and use `--resume` to pick your previous session. Either way, the first message on the new account may use extra usage as its conversation cache rebuilds.

## How it works

- Backs up OAuth tokens and config when you add an account
- Swaps only the account-specific Claude login when you switch accounts;
  live account-independent OAuth state (such as MCP server logins) is
  preserved instead of being overwritten by a slot's older snapshot
- Account credentials stored securely using platform-appropriate methods
- Switches (manual and automatic) hold Claude Code's own credential locks while writing, so a swap never interleaves with a token refresh
- Auto-switch freshens a target's token before activating it, and quarantines accounts whose refresh token has died (recover by logging it in again with `l` in the TUI)
- Usage numbers refresh every few minutes — faster for an account being used or close to switching, slower for idle ones — keeping cswap comfortably inside Anthropic's rate limits however many dashboards you keep open on a machine. An age note like `· 6m ago` just means the next scheduled check hasn't come yet, not that something is stuck.

## Data locations

| Platform | Credentials | Config backups |
|----------|-------------|----------------|
| Windows | File-based (inside the backup directory, under `credentials/`) | `~/.claude-swap-backup/` |
| macOS | macOS Keychain | `~/.claude-swap-backup/` |
| Linux / WSL | File-based (inside the backup directory, under `credentials/`) | `${XDG_DATA_HOME:-~/.local/share}/claude-swap/` |

Session-mode profiles (`cswap run`) live under the backup directory in `sessions/`. Tool preferences (`settings.json`) and auto-switch state (`autoswitch_state.json` — cooldown and quarantined accounts; delete it to reset) live in the backup directory root.

On Linux/WSL, set `XDG_DATA_HOME` to override the default location.

## Advanced

### Live usage from Claude Code's statusline

Claude Code passes the active account's 5-hour and 7-day usage to your [statusline](https://code.claude.com/docs/en/statusline) command on every refresh, at no API cost. Hand it to claude-swap with `cswap feed`, which reads that JSON on stdin, appends a small record to `<backup root>/cache/feed/statusline.jsonl`, and exits quickly (it never fails, so it cannot break your statusline). The auto-switcher (TUI or `cswap auto`) wakes as soon as a record lands, reacts to the new reading at once, and stops polling the active account while the feed is fresh. Once it has been quiet for `autoswitch.feedStaleSeconds` (default 600) the account is polled like any other.

In a statusline script that already reads its input into a variable:

```bash
input=$(cat)
cswap feed <<<"$input" >/dev/null 2>&1 &
```

or put `cswap feed --tee` in front of an existing command, since `--tee` copies stdin to stdout: `"command": "cswap feed --tee | ~/.claude/statusline.sh"`.

A reading is credited to the account that is active when it arrives, unless that is not safe to assume. A running Claude Code session keeps its old token for a moment after a switch (until its next request, or up to ~30 s on macOS) and keeps showing its last response's numbers until it makes a new one. So readings from the first 90 s after a switch, readings from another `CLAUDE_CONFIG_DIR`, and readings whose reset times disagree with what claude-swap already knows about the active account are dropped. Sessions started with `cswap run` are credited to their own account. A dropped reading just leaves the account on normal polling.

### Configuration

Tool preferences live in `settings.json` in the backup root. Change them on the TUI's settings page (`c`), which validates every value and shows each key's range and default; hand-editing the file still works (out-of-range values are clamped on load).

### JSON output for scripting

Add `--json` to `status` or `switch` to emit a single machine-readable JSON object on stdout (human-readable notices go to stderr). Useful for scripting auto-swap and quota tracking.

```bash
cswap status --json                 # current active account
cswap switch --strategy best --json # switch, then report the result
cswap switch 2 --json
```

<details>
<summary>Example output & schema notes</summary>

```json
{
  "schemaVersion": 1,
  "active": { "number": 2, "email": "you@example.com", "managed": true, "usageStatus": "ok",
              "usage": { "fiveHour": { "pct": 25.0, "resetsAt": "2026-06-22T23:29:59Z" },
                         "sevenDay": { "pct": 16.0, "resetsAt": "2026-06-26T17:59:59Z" } } },
  "totalManagedAccounts": 3
}
```

Every payload carries a `schemaVersion` (currently `1`); on a handled error stdout is `{"schemaVersion":1,"error":{...}}` with a non-zero exit code. `switch` reports `{"switched": true|false, "from": …, "to": …, "reason": …}`.

Usage is served from a per-account cache: when the usage API is briefly unreachable, the last-known numbers are shown instead of nothing (the human view marks them with their age, e.g. `· 2m ago`). Rows with decision-trusted usage carry additive `usageFetchedAt`/`usageAgeSeconds` fields telling you how old the measurement is. Whenever `usage` is null but a last-known measurement exists — data too old to drive a decision (`usageStatus` stays `unavailable`), or a row in a non-`ok` state such as `token_expired` — additive `lastGoodUsage`/`lastGoodFetchedAt`/`lastGoodAgeSeconds` fields preserve the human display without making the account actionable. When `usage` is null and nothing else explains it (`usageStatus` is `unavailable`), an additive `usageError` names the last fetch failure by kind (e.g. `http-429`, `timeout`) and, while the cache is backing off from it, `usageRetryAt` gives the time of the next attempt. These fields apply to the managed active row from `status --json`.

An account row carries an additive `alias` field when the account has one (e.g. `"alias": "dev"`); accounts without one simply omit the key.

Weekly windows (`sevenDay` and per-model `scoped` entries — never `fiveHour`) additively carry pace fields once the week is ~a day old: `expectedPct` (where usage would sit if spread evenly across the week) and `aheadOfPace` (`true` when meaningfully above that — the same signal the human views show as an `(ahead)`/`(ahead of pace)` marker). `projectedExhaustionAt`/`willLastToReset` extrapolate the current rate into an ETA to 100% and a yes/no "will it last to the reset"; they stay `--json`-only since a linear projection is too rough to present as fact in the UI.

</details>

`cswap auto --json` emits an event *stream* instead — one JSON object per line (`{"schemaVersion":1,"event":"switch","ts":…, …}` with kinds like `poll`, `switch`, `no-switch`, `account-quarantined`, `all-exhausted`, `error`). The contract is additive: new kinds and fields may appear, so scripts should ignore unknown ones.

### Add an account from a raw token or API key

If you only have a long-lived setup-token (e.g., produced by `claude setup-token`)
or a Console API key (`sk-ant-api03-...` workspace key, `sk-ant-usr-...` personal
key, or a service-account key), press `t` in the TUI and paste it. The token type
is auto-detected and no Anthropic API calls are made.

**API-key accounts.** Any bare `sk-ant-...` value that is not an OAuth token
(`sk-ant-oat...`) registers a managed API-key account (the kind Claude Code uses
after `/login` with a key) rather than an OAuth setup-token. It switches like any
other account; since API keys have no subscription quota, they show no usage and the
usage-aware `switch` strategies never skip them as rate-limited. Create personal and
service-account keys scoped to a single workspace: a multi-workspace key needs an
`anthropic-workspace-id` header on every request, which Claude Code does not add for
a stored key.

## Uninstall

Delete the backup directory (see [Data locations](#data-locations)); on macOS also delete the `claude-swap` items in Keychain Access, which hold the stored credentials. Then uninstall the tool:

```bash
uv tool uninstall claude-swap
# or
pipx uninstall claude-swap
```

## Requirements

- Python 3.12+
- Claude Code installed and logged in

## License

MIT
