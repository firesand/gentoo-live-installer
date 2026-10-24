#!/bin/bash
set -eo pipefail

# Distribution profile scripts may read unset optional environment variables.
source /etc/profile
if [[ -f /tmp/envscript ]]; then
	source /tmp/envscript
fi
set -u

install -d -m 0755 /mnt/gentoo
install -d -m 0700 -o gentoo -g users /home/gentoo
chown -R gentoo:users /home/gentoo
passwd -d gentoo
passwd -l root
usermod -a -G gentoo-installer gentoo
printf 'DISPLAYMANAGER="lightdm"\n' > /etc/conf.d/display-manager
if [[ -f /etc/gentoo-installer-vm-test ]]; then
	# An impossible password permits key-only SSH without unlocking a password.
	usermod -p '*' root
fi

chmod 0755 /etc/init.d/gentoo-installerd
for command_path in \
	/usr/bin/gentoo-installer \
	/usr/bin/gentoo-installerd \
	/usr/bin/hwmonux \
	/usr/bin/portagehub \
	/usr/libexec/portagehub-helper; do
	if [[ ! -x "${command_path}" ]]; then
		echo "Required live application is missing: ${command_path}" >&2
		exit 1
	fi
done
