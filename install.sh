#!/usr/bin/env bash
# Links codex-switchboard and codex-acct into ~/.local/bin and installs the `codex` shell
# function for the current shell. Re-run after `git pull`; it is idempotent.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bin="$HOME/.local/bin"
mkdir -p "$bin"
ln -sfn "$here/switchboard.py" "$bin/codex-switchboard"
ln -sfn "$here/codex-acct" "$bin/codex-acct"
chmod +x "$here/switchboard.py" "$here/codex-acct"
echo "linked codex-switchboard and codex-acct into $bin"

case ":$PATH:" in
  *":$bin:"*) ;;
  *) echo "note: $bin is not on your PATH; add it, or the codex function falls back to plain codex" ;;
esac

shell_name="$(basename "${SHELL:-}")"
case "$shell_name" in
  fish)
    conf="$HOME/.config/fish/conf.d/codex-switchboard.fish"
    mkdir -p "$(dirname "$conf")"
    ln -sfn "$here/shell/codex.fish" "$conf"
    echo "installed the codex function for fish: $conf (open a new shell)"
    ;;
  bash|zsh)
    rc="$HOME/.${shell_name}rc"
    line="source \"$here/shell/codex.bash\"  # codex-switchboard"
    if ! grep -qsF "shell/codex.bash" "$rc"; then
      printf '\n%s\n' "$line" >> "$rc"
    fi
    echo "installed the codex function for $shell_name: $rc (open a new shell)"
    ;;
  *)
    echo "unknown shell '$shell_name': source shell/codex.bash (bash/zsh) or shell/codex.fish yourself"
    ;;
esac

if ! command -v codex >/dev/null 2>&1; then
  echo "note: codex is not on PATH; install codex-cli 0.154 or later first"
fi

cat <<MSG

next:
  codex-acct login work        # log a second account in (any name); repeat per account
  codex                        # the TUI, on whichever account can run right now
  codex-switchboard status     # which account the engine is on
MSG
