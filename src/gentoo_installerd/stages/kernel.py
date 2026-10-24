"""BOOT: the kernel stage."""

from __future__ import annotations

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
        if kernel == "manual":
            # "Manual kernel" is a command-line option: the graphical installer cannot pause for it.
            if self.resume and self._verify_kernel():
                self.log("Verified manually installed kernel and initramfs")
                return
            raise InstallError(f"Manual kernel selected. After cleanup, remount the saved target filesystems at {self.target_root}, chroot into that directory, install the kernel and initramfs under /boot, then resume")
        # Dracut needs the target's storage utilities when it builds the image.
        # Installing them in the later packages stage would produce an unusable
        # encrypted/LVM/RAID root image (or fail generation altogether). Firmware
        # packages also supply microcode and dracut configuration, and the filesystem
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
        self.configurer.emerge(self.manifest, ("--noreplace", "sys-kernel/installkernel", f"sys-kernel/{kernel}"))
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

    def _verify_kernel(self) -> bool:
        boot = self.configurer.path("boot")
        kernel = any(p.is_file() and p.stat().st_size for pattern in ("vmlinuz-*", "kernel-*") for p in boot.glob(pattern))
        initramfs = self.manifest.system.initramfs == "none" or any(p.is_file() and p.stat().st_size for pattern in ("initramfs*", "initrd*") for p in boot.glob(pattern))
        return bool(kernel and initramfs)
