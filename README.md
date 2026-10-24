# Gentoo LiveCD Installer

This repository builds a Gentoo AMD64 live installer and contains its Qt GUI, privileged install daemon, companion apps, and disposable-VM acceptance harness. Current validation and build status is tracked in [REVIEW_FIX_STATE.md](REVIEW_FIX_STATE.md); VM success is not claimed here.

## Build and tests

Run the repository test suite with:

```sh
./scripts/test.sh
```

It runs the Python tests, PortageHub Rust tests, and HWMonux dump smoke check. If `.venv/bin/python` exists, the script uses it for pytest.

Check ISO build prerequisites without starting a build:

```sh
./scripts/build-iso.sh --check
```

Build the pinned ISO for normal use. This does not enable the test SSH access:

```sh
JOBS=2 ./scripts/build-iso.sh
```

To make a separate VM-test ISO, create a fresh key pair and use a distinct output directory:

```sh
install -d -m 0700 build/vm-control
testkey="build/vm-control/iso-test-$(date +%s)"
ssh-keygen -q -t ed25519 -N '' -f "$testkey"
GENTOO_TEST_SSH_PUBLIC_KEY="$(cat "$testkey.pub")" \
  ISO_DIST_DIR=dist/test-only JOBS=2 ./scripts/build-iso.sh
```

The public key enables temporary, key-only root SSH access in that ISO for the VM harness. The VM command examples below assume they are run in the same shell, where `$testkey` remains set; pass that private key to the harness, keep it outside the image, and remove it after testing. An ISO built with this test key is for isolated testing only and is not a production release. `ISO_DIST_DIR` is the build script's supported output-directory setting.

## Disposable VM checks

The VM harness creates a new qcow2 disk under `build/vm`, refuses to overwrite an existing disk, and attaches it as the guest's only target disk with serial `GENTOO-VM-DISPOSABLE`. It does not pass through host block devices. The SSH forward is loopback-only. Use a free host port and the private key whose public half was included in the test ISO.

Run a GUI installation with both companion apps and a same-live-session resume check:

```sh
python3 scripts/test-vm.py \
  --scenario openrc --iso dist/test-only/gentoo-installer-amd64-20261018T124555Z.iso \
  --ssh-host 127.0.0.1 --ssh-port 40222 \
  --ssh-private-key "$testkey" \
  --memory 12288 --cpus 4 --build-jobs 1 --swap-mib 4096 --companions both \
  --install-mode gui --check-resume
```

Companion dependencies can require compiling Qt/PySide. The example limits compilation to one job and allocates swap inside the disposable target disk. Leave at least 3 GiB of host memory available beyond the VM allocation and run this larger test alone. These settings describe the test configuration, not a verified minimum hardware requirement.

The resume check runs before the live VM shuts down. It reruns the daemon with `--resume` against the same manifest and disk, then checks storage identities and recorded destructive commands before the harness boots the installed disk without live media.

The opt-in GUI cancellation regression is:

```sh
python3 scripts/test-vm.py \
  --scenario systemd --iso dist/test-only/gentoo-installer-amd64-20261018T124555Z.iso \
  --ssh-host 127.0.0.1 --ssh-port 40222 \
  --ssh-private-key "$testkey" \
  --memory 6144 --companions portagehub \
  --install-mode gui --cancel-then-resume
```

It requests cancellation when stage3 starts, checks the reported cancellation and cleanup, resumes on the same guarded disk, and then performs the disk-only boot checks. UEFI LUKS can use `--scenario uefi-luks --luks-unlock external`; the harness saves a QMP screendump and polls SSH while an operator inspects the prompt and enters the test passphrase. `--scenario bios` exercises the BIOS layout. These commands describe available checks; consult [REVIEW_FIX_STATE.md](REVIEW_FIX_STATE.md) for whether a VM run has actually succeeded.

## Source entry points

- GUI application and pages: `src/gentoo_installer/app.py`, `src/gentoo_installer/pages/`
- Manifest and GUI-to-daemon client: `src/gentoo_installer/manifest.py`, `src/gentoo_installer/backend.py`
- Daemon protocol and installation flow: `src/gentoo_installerd/server.py`, `src/gentoo_installerd/install.py`
- Storage and system configuration: `src/gentoo_installerd/storage.py`, `src/gentoo_installerd/configure.py`
- ISO build inputs: `scripts/build-iso.sh`, `iso/builder-entrypoint.sh`, `iso/specs/`
- VM harness and GUI driver: `scripts/test-vm.py`, `scripts/vm-gui-install.py`
