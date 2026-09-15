# Source this from ~/.bashrc or ~/.zshrc.
codex() {
    if ! command -v codex-switchboard >/dev/null 2>&1; then
        command codex "$@"
        return
    fi
    # Only the interactive TUI attaches to the shared engine. Anything with a non-TUI
    # subcommand goes straight to the binary, on the default account.
    case "${1:-}" in
        -V|--version|-h|--help|agents|exec|e|review|login|logout|mcp|plugin|app-server|remote-control|app|completion|update|doctor|sandbox|debug|apply|a|queue|archive|delete|migrate-rollouts|unarchive|cloud|exec-server|features|help)
            command codex "$@" ;;
        *)
            codex-switchboard ensure || return
            CODEX_HOME="$HOME/.codex-profiles/pool" command codex --remote unix:// "$@" ;;
    esac
}
