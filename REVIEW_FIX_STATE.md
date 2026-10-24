# Installer repair and VM validation

User authorization: fix all reviewed issues, use Astra/Sol/Luna, preserve ongoing
work, and test end to end in a VM. Never use host block devices as installer targets.

## Current checkpoint

- Final current-workspace checks: 249 Python tests passed in
  build/r19-final-consolidated-tests-20261024T012630Z.log. That combined run
  then exposed a timing-dependent PortageHub cancellation test. It was
  replaced with a bounded ready-signaling process fixture; the final Rust
  suite passes all 65 tests in build/r19-rust-final-after-cancel-order-fix.log,
  and HWMonux --dump exits0 (build/r19-hwmonux-dump-20261024T013116Z.log).
  Only cfg(test) code and the removed integration test differ from frozen
  PortageHub r19; production bytes remain identical. Before copies, diffs,
  repeated cancellation checks and hashes are in
  build/r19-cancel-test-only-delta.json. The frozen ISO payload is unchanged.
- THIRD FULL r19 PASS: BIOS run build/vm/bios-20261024T010257Z-qal0_iib
  reports success=true and all9 probes pass. Static NetworkManager profile,
  address10.0.2.15/24, gateway10.0.2.2, DNS10.0.2.3 and DNS lookup passed.
  Completed resume skipped storage with no destructive commands/identity
  mismatches. Harness/tool exited0 and owned VM cleaned. Actual live QMP
  safety receipt exists; target is evidenced by argv log+successful probes,
  target QMP runtime receipt not captured before fast cleanup.
- FINAL required r19 OpenRC run ACTIVE:
  build/vm/openrc-20261024T011132Z-ud3rktx1, harness152924, exec82174,
  port22226,12GiB4CPU, both companions, GUI/completed resume/keep.
  One build job and4096MiB target swap via actual GUI manifest; no guestpatch.
  Launched alone at01:11:32 UTC with15,996,952KiB host MemAvailable,
  exceeding15,728,640KiB (12GiBguest+3GiBreserve). Published ISO rehashed.
  Launcher build/run-r19-openrc.py; command+gate build/r19-openrc-command.json;
  stdout build/r19-openrc-run.log. Root owns runner/input; Astra read-only
  QMP safety receipts and acceptance-generator preparation. No other VM launch.

### Earlier r19 checkpoint (historical)

- SECOND FULL r19 PASS: systemd run
  build/vm/systemd-20261024T004407Z-6ntw6eds reports success=true, all8 probes
  pass including PortageHub and HWMonux absence. Intentional GUI cancellation
  cleaned all target mounts/swap/mappings; resume skipped storage with no
  destructive commands or identity mismatches. Disk-only boot evidenced by
  qemu-target.log and actual SSH probes. Live QMP storage receipt exists;
  target QMP receipt was not captured before cleanup. Harness exited0.
- BIOS run active: build/vm/bios-20261024T010257Z-qal0_iib, runner137341,
  QEMU137349, exec37226, port22229,4GiB2CPU/no companions/GUI/completed resume/
  static network. Actual live QMP receipt saved; keymap Enter sent only after
  observing boot-observation.png. Root OpenRC full companion case remains
  queued for sufficient memory after smaller VMs; planned12GiB/onejob/4GiBswap.
- LUKS harness logged PASS and wrapper printed Exit0, but tool transport
  subsequently reported143. Cause unknown, preserved honestly in
  build/r19-luks-exit-status-discrepancy.json; actual successful probes and
  owned-QEMU cleanup independently verified, no successful run repeated.

### Earlier r19 checkpoint (historical)

- FIRST FULL r19 PASS: UEFI-LUKS run
  build/vm/uefi-luks-20261024T004326Z-9zpc85z_ reports success=true and all8
  target probes pass (init/hostname/user/network/DNS/Portage/both disabled
  companions absent). GUI install and completed resume passed; resume had
  storage_skipped=true, no destructive commands, no identity mismatches.
  Root independently viewed actual LUKS prompt (luks-root-observation.json).
  Actual target QMP receipt confirms disk-only boot and regular-file media.
- Initial host QMP passphrase helper sent all events in one batch; guest
  received only8 chars. Separate Enter returned fresh prompt. Luna fixed host
  scripts/vm_qmp.py to send key down/up pairs with50ms pacing and Enter last;
  exact fixed test passphrase then unlocked successfully. No guest/product/ISO
  patches. QMP3 tests and all76 VM tests pass. Failed-input screenshots and
  successful retry receipts retained. Test helper is not installed ISO payload.
- Systemd cancel/resume run continues at final network packages; PortageHub
  compiled/installed. Astra authorized next BIOS4GiB2CPU/no companions/GUI/
  completed resume/static networking only after LUKS cleanup and fresh memory
  gate reserving full remaining systemd6GiB allocation. Maximum2VMs.
- Normal ISO verifier prepared and actually checked accepted test ISO:
  build/r19-accepted-test-iso-components.json validates113 Portage CONTENTS
  hashes,26 exact Python modules,3 packaging files and10 companion paths.
  No wheel RECORD survives Portage installation. Normal build still blocked
  until all4 fresh r19 VM cases pass; no acceptance JSON yet.

### Earlier r19 checkpoint (historical)

- r19 test build exited0; published ISO equals staged SHA512 below. Completed
  stage1 archive receipt: build/r19-stage1-receipt.json. All4 acceptance cases
  remain pending. LUKS4GiB and systemd6GiB VMs now active; systemd run
  build/vm/systemd-20261024T004407Z-6ntw6eds / exec55313 / port22228 uses
  GUI cancellation/resume and PortageHub only. Both boot exact r19 bytes.
