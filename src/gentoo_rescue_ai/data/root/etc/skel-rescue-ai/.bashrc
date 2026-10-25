# Written by gentoo-rescue-ai at every start. Claude Code always runs with user settings only: a .claude/settings.json found in a
# project folder (the broken disk!) could otherwise set environment variables, an API helper or a status line.
claude() {
    case "$(pwd -P)" in
        /mnt|/mnt/*) echo "claude: not started inside the broken disk (/mnt). cd ~ first; the agent reads the disk through gentoo-rescue." >&2; return 1 ;;
    esac
    command claude --setting-sources user "$@"
}
