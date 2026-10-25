"""ACCT: the sentences of the account choices, one source for the graphical and the text installer.

Both installers import these strings, so a newcomer reads the same words wherever they install from.
"""

from __future__ import annotations

MIN_PASSWORD_LENGTH = 8  # the hard minimum (manifest validation); the hint below asks for more

ROOT_TITLE = "How should the administrator (root) account work?"
ROOT_HELP = ("Root is the all-powerful account. Turning its login off is safer; you still get administrator rights with sudo. "
             "If you ever lock yourself out, start this installer's Repair tool.")
# key -> (short label, detail). The keys are the text installer's own names for accounts.root_mode: off = locked.
ROOT_CHOICES: dict[str, tuple[str, str]] = {
    "off": ("Turn root login off (recommended; you use {tool})", "You administer the system with {tool} from your own account."),
    "same": ("Use the same password for root", "One password for both accounts: simple, but anyone who learns it controls everything."),
    "separate": ("Choose a separate password for root", "Root keeps its own password, different from yours."),
}
ROOT_MODE_OF_CHOICE = {"off": "locked", "same": "same", "separate": "separate"}  # to accounts.root_mode
CHOICE_OF_ROOT_MODE = {mode: key for key, mode in ROOT_MODE_OF_CHOICE.items()}

TOOL_NAMES = {"sudo": "sudo", "doas": "doas", "run0": "run0", "su": "su"}
TOOL_HELP = {
    "sudo": "Lets your user run administrator commands by typing sudo before them.",
    "doas": "A small alternative to sudo: type doas before an administrator command.",
    "run0": "systemd's own way to run administrator commands: type run0 before them. Needs systemd.",
    "su": "Becomes root with root's own password. Needs a root password, so root login cannot be turned off.",
}

PASSWORD_HINT = "Use a long phrase, 16 characters or more, different from your other passwords."
PASSWORD_TOO_SHORT = f"At least {MIN_PASSWORD_LENGTH} characters are needed."
PASSWORD_SAME_AS_ROOT = "Your password and the root password are the same. If one is guessed, both accounts are open: choose different ones."
PASSWORDS_DIFFER = "The two passwords do not match."
HOSTNAME_HELP = "The name of this computer on your network, for example tux. Letters, digits and hyphens only."
HOSTNAME_PLACEHOLDER = "tux"
USERNAME_HELP = "The name you log in with. Lowercase letters, digits, - and _ ; it must start with a letter."

DOMAIN_HELP = "Optional. Leave empty at home. A company or school network may give you one, for example example.org."
HOSTS_HELP = "Lets this computer find others by name without a DNS server. One line each: an address, then one or more names."
LOCALE_HELP = "Locales decide the language, date and number formats."
LOCALE_COMPAT_NOTE = "English (en_US.UTF-8) is also installed for compatibility."
LEGACY_CHARSET_WARNING = "Many programs need UTF-8. Older character sets are for special cases only."
TIMEZONE_HELP = "Sets the clock of your computer to your part of the world."
TIMEZONE_FALLBACK = "Your time zone could not be detected, so UTC is set for now. Choose your region and city."
ETC_SIGN_WARNING = "Etc/GMT zones count the other way round: Etc/GMT+5 is five hours BEHIND UTC. Pick a region and city unless you need this."
RTC_LOCAL_TEXT = "Hardware clock keeps local time (choose this if Windows shares this computer)"
CONSOLE_FONT_HELP = "Makes the text screen bigger and easier to read on high-resolution displays."
CONSOLE_FONTS = (("", "Default"), ("ter-v16n", "Terminus 16"), ("ter-v24n", "Terminus 24"), ("ter-v32n", "Terminus 32 (large)"))

SSH_TEXT = "Allow remote login over SSH"
SSH_HELP = "Lets you log in from another computer. Root cannot log in remotely; use your own account."
SSH_KEYS_ONLY_TEXT = "Keys only: do not allow logging in with a password"
SSH_KEYS_ONLY_HELP = "Safer on a network you do not trust. You must add at least one public key above."

LOGGER_HELP = "Writes a record of what the system does to files in /var/log. Useful when something goes wrong."
CRON_HELP = "Runs scheduled jobs, for example log rotation and the weekly disk TRIM."
CRON_SYSTEMD_HELP = "systemd uses timers instead; you only need a cron daemon for programs that expect one."
JOURNAL_NOTE = "systemd keeps logs itself; read them with journalctl."
JOURNAL_PERSISTENT_TEXT = "Keep logs across reboots"
JOURNAL_PERSISTENT_HELP = "Without it the logs vanish at every restart, which makes finding a problem hard."
TIMESYNC_TEXT = "Keep the clock correct automatically"
COMPLETION_HELP = "Press Tab in the terminal to finish commands."
GENTOOLKIT_HELP = "Includes eclean-dist to free disk space."
MLOCATE_HELP = "Lets you find any file by name in a moment with the locate command."


def root_choice_text(key: str, tool: str = "sudo") -> tuple[str, str]:
    """The label and detail of a root choice, naming the administrator tool the user picked."""
    label, detail = ROOT_CHOICES[key]
    return label.format(tool=tool), detail.format(tool=tool)


def password_hint(password: str, root_password: str = "") -> tuple[bool, str]:
    """(acceptable, one line) for the password boxes: a hint while typing, never a pop-up."""
    if not password:
        return False, PASSWORD_HINT
    if len(password) < MIN_PASSWORD_LENGTH:
        return False, PASSWORD_TOO_SHORT
    if root_password and password == root_password:
        return True, PASSWORD_SAME_AS_ROOT
    return True, "Good." if len(password) >= 16 else PASSWORD_HINT