- Full-companion retry resource plan revised explicitly after read-only
  diagnosis: 12GiB RAM, one build job, 4GiB target swap, run alone with
  >=15GiB host MemAvailable. Host harness now exposes --build-jobs/--swap-mib;
  76 VM tests pass. This does not alter installed r19 product files or frozen
  build inputs (59 files reverified unchanged). Diff and receipt:
  build/r19-host-resource-controls.{diff,json}. No guest patches or host
  memory/process changes. User need not stop unrelated work for smaller tests.
- Luna strengthens normal-build guard to hash-bind actual recorded live and
  target QEMU commands, prove real booted ISO bytes equal r19, and reject any
  ISO on target boot. Acceptance metadata alone previously could relabel an
  old successful run; none has been accepted. No acceptance JSON exists yet.

### Prepublication checkpoint (historical)

- r19 ISO assembly completed 00:41 UTC; final stage2 archive compression is
  still active. Verified staged image build/r19-staged-test.iso has SHA512
  be1973fd1788c862e86a3280bdbd01ca2842b397b3cdbdf36dd3bbe6c64631637b6cf0fd7ebd11a918b1f5fde67cdb2c3d61fe4a91e6d09372936992ba092886.
  Publication must match before acceptance; Astra owns final receipt checks.
- Fresh r19 LUKS VM active: build/vm/uefi-luks-20261024T004326Z-9zpc85z_,
  runner77083/QEMU77096/exec84691, 4GiB2CPU/port22227/GUI/no companions/
  completed-resume/external unlock. Real boot prompt still required.
- OpenRC16GiB test waits for >=19GiB host MemAvailable. At 00:43 UTC only
  ~15GiB was available; unrelated Kyty uses ~14GiB and remains untouched.
  User informed asynchronously. Sol may launch systemd6GiB only if headroom
  after reserving all LUKS configured RAM remains >=9GiB. Maximum2VMs.
  r18 Ninja was bounded -j2 and target build directory was disk-backed;
  likely compiler working-set pressure, not an identified tmpfs/product bug.

### Immediately preceding checkpoint (historical)

- PRIMARY r18 OpenRC test STOPPED unsuccessfully: PySide stalled at 63/107,
  guest SSH banner timed out and QMP measured repeated target reads ~2829 MiB/s
  with no writes. Guest memory pressure suspected, not directly confirmed.
  Root verified owned runner 4104834 and sent SIGINT; exec 45180 exited130;
  QEMU 4104839 gone. Disk/logs/screens preserved; result.success=false,
  resume/probes never ran. See interruption-receipt.json and diagnostic files
  in build/vm/openrc-20261023T234927Z-rr1rt6kx.
- ALL FOUR acceptance runs must now be fresh r19 ISO runs. No mixed r18 reuse.
  Active build/run-r19-normal-stage2.py is strict same-ISO; previous mixed guard
  preserved as run-r19-normal-stage2-mixed-provenance-historical.py. Next OpenRC
  will use16 GiB RAM; LUKS4 GiB; max2 VMs and fresh resource gates. All test
  targets remain newly created qcow2 files, with no real disk access.
- r19 build continues (exec93857 / b9a35664af6a), now stage2 SquashFS. Astra
  owns guarded staging watcher exec87527 and will report verified ISO readiness.
  All26 installer Python files actually installed in r4 match frozen r19:
  build/r19-installed-installer-verification.json. No VM currently running.

### r19 build checkpoint (historical)

- r19 replacement test ISO BUILD ACTIVE: exec 93857, isolated builder
  b9a35664af6a, 4 CPUs / 8 GiB / no extra swap / no host devices. Frozen
  source build/validated-source-20261024-r19 (160 files); 12 changed paths
  recorded in build/r18-r19-source-difference.json. Product delta is encryption
  only plus exact installer r3-to-r4 packaging revision. Full Python suite:
  244 tests + 37 subtests PASS (build/python-r19-final-tests.log). Native
  systemd transaction and full 167-package resolver pass; pinned parser probe
  build/dracut-luks-parser-probe-r2.json passes both init modes + negative controls.
  Builder log: build/validated-source-20261024-r19-reseed-iso.log.
- r19 enables systemd[cryptsetup], explicitly rebuilds installed systemd before
  kernel/dracut and checks helper presence, and supplies rd.luks.uuid plus name.
  r18 OpenRC still active at PySide package 130/131, exec 45180. Do not stop it.

### r18 encrypted-boot failure checkpoint (historical)

- r18 UEFI-LUKS GUI installation and completed resume PASSED, but disk-only
  boot FAILED to offer a passphrase prompt: initramfs omitted systemd-cryptsetup
  because the systemd stage3 lacks USE=cryptsetup. Evidence retained in
  build/vm/uefi-luks-20261024T000723Z-brefq9sr, especially luks-missing-prompt.png,
  resume-result.json and result.json (success=false). Owned runner was interrupted
  cleanly and its QEMU removed; no passphrase or guest repair was applied.
- Sol implements narrowly scoped encrypted-systemd early dependency fix; Luna
  independently reviews ordering and initramfs command-line support. Astra
  prepares r19 helpers, without launching until source fix is reviewed/frozen.
  r18 OpenRC continues compiling companions and remains untouched. Its evidence
  may be reused only with explicit per-run ISO hashes and independently proven
  unencrypted OpenRC configuration/command equivalence; never call it an r19 run.
- Original WIP backup reverified: all 150 archived files match sha256.json.
  Receipt: build/original-wip-backup-verification.json.

### Prior r18 checkpoint (historical)

