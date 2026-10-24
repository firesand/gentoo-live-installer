First sectors (512 bytes) of image files that were partitioned with the exact commands the storage stage runs
(src/gentoo_installerd/storage.py, _erase_layout and _mark_disk_bootable), on 2 GiB sparse files:

mbr-gpt-pmbr-boot.bin    sgdisk --zap-all; sgdisk --new=1:0:+3MiB --typecode=1:EF02 ...; --new=2:0:+512MiB --typecode=2:8200 ...;
                         --new=3:0:0 --typecode=3:8300 ...; parted --script IMG disk_set pmbr_boot on
mbr-gpt-no-flag.bin      the same without the last command
mbr-dos-root-active.bin  parted --script IMG mklabel msdos; mkpart primary linux-swap 1MiB 513MiB; mkpart primary ext4 513MiB 100%;
                         set 2 boot on  (swap is partition 1, the root partition 2 is active)
mbr-dos-boot-active.bin  mklabel msdos; mkpart primary ext4 1MiB 1025MiB; mkpart primary ext4 1025MiB 100%; set 1 boot on
                         (the encrypted layout: /boot is partition 1 and is the active one)
mbr-dos-no-flag.bin      the swap + root layout without 'set 2 boot on'
mbr-dos-swap-active.bin  the swap + root layout with 'set 1 boot on' (the wrong partition)
