"""BOOT: the kernel stage."""

from __future__ import annotations

from ..configure_boot import INITRAMFS_IMAGES, KERNEL_IMAGES
from ..runner import RunnerError
from .context import InstallError
from .storage import StorageStages


class KernelStage(StorageStages):
    def _kernel(self) -> None:
        plan = self._plan()
        self.configurer.notify = self.log
        root = self._root_device()
        stack = self._storage_stack()
        if stack.unsupported:
            raise InstallError(stack.unsupported)
        self.configurer.configure_initramfs(self.manifest, root, self.storage._stack.get("luks_device"), plan, resume_device=self._resume_device(), stack=stack)
        kernel = self.manifest.system.kernel
        self.configurer.write_boot_portage_files(self.manifest, plan)  # dist-kernel USE, kernel config lines, Secure Boot signing, README
        self.configurer.prepare_secure_boot(self.manifest)  # the signing key must exist before the kernel image is built and signed
        self.configurer.configure_serial_getty(self.manifest)
        # Dracut needs the target's storage utilities when it builds the image.
        # Installing them in the later packages stage would produce an unusable
        # encrypted/LVM/RAID root image (or fail generation altogether). Firmware
        # packages also supply microcode and dracut configuration (with a distribution
        # kernel they come with the kernel itself, see kernel_package_atoms), and the filesystem
        # userland lets the installed system check its own partitions; later --noreplace skips them.
        self.configurer.prepare_stack_packages(stack)
        batch = self.configurer.kernel_stage_atoms(self.manifest, self.storage.layout, plan, stack)
        if batch:
            self.configurer.emerge(self.manifest, ("--noreplace", *batch))
        self.configurer.finish_stack(self.manifest, stack)
        # systemd (systemd-utils on OpenRC) is already installed in its stage3, so --noreplace would keep the
        # build without the flags the installer wrote: cryptsetup for the initramfs, boot and ukify for
        # systemd-boot and a Unified Kernel Image. One rebuild, and a stop right here if a tool is still missing.
        rebuild = self.configurer.flag_rebuild(self.manifest, plan)
        if rebuild is not None:
            atom, expected = rebuild
            self.configurer.emerge(self.manifest, ("--update", "--newuse", "--oneshot", atom))
            for relative, message in expected:
                if not self.dry_run and not self.configurer.path(relative).exists():
                    raise InstallError(message)
        if kernel == "manual":
            self._manual_kernel(plan)
            return
        self.configurer.emerge(self.manifest, ("--noreplace", *self.configurer.kernel_package_atoms(self.manifest, plan)))
        # Files and out-of-tree drivers from the hardware plan must exist before the initramfs is built.
        self.configurer.write_plan_files(plan)
        driver_atoms = [str(atom) for atom in getattr(plan, "driver_atoms", None) or []]
        if driver_atoms:
            try:
                self.configurer.emerge(self.manifest, ("--noreplace", *driver_atoms))
            except RunnerError as exc:
                self.degraded.append(f"Hardware drivers ({', '.join(driver_atoms)}) could not be installed: {self.configurer.explain(exc).summary[:200]}")
                self.log("WARNING: " + self.degraded[-1])
        if self.manifest.system.initramfs == "dracut":
            self.configurer.emerge(self.manifest, ("--noreplace", "sys-kernel/dracut"))
            self.configurer.chroot(("dracut", "--force", "--regenerate-all"))

    def _manual_kernel(self, plan: object | None) -> None:
        """Everything of this stage but the kernel, so the person's chroot has the tools; then stop until the kernel is there.

        "Manual kernel" is a command-line option: the graphical installer cannot pause for it. A resumed install comes through here
        again, and --noreplace skips what the first run installed.
        """
        self.configurer.emerge(self.manifest, ("--noreplace", *self.configurer.kernel_atoms(self.manifest)))
        self.configurer.write_plan_files(plan)
        if self.resume and self._verify_kernel():
            self.log("Verified manually installed kernel and initramfs")
            return
        raise InstallError(self._manual_kernel_steps())

    def _manual_kernel_steps(self) -> str:
        """What the person has to do. The fstab comes in the next stage, so the devices are named here, with the options and the names
        of the installer's own mounts: a subvolume is only found with them, and the resume looks for this mapper name."""
        mounts = [f"{entry.device} at {self.target_root}{entry.mountpoint.rstrip('/')}" + (f" with -o {entry.options}" if entry.options else "")
                  for entry in sorted(self.storage.layout, key=lambda entry: len(entry.mountpoint)) if entry.mountpoint.startswith("/")]
        stack = self.storage._stack
        volumes = ([f"cryptsetup open {stack['luks_device']} {stack['mapper']}"] if "mapper" in stack else []) + ([f"vgchange -ay {stack['vg']}"] if "vg" in stack else [])
        opened = f"run '{' and '.join(volumes)}', then " if volumes else ""
        images = "the kernel as /boot/vmlinuz-<version>" + ("" if self.manifest.system.initramfs == "none" else " and its initramfs as /boot/initramfs-<version>.img")
        return ("Manual kernel selected: everything but the kernel is installed, and the installer stops here so that you can install it. "
                f"When the cleanup has finished, {opened}mount {', then '.join(mounts)}. Mount /proc, /sys, /dev and /run below it as the Handbook says, "
                f"and chroot into {self.target_root}. Install {images}. "
                f"Leave the chroot, run 'umount -R {self.target_root}', then run the installer again with the same options and --resume.")

    def _verify_kernel(self) -> bool:
        boot = self.configurer.path("boot")
        kernel = any(p.is_file() and p.stat().st_size for pattern in KERNEL_IMAGES for p in boot.glob(pattern))
        initramfs = self.manifest.system.initramfs == "none" or any(p.is_file() and p.stat().st_size for pattern in INITRAMFS_IMAGES for p in boot.glob(pattern))
        return bool(kernel and initramfs)