- r18 test ISO built and verified; no full installation/disk-only boot has passed yet.
- OpenRC full GUI installation ACTIVE: build/vm/openrc-20261023T234927Z-rr1rt6kx,
  exec 45180, QEMU 4104839, SSH 22226, 6 GiB / 4 CPU. Kernel + initramfs and
  target identity/accounts/network passed. Applications are installing (131
  packages, 113 binaries); both companions selected. Completed resume and
  disk-only boot remain pending.
- UEFI-LUKS full GUI installation ACTIVE: build/vm/uefi-luks-20261024T000723Z-brefq9sr,
  exec 39407, QEMU 4138167, SSH 22227, 4 GiB / 2 CPU. Launched only after
  fresh available-memory check exceeded 7 GiB. No companions; completed resume,
  observed LUKS unlock and disk-only boot required. Astra owns this VM.
- Final harness supports explicit companion subsets and manifest-derived
  runtime/absence probes, checks BIOS static NetworkManager configuration, and
  handles metadata failures without skipping VM cleanup or reporting PASS.
  Focused suite: 71 tests passed (build/r18-harness-final-vm-tests-venv.log).
  Product sources and built ISO remain frozen r18.
- Remaining matrix: systemd with PortageHub + GUI cancel/resume, BIOS without
  companions + static network/DNS + completed resume. Every scenario requires
  actual disk-only boot. HWMonux/PySide runtime coverage is OpenRC only.
- Normal ISO build is guarded by all four actual acceptance results and has
  not started. Normal image content checks and actual live GUI smoke remain.
- All VM targets are fresh regular qcow2 files. No real disks or host directory
  shares are attached; original WIP and failed-run evidence are preserved.

### r18 earlier checkpoints (historical)

- r18 OPENRC VM passed kernel + initramfs and target identity/accounts/network
  stages. Actual installkernel[dracut] binary merged; previous blocker cleared.
  Now packages stage: 131 packages, 113 binaries; Qt/PySide source builds pending.
  Active run remains build/vm/openrc-20261023T234927Z-rr1rt6kx / exec45180.
- Planned efficient acceptance matrix (Astra independently reviewed): OpenRC
  both companions + completed resume; systemd PortageHub + GUI cancel/resume;
  UEFI-LUKS no companions + completed resume + observed external unlock;
  BIOS no companions + completed resume + static network/DNS. Every case must
  actually boot from disk without ISO. HWMonux/PySide runtime coverage will be
  OpenRC only; systemd full companion native dependency plans already passed.
  Luna implements explicit --companions and manifest-derived probes; Astra
  strengthens task-local acceptance guards. Product sources remain frozen r18.

- r18 BUILD COMPLETE: exec12551 exit0, builder d40bf3522422 removed.
  Published dist/test/gentoo-installer-amd64-20261018T124555Z.iso exactly
  matches staged copy and manifest SHA512:
  2f328c31b4179630ef5343ec8e61a041e0af1bf8bc5893696d41fd40f1d1d58825f6fc7d69a04d6075c8500b90e30a01ab7a3b4ceed63a2efd5112ce222f75c8
  Installed r3 source hashes verified; build/r18-stage1-receipt.json saved.
  Active OpenRC VM passed GUI preflight/storage and downloaded stage3.
  Keymap default Enter was sent only after observing live-boot-check.png;
  input receipt saved in its run directory. No full install acceptance yet.

- r18 FULL OPENRC GUI VM ACTIVE: exec45180, run
  build/vm/openrc-20261023T234927Z-rr1rt6kx, SSH22226. Fresh40G qcow2,
  6GiB/4CPU, virtio graphics, actual KVM confirmed. Uses verified completed
  build/r18-staged-test.iso; SHA512 starts2f328c31b417. Publication must match
  before acceptance. GUI install, completed-resume and disk-only boot pending.

- r18 BUILD ACTIVE: exec12551, owned container d40bf3522422; source freeze
  build/validated-source-20261024-r18 (160 files, 13 changed paths versus r17).
  Isolation receipt build/iso-r18-isolation.json; build log
  build/validated-source-20261024-r18-reseed-iso.log. Guarded helper preserves
  r17 ISO directory and verified completed stage1 before modifying task cache.

- r18 READY TO FREEZE: native chroot exact kernel and full source-only
  dependency plans pass for authentic OpenRC (172 packages), systemd (165),
  and UEFI-LUKS systemd (167 including cryptsetup) stage3s. Logs:
  build/native-kernel-{openrc-resolver-fixed,systemd-resolver,uefi-luks-resolver}.log.
  Original failed plan remains preserved. Systemd seed signature/hash evidence:
  build/native-systemd-seed-fetch.log.
- Source fixes since r17: installkernel[dracut], narrow companion/NetworkManager
  USE dependencies, LVM tools enabled when selected, truthful failure UI after
  cleanup, underlying VM backend error reporting, early validation against
  unsupported automatic-kernel/initramfs combinations, matching UI guidance,
  and installer ebuild r3. Manual kernel workflow remains available.
- Final Python suite: 227 tests + 31 subtests PASS, build/python-r18-freeze-tests.log.
  Rust source unchanged since 65 passing tests plus actual bind-mount regression.
  No full installation or disk-only boot has passed yet.
- VM graphics probe completed safely: build/vm/virtio-vga-r17-5bhelqtf,
  owned QEMU stopped, readable 1280x800 early console and virtio GPU handoff.
  Common VM command explicitly uses -vga virtio, including normal smoke.
  LUKS target unlock prompt still requires actual acceptance.

### Earlier checkpoints (chronological evidence; may describe completed runs)

- Additional actual r17 failure UI issue: gui_result correctlyfalse/rebootfalse,
  but saved gui-result.png ends'Finishing and unmounting/100%/Cleanup completed'
  because cleanup events overwrite ProgressPage._fail state. Luna fixing
  progress.py + error-cleanup-completedfalse regression; harness diagnostics
  will surfaceunderlyingbackenderror ratherthangenericGUI-ready error.
  Include these inr18; helper allowlist updated, notfrozenorlaunched yet.

