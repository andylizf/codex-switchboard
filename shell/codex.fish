# install.sh links this into ~/.config/fish/conf.d/. A `codex` function defined in your own
# config.fish loads later and would replace it, so put any extra flags of yours in here.
function codex --wraps codex
    if not command -q codex-switchboard
        command codex $argv
        return
    end
    # Only the interactive TUI attaches to the shared engine. Anything with a non-TUI
    # subcommand goes straight to the binary, on the default account.
    switch "$argv[1]"
        case -V --version -h --help agents exec e review login logout mcp plugin app-server remote-control app completion update doctor sandbox debug apply a queue archive delete migrate-rollouts unarchive cloud exec-server features help
            command codex $argv
        case '*'
            codex-switchboard ensure; or return
            env CODEX_HOME=$HOME/.codex-profiles/pool codex --remote unix:// $argv
    end
end
