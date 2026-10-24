#!/bin/sh
# Replaces acpid's sample handler, which shuts the machine down whenever no desktop power
# manager is found. The live system does not start acpid (ACPI="no" in conf.d/autoconfig);
# elogind and xfce4-power-manager handle the power button. If acpid is started by hand
# this script keeps a stray button press from powering the PC off in the middle of an
# installation. Use the desktop menu or "sudo poweroff" to shut down.
logger -t powerbtn "ACPI power button pressed; ignored on the installer and rescue disc" 2>/dev/null || :
exit 0