- r17 FIRST FULL VM FAILED at kernel dependency: sys-kernel/installkernel
  lacks dracut USE needed by gentoo-kernel-bin[initramfs]. Run
  build/vm/openrc-20261023T231212Z-78spdj58, exec91431 exited1; ownedVM
  cleaned, disk/screens/daemonlogs retained. ActualGUI reports failure,
  rebootdisabled, cleanup completed/unmountedtarget/swapoff. Stage3 signature,
  extraction, repository, Portage, firmware stages progressed successfully.
  Sol fixes installkernel USE+regression+actual native chroot resolver, r3
  revision. Astra prepares r18 reseed and preserves r17. Earlier --root=/target
  resolver probes hid missingtargetUSE by resolving kernel BDEPEND on builder;
  require nativechroot stage-specificpretend now. No disk-onlyboot acceptance.
- r17 publishedISO fullyverified equalstaged SHA512
  386070d975f837710c35e5b5891532a51e9af65a9028389383f1a34b02d88f1dcc0dc9891e3e1d5a2b96d8a5d2f3de3cd6c18bcc07fa4201fbee41e628a8056b
  Builder12321 exited0, container aaff41cc0dc6 removed; receipt
  build/r17-stage1-receipt.json capturesarchive+source+ISOhash. Normalhelpers
  r17 prepared but mustnotrun: all4VM acceptancepending.

- FIRST FULL GUI INSTALL ACTIVE r17 OpenRC/UEFI: exec91431, run
  build/vm/openrc-20261023T231212Z-78spdj58, SSH22226, staged r17 ISO.
  Fresh40Gqcow2,6GiB4CPU, GUI wizard + completed-resume check + disk-onlyboot.
  r17 LIVE SMOKE PASS: build/vm/openrc-20261023T230917Z-95dp9_21;
  its VM cleaned. Actual screenshot viewed: Continue fully visible, window
 1180x725 at(5,56) within1280x800; DNS/nonrootdaemon/servicesPASS. Early
  defaultQEMU-VGA kernelconsole garbled beforeXorg; LUKS prompt readability
  remains toverify. Builder finishingpublish; staged/publishedhash comparison
  and stage1receipt assignedAstra. No full installation acceptance yet.

- r17 live smoke ACTIVE (no installation): exec84990, ownedhelper3806792,
  QEMU3806823, run build/vm/openrc-20261023T230917Z-95dp9_21, SSH22226.
  Readonly build/r17-staged-test.iso is verified exact assembledISO copy;
  receipt build/r17-staged-test-receipt.json SHA512 starts386070d975f8.
  Builder aaff41cc0dc6 still finishing stage2gzip capture; publishedISOhash
  mustmatchstaged beforeacceptance. Fresh40Gqcow2only,6GiB4CPU,nohostdevices.
  Smoke waits actualinstallerwindowfit, DNS, nonrootdaemon thencleansVM.

- r17 build ACTIVE: execsession12321, ownedcontainer aaff41cc0dc6; isolation
  verified4CPU/8GiB/noextra swap/nohostdevices/sourceRO, receipt
  build/iso-r17-isolation.json. Frozen160files; exact14pathdelta in
  build/r16-r17-source-difference.json. Guardedrun-r17-reseed.py preserved
  r16testoutput in dist/test-r16 and completedarchive as r16-completed.tar.gz.
  Log build/validated-source-20261024-r17-reseed-iso.log. ActualVM tests pending.

- r17 source ready: live DNS/LightDM/XDM fixes; app initial-height clamp;
  NetworkManager static Ethernet profile now generated/actual libnm verified;
  installer revision r2 recognized as upgrade from actualr16VDB. Full Python
  suite211tests+28subtests PASS (build/python-r17-final-tests.log); existing
  Rust65+realnamespace regression remains unchanged/pass. VM harness now checks
  installed DNS and uses static Ethernet in BIOS scenario, othersDHCP; allwizard
  roundtripsPASS. Root freezing r17 then guardedarchive-reseed build. Prior r16
  as-shipped liveVMFAILED preserved, no install or disk-only acceptance yet.

- r16 first live VM finished FAILED as shipped, diagnostics complete; owned
  smokehelper3751070 and QEMU3751075 both exited. Run evidence:
  build/vm/openrc-20261023T224628Z-n7mjjbxc. Guest-only fixes confirmed:
  NetworkManager rc-manager=file restores real DNS; LightDM explicit Gentoo
  Xsession wrapper starts XFCE and installer; xdm return0 permits autoconfig.
  Source ISO fixes applied, r16 ISO remains unaccepted. Actual screenshot also
  shows bottom Continue clipped at1280x800, so Luna fixes initial app geometry;
  Sol bumps installer to r2. Astra prepares guarded r17 archive reseed, preserving
  r16 completed stage1/testISO before writes. No installation attempted yet.

- FIRSTVM RUNNING r16 live smoke: build/vm/openrc-20261023T224628Z-n7mjjbxc
  SSHroot127.0.0.1:22226, execsession92530. Ownedsmokehelper PID3751070
  PAUSED(SIGSTOP) for diagnostics; QEMU3751075 remainsrunning. Root MUST
  SIGCONT helper afterdiagnostics so it capturesfailure/cleansownedVM.
  Boot/SSH/nonrootdaemonaccessPASS; GUIautologin authenticates thenlauncher
  exits1. Luna inspectingLightDMwrapper. ActualDNSFAILED missingresolv.conf
  despiteDHCPDNS10.0.2.3. Guest-only candidate rc-manager=file + nmclireload
  PASSED realDNSlookup (dns-guest-candidate-fix.log). Thisguestpatch is
  diagnostic only; r16ISO notaccepted. SolsourceNM/specfixes readyforr17.

