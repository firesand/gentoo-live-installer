"""Console installer: ``gentoo-installer-text``.

For computers whose graphics do not work with the live kernel and for serial consoles. Numbered
menus and plain prompts, standard library only (no Qt, no curses), usable at 80x24 and over a
serial line. It collects the same manifest as the graphical installer and hands it to the
installer daemon (``gentoo-installerd``) through its socket; see docs/protocol.md.

Modules: ``console`` (terminal input and output), ``host`` (the programs it runs on the live
system and the system files it reads), ``daemon`` (the socket), ``inventory`` (the daemon's
``inspect`` reply), ``check`` (answers checked with the manifest's own rules), ``wizard`` (the
questions), ``summary`` (what is shown before the start), ``follow`` (progress, reconnecting),
``conclude`` (restart, resume, the log) and ``cli`` (the command line and the exit codes).
"""

from __future__ import annotations

# Exit codes of gentoo-installer-text (contract I5).
EXIT_OK = 0
EXIT_FAILED = 1  # the installation failed or was cancelled
EXIT_USAGE = 2  # wrong options or an invalid manifest
EXIT_REFUSED = 3  # the typed confirmation is missing or wrong
EXIT_NO_DAEMON = 4  # the installer service cannot be reached
EXIT_INTERRUPTED = 130  # Ctrl-C (confirmed), or the input ended
