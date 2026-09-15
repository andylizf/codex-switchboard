# codex-switchboard

One `codex` command, several ChatGPT accounts, swapped underneath a running session.

Every ChatGPT account hits its weekly Codex limit at a different moment. The tools that
exist for this swap `~/.codex/auth.json` and need the TUI restarted before the new account
takes effect. This one does not restart anything: the TUI is attached to a shared
`codex app-server` engine, and a controller hands that engine a different account's token
the moment the current one is refused. The thread and its context stay where they are, a
turn that failed on the limit is re-run on the new account by itself, and the only visible
change is the weekly percentage in the status bar.

It works on accounts that are yours. OpenAI's terms do not allow sharing an account with
anyone else, and nothing here changes that.

## What you get

    codex                            TUI on whichever account can run right now
    codex resume                     same, with the session picker; history is shared
    codex-switchboard status         current account, its limits, engine pid, recent events
    codex-switchboard switch NAME    force an account (the loop still leaves it when refused)
    codex-switchboard pick           CODEX_HOME of the best account, for `codex exec` jobs
    codex-switchboard stop           stop controller and engine; the next `codex` restarts them

Two rules decide the account:

- Proactive: every 30 s the controller asks the backend about the current account. When the
  weekly window has less than 5 % left and another account has at least 5 %, it switches
  between turns, so a turn normally never hits the wall (`SWITCH_BELOW_PERCENT`).
- Reactive: every 3 s it scans recently touched threads for a turn that failed with
  `usageLimitExceeded`. If one did, it switches and queues a continuation into that thread
  via `thread/queue/add`, the API behind `codex queue`, so the work resumes without anything
  typed.

When every account is out, the engine stays logged in on one that still authenticates, so
the TUI keeps working with whatever fallback that account has, and `status` says so. An
optional webhook gets a message then, and again when quota is back.

## Requirements

- codex-cli 0.154 or later, verified on 0.154.0. The login mode this relies on,
  `chatgptAuthTokens`, is the one the Codex desktop app uses; it is marked unstable in the
  protocol schema and needs the `experimentalApi` capability.
- Python 3.10+, standard library only.
- macOS or Linux.

## Install

    git clone https://github.com/andylizf/codex-switchboard
    cd codex-switchboard
    ./install.sh

That links `codex-switchboard` and `codex-acct` into `~/.local/bin` and installs a `codex`
shell function for your shell, fish, bash or zsh, from `shell/`. The function sends the
interactive TUI to the engine and every other subcommand, `exec`, `login`, `mcp` and the
rest, straight to the binary on the default account. Re-run `./install.sh` after a
`git pull`.

Log the accounts in, one profile each. `default` is your existing `~/.codex` login; each
further account gets its own directory under `~/.codex-profiles/`, holding only that
account's `auth.json`, with config, skills and thread history symlinked back to `~/.codex`:

    codex-acct login work        # opens the browser login for a second account
    codex-acct login personal
    codex-acct ls

Log in on each machine separately rather than copying an `auth.json` across. Two copies
share one refresh token, it rotates on use, and the copy that loses the race is logged out
for good (openai/codex#19803). `codex-acct` refuses to use a profile minted on another
host for that reason.

Optionally set the order, first preferred:

    printf 'default\nwork\npersonal\n' > ~/.codex-profiles/pool/order

Without the file the pool is `default` followed by every logged-in profile in name order.
For alerts, put a Slack- or Feishu-style incoming-webhook URL in
`~/.codex-profiles/pool/alert-webhook`, mode 0600.

## How it works

Three pieces, all on Codex's own ChatGPT auth path: no proxy, no relay, no API key.

1. Engine: `codex app-server --listen unix://` under the `pool` profile. The pool is a
   `codex-acct` profile with no `auth.json` of its own; config and the thread store are
   symlinked to `~/.codex`, so `resume` sees every session.
2. Controller: `switchboard.py run`, detached by `ensure`. It connects to the engine as a
   client and logs it in with `account/login/start {type: chatgptAuthTokens}`: the client
   hands over an access token and can hand over a different one at any time. Probing an
   account and refreshing its token go through a throwaway `codex app-server` under that
   profile's own `CODEX_HOME`, so every `auth.json` keeps refreshing through Codex's own
   code and stays single-copy per machine.
3. TUI: `CODEX_HOME=~/.codex-profiles/pool codex --remote unix://`.

State and logs live in `~/.codex-profiles/pool/`: `switchboard.log`, one JSON line per
event; `engine.log`, the app-server's stderr; `switchboard-state.json`.

## The one thing to know: a thread pins the credential of its first turn

Codex talks to the backend over a Responses websocket, one connection per thread, opened
on the thread's first turn with the credential current at that moment. An account swap on
the engine does not reach an existing thread: not after `account/logout` + login, a second
login, `thread/resume`, compaction, a model or reasoning-effort change, or 200 s idle. Two
things do reach it. A new thread connects with the current account. And a turn that fails
on the usage limit closes the connection, so the thread's next turn reconnects with the
current account. The design relies on exactly those two.

So what you see: a thread that is already running when its account is switched away from
keeps that account until the account is refused; its first turn after that fails with the
usage-limit message, and the switchboard queues the continuation, which reconnects on the
current account. A thread started after the switch is on the new account from its first
turn.

Plain HTTP, a custom provider with `supports_websockets = false`, would remove the pin, but
it re-sends the whole context on every model call: measured at about one extra second per
call with 80 KB of context in the thread, paid once per tool step. Too slow, so the
websocket stays.

Never call `account/logout` on the engine: it tries to revoke the token it holds, which is a
profile's live token.

## Known quirks

- Engine notifications go only to the connection that started a thread, so the controller
  polls: 30 s for limits, 3 s for failed turns. `thread/loaded/list` is per connection too;
  the controller reads the shared thread table instead.
- `codex exec` threads are skipped by the reactive path: that process exits on the failure,
  so nobody would pick up a queued continuation. Such jobs choose their account up front
  with `CODEX_HOME="$(codex-switchboard pick)"`.
- The controller reads the shared thread table, so a thread run by some other Codex process
  on the machine that fails on its limit also gets a continuation queued; that process picks
  it up, still on its own account, and fails again. One extra line there, nothing else.
- Probing and token refresh spawn short-lived `codex app-server` helpers; they are killed by
  process group because the `codex` node wrapper does not take its native child with it.
- `/status` in the TUI shows the engine's current account and its limits. A TUI that shows a
  different account than `codex-switchboard status` is not attached to the engine; check
  its command line for `--remote unix://`.
- The controller is not a service. `codex` re-runs `ensure`, which restarts a dead
  controller or engine. A TUI attached to an engine that dies loses its connection like any
  crash; `codex resume` picks the thread back up.
- An upgrade of codex-cli can rename the fields of the unstable login mode. `status` and
  the log say so immediately.