- r16 ISO assembled successfully at22:42:28UTC: 1455916sectors (~2.78GiB).
  Catalyst stage2 capturetar active; publish/checksums/manifest pending.
  Known probable liveDNS issue to verify in VM: NM rc-manager=unmanaged
  while resolv.conf isremoved and nodhcp skips livecd-tools DHCP. Smoke
  helper now captures resolver/lease state and requires actualDNSlookup.
  No VM has launched yet.

- r16 stage2 kernel/bootloader/fsscript/rcupdate PASSED. Actual dracut
  uses arch/x86/boot/bzImage; lsinitrd contains dmsquash-live(-ntfs),
  fusermount, mount.fuse, ntfs-3g. Squashfs compression running since
  22:34:29UTC. Installer daemon, NM, xdm and testsshd runlevels present.
  Stale udev-mount rcadd warning is redundant (devfs/udev/udev-trigger
  already in sysinit), not a boot blocker. VM acceptance stillpending.

- r16 RUNNING: container 5e4815ab0fe8, execsession41981; frozen159files,
  sourceguards passed. Exact FUSE2 version atom passes Catalystparser and
  Portageresolver. Log build/validated-source-20261024-r16-resumed-iso.log;
  isolationreceipt build/iso-r16-isolation.json. Await stage1 delta/repack,
  stage2 boot image and actual VM test.

- r15 stopped at spec parse before stage1 mutation: Catalyst treats slot
  colon in sys-fs/fuse:0 as a spec key. Sol replaces it with exact pinned
  =sys-fs/fuse-2.9.9-r2 and checks actual Catalyst parser plus resolver.
  Preserved completedr14 seed already exists/hashverified; r16 helper will
  reuse that exact copy. No package rebuild or VM was started in r15.

- r15 running: container a89fa19e76f7, execsession33908. Frozen159files,
  host guards pass; completedr14 seed exclusively preserved before recapture.
  Log build/validated-source-20261024-r15-resumed-iso.log; isolation receipt
  build/iso-r15-isolation.json. Next actual stage2 then VM acceptance.

- r15 fixes ready: explicit amd64 Catalyst basearch (actual installed wrapper
  smoke and kmerge image-path probe pass); sys-fs/fuse:0 dependency resolver
  passes; fsscript nounset enabled after distro environment. Exact required
  live app/service paths and offscreen PySide initialization pass in retained
  read-only stage2. r15 will use preserved authenticated completed r14 archive
  as its stage1 seed, then fresh stage2; no fabricated resume markers.

- r14 stage2 stopped naturally exit2 at fsscript: distro debuginfod profile
  read optional DEBUGINFOD_URLS under nounset. Corrected profile/nounset
  boundary passes actual retained-chroot read-only probe. Sol reviewing
  Catalyst arch environment and missing FUSE2 NTFS module utilities.
  Stage1 archive and all784 installed packages preserved; preparing r15
  supported archive-seed reuse. No VM yet. Receipt build/r14-stage2-failure.json.

- r14 STAGE1 COMPLETE (~2.9GiBtar.gz); stage2started22:19:22UTC using
  retainedstage1chroot. Livekernel/dracut/bootableISO assembly active.
  ActualVMtest stillpending.

- r14 STAGE1 ALLPACKAGES COMPLETED. Catalyst began capturetar.gz at
  2026-10-23 22:16:19UTC. Correctinstaller0.1.0-r1 installed/verified.
  Stage2kernel/initramfs/ISOassembly andrealVMacceptance stillpending.

- r14 installer0.1.0-r1 actualupgradecompleted9/14. In-chrootverification:
  executable/etc/init.d/gentoo-installerd present, old.initdabsent, systemd
  unitpresent, bothPythonentrypointsimport/--helpPASS. Packagephasehealthy.

- r14 guardedresumeRUNNING: ownedcontainer52b693a97908, execsession79378.
  Log build/validated-source-20261024-r14-resumed-iso.log; isolationreceipt
  build/iso-r14-isolation.json. r13 stopped/auto-removed, receipt retained.
  Frozen159files; installerrevision/servicebasename andonepreflightline
  onlyproductdeltas. Verifyr1installedpaths beforestage2assembly.

- Installer0.1.0-r1 isolatedphase/pathprobePASS: executableexactOpenRC
  basename, no.initdsuffix, correctsystemdunit. ActualPortageresolver
  recognizesoneupgradefrominstalledr0, zerodownloads. Logs:
  build/probe-installer-r1.log andbuild/probe-installer-r1-upgrade.log.
  Freezingr14 withnewinitd/ebuildrevision andbuildpreflightpathfix.

- r13 PAUSED ownedcontaineraf5022ef7f14 duringbtrfs51/64 afterservice-path
  audit found /etc/init.d/gentoo-installerd.initd instead ofexpectedbasename.
  Sol fixesnewinitd andbumpsinstaller ebuild to0.1.0-r1 (archiveunchanged);
  scripts/build-iso.sh preflight updatesfilename. Astra captures exactcache
  and preparesguardedr14resume; nohostdisks involved. Pause receipt:
  build/r13-paused-service-path.json.

- r13 main build: all3localpackages compiled/installed successfully: HWMonux
  2/64, PortageHub22/64, gentoo-live-installer47/64. Remaining live-system
  packages then stage1capture/stage2assembly are pending. No VM yet.

- r13 resumed build RUNNING: owned containeraf5022ef7f14, execsession31051.
  Log build/validated-source-20261024-r13-resumed-iso.log; isolation receipt
  build/iso-r13-isolation.json. Frozen159files, exact6pathdelta saved in
  build/r12-r13-source-difference.json. Guarded resume retainsPySide.

- Explicit .tar.zst unpack fixes validated by real isolated networkless
  ebuild phases: all3unpack/prepare, bothPythonconfigure/compile/install,
  PortageHubcargooffline metadata. Log build/probe-local-ebuilds.log.
  Only3ebuilds changed; source archives unchanged. Freezingr13 for resume.

- HWMonux root cause: Portage default unpacker omits .tar.zst; archive
  contents/prefix are correct. Sol adds explicit tar --zstd unpack and
  zstd BDEPEND to all3localebuilds. Source archives unchanged.
- Retained cache receipt build/r12-resume-cache-receipt.json confirms722
  installed packages including successful PySide6.11.2, no local app packages,
  exact4resume markers and unchanged original seed hash.

- r12 stopped/auto-removed after successful PySide install, on HWMonux
  prepare phase: expected source work/hwmonux-1.0.0 absent. Main session35067
  exit2. Sol repairing all local archive/ebuild layout contracts; Astra
  preparing safe resume with refreshed uninstalled local overlay. No ISO yet.

- r12 actual PySide Ninja confirmed `/usr/bin/ninja -j4 -l8 -v`; zeroOOM
  events in observed samples. Evidence build/r12-ninja-observe.log.
- Post-harness failure-screenshot changes: full scripts/test.sh passed,
  204Python tests and65Rust tests plus real namespace cleanup regression.
  Log build/python-pre-vm-final-tests.log. No VM has been launched yet.

- Guarded r12 resume RUNNING: owned container7a1c6e3d3305, exec session35067.
  Exact input guard passed for712VDB packages; reference source/config/archives
  matched. Log build/validated-source-20261024-r12-resumed-iso.log; isolation
  receipt build/iso-r12-isolation.json. No ISO or VM acceptance yet.

- r11 frozen source build/validated-source-20261024-r11 stopped at PySide435/509;
  exec session58290, log build/validated-source-20261024-r11-iso.log.
  Owned container bdacca72f812 stopped/auto-removed; isolation receipt build/iso-r11-isolation.json.
  All post-r10 GUI, target-dependency and Ninja-limit fixes are included.
  Actual PySide still launched bare Ninja: Catalyst setup_environment overrides
  make.conf with NINJAOPTS="" when no FIFO jobserver exists. Paused before
  bindings/OOM;434completed packages remain installed/cached. Evidence:
  build/iso-r11-paused-pyside.json, iso-r11-pyside-environment.txt,
  iso-r11-pyside-build.log, catalyst-ninja-empty-environment-repro.log.
  Wrapper setup_environment fix passes real installed Catalyst smoke:
  build/catalyst-ninja-environment-smoke.log. No application source changed.
  Guarded supported stage1 autoresume is being prepared for frozen r12.
  build/r11-resume-reference saves source/config/archive hashes, four markers,
  and712 installed VDB package entries. No companion application packages were
  installed yet, and no build_packages marker exists. Stop receipt:
  build/iso-r11-stopped-for-resume.json. Do not use Catalyst -a: it clears state.
- ISO r10 stopped after PySide package447/521 exceeded its container memory cap.
  Completed446 packages are cached, including glibc and Mesa. No usable ISO
  or VM acceptance yet. Main log: build/validated-source-20261024-r10-iso.log.
- The owned r10 builder deb675e20d94 was stopped and auto-removed.
  Evidence: build/iso-r10-pyside-resource-failure.json and
  build/iso-r10-pyside-build.log. No host device passthrough; 4CPU/8GiB limit.
- Current tests:203Python tests/28subtests pass in root's full rerun
  (build/python-r11-freeze-tests.log). Rust65tests passed.
- Working harness adds bounded XFCE readiness polling after r10 freeze; this
  script is not installed in the ISO. SHA256:
  cc08deefc7887937c8a405fb0cc83fb646d415a3679ac019818af75c443e3cb1.
- Next: guarded r12 stage1 autoresume with exact input equality checks; verify
  actual PySide Ninja invocation is bounded, complete normal stage2,
  then live-boot-only smoke via build/boot-live-smoke.py. Run GUI OpenRC install+same-session resume, disk-only
  boot/probes, then systemd cancellation/resume, LUKS and BIOS coverage.
- IMPORTANT: r10 lacks the newly found GUI mountpoint fix: lsblk [null] was
  incorrectly treated as mounted, preventing erase selection. Working source
  now tests nonempty mountpoints recursively. GUI driver now traverses all
  Continue buttons and checks exact manifest/target before confirmation; all
  four VM scenario manifests round-trip in tests. Need r11 source image for
  final acceptance. Also corrected clock warning's unimplemented sync promise.
- Post-r10 target resolver fixes: minimal HWMonux requires qtbase[opengl]
  and libxkbcommon[X]; PortageHub on systemd requires systemd[policykit].
  Actual OpenRC resolver now passes (build/target-resolver-review.log).
  Authenticated native systemd seed resolver also passes
  (build/target-systemd-native-resolver.log), including kernel/firmware,
  companions, GRUB and cryptsetup. Earlier systemd failure using an OpenRC
  seed was a test-environment init-system conflict, superseded by native pass.
- PySide failure cause: inherited NINJAOPTS="" disables Gentoo's MAKEOPTS
  fallback. The build passed --parallel=4 but actually ran bare ninja -v,
  launching about24 C++ compilers. Working ISO make.conf and both preservation
  allowlists now set explicit NINJAOPTS; target Configurer similarly derives
  bounded Ninja settings. Memory cap prevented host-wide exhaustion.
- Explicit Ninja setting verified with pinned eclass and rewrite-hook smoke
  (build/iso-r11-ninja-smoke.log). Freeze/build r11 is the next actual compile
  acceptance. Source snapshot includes README and all post-r10 fixes.

## Preserved baseline

Before any repair, 150 source files were archived with SHA-256 inventory at:
`/home/edo/Downloads/Gentoo_LiveCD_installer-review-backups/20261023T192213Z/`.
There was no Git repository in this project. Preserve unrelated WIP and existing
build artifacts. Do not reset, clean, stop, or replace unrelated processes.

At takeover an existing Docker image build was active (PID 896661, container
ed8c8d0c6fa7); it has no host bind mounts. Recheck live identity before acting.
An unrelated PortageHub release build was active in
`/home/edo/Downloads/Gentoo_package_mananger`; do not modify that project.

## Ownership

- Astra (`storage_review`): storage.py, install.py, runner.py and relevant tests.
- Sol (`iso_config_review`): stage3.py, configure.py, corresponding tests,
  HWMonux/PortageHub ebuilds.
- Luna (`ui_review`): GUI, manifest.py, server.py, corresponding tests,
  installer ebuild/services.
- Root: Rust safefs cleanup, ISO scripts, VM harness and end-to-end verification.

## Acceptance and safety

- Fix all eleven review findings and add regression coverage.
- VM disks must be new regular qcow2 files below build/vm, with no host disk,
  device passthrough or writable host-directory shares.
- Boot the live environment, perform an actual install, detach live media, boot
  the installed target, and verify init, user, hostname, networking and packages.
- Keep logs and exact outcomes; mocked tests do not count as VM acceptance.
- ISO test SSH keys are ephemeral and test-only. Do not distribute a test-access
  ISO as a production release.

## Progress as of 2026-10-24

- All eleven original findings repaired across storage/resume, cancellation,
  stage3 authentication/extraction, permissions, companion packaging and Rust
  bind-mount-safe recursive deletion. Independent review found and repaired
  additional cache security and stable boot UUID/ESP configuration issues.
- Python aggregate: 147 passed, 18 subtests before final review hardening.
  Focused Stage3/configuration retest: 36 passed. Final aggregate still required.
- PortageHub full Rust suite: 65 passed. Bind-mount regression used private
  unprivileged user/mount namespaces with temporary regular directories only.
- First frozen snapshot build/validated-source-20261024 is preserved. Its ISO
  attempt failed in source packaging because snapshot excluded nested app dist
  source files. No usable ISO or VM acceptance yet.
- Original Kimi builder image completed successfully and expensive layers are
  reused. Logs: build/iso-test-build.log, build/python-fixed-tests.log,
  build/portagehub-all-tests.log, build/snapshot-tests.log.
- Final snapshot must include apps/portagehub/dist and newest agent patches.
  Additional ISO corrections: binary release keyring, bundled overlay Manifests,
  installer user group, valid display-manager startup, optional masked Broadcom
  module removed, key-only root SSH for test image and truthful image metadata.
- Test-only key: build/vm-control/id_ed25519. Never publish private key or distribute
  a test-key-enabled ISO as a production artifact.
- No physical disk installation, formatting, or device passthrough performed.
  Unrelated open-webui and external PortageHub checkout were not altered.

## Latest integration checkpoint

- Frozen r3 aggregate: 178 Python tests and 21 subtests pass (build/snapshot-r3-tests.log).
- Actual official OpenRC Stage3 authenticated download/extraction succeeded in a
  disposable no-device container. Evidence: build/stage3-container-integration-20261024.log.
  Confirmed char devices, root/group ownership, relative links, SUID passwd.
- r2 snapshot: build/validated-source-20261024-r2 with separate SHA256 inventory.
  Its ISO attempt naturally failed before Catalyst on readonly DISTDIR assignment;
  no package compilation or VM occurred. Archive/cache downloads preserved.
- Current source now additionally fixes target storage utility ordering before
  initramfs, OpenRC display-manager package, manual /boot/efi paths, checked GUI
  artifact capture, non-root X session discovery, and external bounded LUKS QMP mode.
- Catalyst make.conf replacement now uses supported envvars/spec keys. Removed
  purge-only -T from actual build calls. Pinned repository binds are verified
  read-only, including duplicate roots; no loop devices are exposed.
- r3 reached actual stage1 but Portage network-sandbox could not configure its
  isolated loopback interface. Stopped only the verified r3 container cleanly;
  evidence is build/iso-r3-stopped.json and validated-source-20261024-r3-iso.log.
  Builder now adds NET_ADMIN inside its own network namespace, with no host
  network, host devices, or privileged mode. Limits: 4 CPUs and 12 GiB RAM.
- Sol is checking the full resolver with the real desktop profile and container
  capabilities. Latest correction breaks the libwebp/tiff binary dependency cycle.
  Frozen r4 passes 179 tests/21 subtests but was not built. Its resolver check
  incorrectly exported USE, overriding package.use; focused corrected check
  resolves the binary cycle. Actual seed build is the decisive verification.
- Astra identified a Catalyst 4.2 kerncache/distkernel path bug before stage2:
  it moves the kernel source directory then passes the old path to dracut.
  Disable kerncache (retain seed/package caches) and explicitly use
  sys-kernel/gentoo-kernel-bin for the live kernel. Freeze r5 for the actual build.
- r5 reached metadata sourcing without network-sandbox errors, but repository
  top directory retained mktemp mode 0700. Portage workers failed with permission
  denied. Stop receipt: build/iso-r5-stopped.json; isolation receipt:
  build/iso-r5-isolation.json. Fix only the snapshot top directory to 0755,
  including existing cached extractions. Add explicit sys-apps/busybox for the
  mandatory dracut -I busybox argument. Freeze r6 next. No package build or VM
  acceptance yet.
- No ISO acceptance or VM end-to-end acceptance yet. Next: final source snapshot,
  real Catalyst build, GUI install to newly created qcow2, media-free target reboot.
- r6 exited naturally at resolver: ipw2100 firmware license masked because
  Catalyst replaced source make.conf (including ACCEPT_LICENSE). No sandbox or
  repository permission errors this attempt. Log:
  build/validated-source-20261024-r6-iso.log; isolation receipt iso-r6-isolation.json.
  No main builder running. Sol/Astra inspecting complete effective configuration
  before r7, including license, GRUB platforms, Python and signed-binary flags.
- Exact-seed r7 resolver passes all 93 stage1 atoms (522 planned packages),
  with firmware licenses, both GRUB platforms, Python 3.14 and binary signatures
  verified effective. Evidence: build/iso-r7-exact-seed-resolver.log and
  build/catalyst-make-conf-smoke.log. Wrapper preserves allowlisted make.conf
  settings on initial setup and cleanup. Root now freezes r7 and builds.
- Host ongoing work raised memory usage; builder cap lowered to 8 GiB with
  no extra swap and four CPUs. Unrelated processes remain untouched.
- Active ISO attempt: frozen build/validated-source-20261024-r7, log
  build/validated-source-20261024-r7-iso.log, exec session 5718, container
  c1cbbd733599 (recheck identity before any action). Actual resolver cleared;
  package downloads started. No ISO or VM acceptance yet.
- Current working harness includes opt-in --cancel-then-resume, added after the
  r7 source freeze (installer code unchanged). It cancels via actual GUI at
  stage3 after storage completion, requires unsuccessful/cancelled result and
  completed cleanup, checks no target mounts/swap/mappings, then serial-guarded
  CLI resume without formatting before disk-only boot. Default GUI and
  --check-resume paths stay intact. Working suite: 182 tests +21 subtests pass.
- r7 installed 24 signed remote packages, then its first local source package
  compiled but failed binary-cache packaging: global binpkg-request-signature
  rejects Catalyst's unsigned locally generated gpkg. Main build exited1;
  no container remains. Sol/Astra verified exact Portage per-binrepo signature
  enforcement. Correct builder feature policy removes global requirement while
  retaining mandatory Gentoo remote verify-signature=true and distinct remote
  cache location. Never set binpkg-ignore-signature. Sol is running an actual
  source-build and remote-verification smoke before r8.
- Signature-policy smoke passed: actual signed remote package installed; local
  --buildpkg succeeded; installed Portage's exact remote verification API rejects
  the same unsigned gpkg when verify_signature=True. Logs:
  build/iso-r8-remote-signed-install.log, iso-r8-local-buildpkg.log and
  iso-r8-signature-rejection.log. Global builder signature requirement removed;
  Gentoo repository policy remains mandatory. Root starts frozen r8 next.
- r8 deliberately stopped before source compilation after independent resource
  review: compression tools see host32CPUs/46GiB rather than cgroup4CPU/8GiB.
  Both stage captures now use bounded GNU gzip; stage2 uses gensquashfs -j2
  -Q20 (20*128KiB data-block queue). Exact Catalyst spec parser, real squashfs
  write/read and gzip roundtrip passed: build/catalyst-compression-smoke.log.
  Stop receipt build/iso-r8-stopped.json. Root starts frozen r9 next.
- r9 is active from frozen build/validated-source-20261024-r9, log
  build/validated-source-20261024-r9-iso.log, exec session 20267, container
  019036788b52 (recheck live identity before acting). It has passed both signed
  remote installs and local source buildpkg, completing 30/521 package operations.
  Isolation receipt: build/iso-r9-isolation.json. No VM has been launched yet.
- Latest working Python suite: 182 passed, 21 subtests
  (build/python-pre-r8-tests.log). Actual pinned Stage3 archive metadata audit
  scanned all 66,311 members: no PAX xattrs, ACLs, or capability entries are
  present (build/stage3-pax-metadata-audit.log).
- r9 exited naturally at package72/521: app-arch/cpio's postinst creates an
  unowned cpio->gcpio compatibility link, which the builder's extra
  collision-protect feature rejects when app-alternatives/cpio claims it.
  Builder now uses Gentoo's default protect-owned policy. Actual merge of the
  failed package in the disposable r9 chroot passes and records ownership:
  build/iso-r10-cpio-collision-smoke.log. No main builder remains running.
- Final preflight found target defaults also named nonexistent amd-ucode and
  lacked selected firmware licenses. Sol removed the invalid atom and added
  narrow package.license/initramfs USE settings; Astra now installs requested
  firmware/microcode before kernel hooks and dracut. Focused43tests pass.
  Luna is correcting invalid generated UI profile paths before r10 freeze.
- r10 source is stable: profile generator and default KDE profile now match
  published Gentoo paths, all eight Stage3 variants checked against official
  autobuild index. Plain-root initramfs also explicitly uses generic mode so
  it cannot capture live-root host-only assumptions before target fstab exists.
  Root full rerun: build/python-r10-final-tests.log. Freeze r10 and rebuild
  using preserved package/distfile caches; still no VM acceptance.
- Active r10: frozen build/validated-source-20261024-r10; log
  build/validated-source-20261024-r10-iso.log; exec session46766; container
  deb675e20d94 (recheck exact readonly /workspace mount before acting).
  Isolation receipt build/iso-r10-isolation.json confirms bridge network,
  no host devices, no privileged mode, 4CPU/8GiB bound. Root full suite:
  187passed/28subtests. Build now installing the521package set using prior
  source-package cache. Added README.md after freeze (documentation only).
