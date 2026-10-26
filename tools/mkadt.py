#!/usr/bin/env python3
"""Build the minimal Apple Device Tree that the SWEET xnu needs to boot.

Only what the early boot path actually reads (see NOTES.md); every MMIO
address comes from the sweet live FDT. Usage:  mkadt.py OUT.adt [--dump] [--qemu] [--highmem]
"""
import argparse
import hashlib, os, struct, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adt  # noqa: E402

# Kernel-managed RAM: above every Qualcomm XPU carve-out (see NOTES.md)
PHYS_BASE = 0xb0000000
MEM_SIZE = 0x4d000000
# Panic log buffer, carved from the TOP of the dram range and exposed to XNU as
# /pram + /chosen/embedded-panic-log-size. Without it, pe_init.c's
# check_for_panic_log() leaves gPanicBase == 0, debug_log_init() bails out twice
# and the kernel hangs right after "kdp_core zlib memory" — reproduced on QEMU
# and independently on ios-fajita.
PANIC_LOG_SIZE = 0x100000
# --highmem (phone only): the whole upper DDR bank. The live FDT memory node is
# <0x80000000 2G>, <0x1_00000000 0xfcc00000>; every fixed reserved-memory
# carve-out is in the low bank and the dynamic pools are limited below 4G.
HIGH_PHYS_BASE = 0x100000000
HIGH_MEM_SIZE = 0xfcc00000
# /arm-io children are relative to this; it must not be 0 (serial_init bails)
SOC_BASE = 0x01000000

GICD, GICD_SIZE = 0x17a00000, 0x10000
GICR, GICR_SIZE = 0x17a60000, 0x100000
FB, FB_SIZE = 0x9c000000, 0x1700000
SWEET_ECID = 0x5357454554000001  # 'SWEET' + 1, synthetic
FB_W, FB_H, FB_STRIDE = 1080, 2400, 1080 * 4
# Internal UFS host (ufshc@1d84000 "ufs_mem"); QEMU virt has none
UFS, UFS_SIZE = 0x01d84000, 0x3000
TIMEBASE = 19200000
VTIMER_INTID = 19
# QEMU virt (-M virt,gic-version=3 -cpu cortex-a76): GICD/GICR and the generic timer's CNTFRQ
QEMU_GICD, QEMU_GICR, QEMU_TIMEBASE = 0x08000000, 0x080a0000, 62500000
QEMU_VTIMER_INTID = 27

# ---------------------------------------------------------------------------
# Per-board profile. Every number above was read out of the sweet live FDT;
# another SoC reuses none of them by assumption, so they live in a table and
# build() takes it as input. 'sweet' reproduces the original output byte for
# byte (tools/validate-repo.py and the CI images depend on that).
# ---------------------------------------------------------------------------
BASE_PROFILE = dict(
    board='xiaomi,sweet',       # stays: SweetPlatformExpert matches this name
    soc='qcom,sm7150',
    soc_base=SOC_BASE,
    phys_base=PHYS_BASE,
    mem_size=MEM_SIZE,
    high_phys_base=HIGH_PHYS_BASE,
    high_mem_size=HIGH_MEM_SIZE,
    gicd=GICD, gicd_size=GICD_SIZE,
    gicr=GICR, gicr_size=GICR_SIZE,
    vtimer_intid=VTIMER_INTID,
    timebase=TIMEBASE,
    fb=FB, fb_size=FB_SIZE,
    fb_w=FB_W, fb_h=FB_H,
    ufs=UFS, ufs_size=UFS_SIZE,
    ufs_node=True,
    panic_log_size=PANIC_LOG_SIZE,
    canvas=(1125, 2436),        # iPhone X presented size, not the panel's
    cpu_compatible=('qcom,kryo470', 'ARM,v8'),
    # Per-core clock-frequency, informational only (no cpufreq in early boot).
    cpu_clocks=[1804800000] * 6 + [2304000000] * 2,
)

PROFILES = {
    'sweet': {},
    'penrose': dict(
        # Black Shark 4 (PRS-A0), SM8250-AC. Values below are read from the
        # official V13.22.10.21 PROS2210210CN00AS1 package (vendor_boot.img +
        # dtbo.img penrose fragment), see penrose-port/out/penrose-facts.json.
        soc='qcom,sm8250',
        # GICD/GICR, UFS host and the 19.2 MHz timebase are identical to
        # sweet's; cont_splash_region is at 0x9c000000 too, 1080x2400.
        # Only the SoC id and the CPU topology actually differ.
        # kryo470 stays in the list: SweetCPU matches on it (patch 0018).
        cpu_compatible=('qcom,kryo585', 'qcom,kryo470', 'ARM,v8'),
        # 4x A55 + 3x A77 + 1x A77 prime. vendor_boot's cpu@0..cpu@700 confirm
        # reg = 0x0..0x700 (MPIDR Aff1:Aff0), so core << 8 still holds. The
        # live tree has three clusters (cpu-map cluster0/1/2) while this one
        # keeps sweet's single homogeneous cluster: fine for bring-up, revisit
        # for cpufreq/scheduling once the kernel boots with cpus=8.
        cpu_clocks=[1804800000] * 4 + [2419200000] * 3 + [3187200000],
        # Upper DDR bank. The dtb cannot tell us (its /memory reg is zeroed and
        # the bootloader fills it in), so this comes from the phone itself:
        # MemTotal 7797180 kB => an 8 GiB SKU, i.e. DRAM spans
        # 0x80000000..0x280000000 with the bank boundary at 4 GiB.
        # 6 GiB minus the 0x3400000 the bootloader keeps at the top, the same
        # reservation sweet's live FDT shows. The value ends 64 MiB below the
        # physical end, so even if this board reserves more, we stay inside RAM.
        # (An over-declared dram-size makes XNU touch absent memory and hang;
        # under-declaring only wastes RAM, so the margin errs the safe way.)
        high_mem_size=0x17cc00000,
    ),
}

BOOT_MANIFEST_HASH = hashlib.sha384(b'sweet boot manifest').digest()

# The iOS userland is the iPhone X (iPhone10,3 / D22AP) IPSW. MobileGestalt
# derives product type, device class, home-button-less UI, artwork etc. from
# the root identity and /product of the real D22AP device tree. That tree is
# an Apple artifact (kept out of git): extract it from the IPSW with
#   tools/remotezip.py URL DeviceTree.d22ap.im4p DIR
#   ipsw img4 im4p extract DIR/Firmware/all_flash/DeviceTree.d22ap.im4p -o d22ap.adt
D22AP_ADT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../research/ipsw-x/d22ap.adt')


def d22ap_product(path=D22AP_ADT):
    """/product properties of the real D22AP tree, without the syscfg
    placeholders iBoot would fill in (flag bit set) and the node bookkeeping."""
    if not os.path.exists(path):
        print(f'warning: {path} missing, no /product node', file=sys.stderr)
        return None
    root, _ = adt.parse(open(path, 'rb').read())
    for c in root.children:
        if dict((k, v) for k, f, v in c.props).get('name') == b'product\0':
            return [(k, v) for k, f, v in c.props if not f and k not in ('name', 'AAPL,phandle')]
    raise SystemExit(f'{path}: no /product node')

u32 = lambda *v: struct.pack('<%dI' % len(v), *v)
u64 = lambda *v: struct.pack('<%dQ' % len(v), *v)
cstr = lambda s: s.encode() + b'\0'


def node(name, props=(), children=()):
    n = adt.Node()
    n.props = [('name', 0, cstr(name))] + [(k, 0, v) for k, v in props]
    n.children = list(children)
    return n


def load_profile(name=None):
    """Merge a named profile (or a JSON file) over the sweet defaults."""
    cfg = dict(BASE_PROFILE)
    if not name:
        return cfg
    if os.path.exists(name):
        import json
        cfg.update(json.load(open(name)))
        return cfg
    if name not in PROFILES:
        raise SystemExit(f'unknown profile {name!r}; have {", ".join(PROFILES)}')
    cfg.update(PROFILES[name])
    return cfg


def build(seed=None, qemu=False, highmem=False, cfg=None):
    """qemu=True: same tree, but with QEMU virt's GIC addresses and CNTFRQ (tools/qemu-sweet.py)."""
    cfg = dict(cfg or BASE_PROFILE)
    if highmem:
        phys_base, mem_size = cfg['high_phys_base'], cfg['high_mem_size']
        if mem_size is None:
            raise SystemExit(
                'profile has no high_mem_size: pass --high-mem-size '
                '(phone: cat /proc/iomem, add up the System RAM ranges)'
            )
    else:
        phys_base, mem_size = cfg['phys_base'], cfg['mem_size']
    # Carve the panic log off the top so the kernel never allocates over it.
    panic_log_size = cfg['panic_log_size']
    panic_base = phys_base + mem_size - panic_log_size
    mem_size -= panic_log_size
    gicd, gicr = cfg['gicd'], cfg['gicr']
    timebase = cfg['timebase']
    if qemu:
        gicd, gicr, timebase = QEMU_GICD, QEMU_GICR, QEMU_TIMEBASE
    seed = seed if seed is not None else os.urandom(64)
    phandle = iter(range(1, 1000))
    ph = lambda: ('AAPL,phandle', u32(next(phandle)))

    chosen = node('chosen', [
        ph(),
        ('dram-base', u64(phys_base)),
        ('dram-size', u64(mem_size)),
        ('random-seed', seed),
        ('debug-enabled', u32(1)),
        ('embedded-panic-log-size', u32(panic_log_size)),
        ('firmware-version', cstr('sweet-shim')),
        ('system-firmware-version', cstr('sweet-shim')),
        # iBoot's SHA-384 of the boot manifest; /sbin/mount -P 1 exits unless
        # it exists (bind mounts /private/preboot/<hex>/usr/standalone/firmware,
        # tolerating a missing directory). Fixed stand-in value.
        ('boot-manifest-hash', BOOT_MANIFEST_HASH),
        # libimg4 (cryptexd) aborts "failed to get crypto algorithm" without
        # it; real A11+ device trees say sha2-384.
        ('crypto-hash-method', cstr('sha2-384')),
        # MobileGestalt's screen class: display-scale (u32, default 0) comes
        # from /chosen, the pixel size from IOMobileFramebuffer. Scale 0 makes
        # SpringBoard's FBSDisplayMode "infxinf@0x" invalid. 1080x2400 @3x =
        # 360x800 points (like iPhone X class devices).
        ('display-scale', u32(3)),
        # XNU derives IOPlatformUUID (gethostuuid) from these on arm; without
        # them CFPreferences cannot use kCFPreferencesCurrentHost (no ByHost
        # files), so e.g. DataMigration never records LastSystemVersion and
        # migrates on every boot. Synthetic ECID (not the phone's); chip-id
        # of the A11 that the iPhone X userland expects.
        ('unique-chip-id', struct.pack('<Q', SWEET_ECID)),
        ('chip-id', u32(0x8015)),
        # Security status as on a dev-fused, undemoted board (tuple 0,0,1,1):
        # MobileGestalt reads these from /chosen, and mobileactivationd's
        # DeviceType then takes its native "Hactivation" branch (state
        # Activated without an activation record; no SEP, no baseband here).
        ('certificate-production-status', u32(0)),
        ('effective-production-status-ap', u32(0)),
        ('certificate-security-mode', u32(1)),
        ('effective-security-mode-sep', u32(1)),
    ], [node('memory-map', [ph()]),
        # Local boot policy (kern_csr.c): SIP relaxed like `csrutil disable`
        # (lp-sip0: untrusted kexts, unrestricted FS, task_for_pid, kernel
        # debugger, dtrace, nvram), lp-sip1: unauthenticated (unsealed) root.
        # The iOS shared cache is in no trust cache, so dyld can only map it
        # with CSR_ALLOW_UNRESTRICTED_FS (vm_unix.c shared region checks).
        node('asmb', [ph(), ('lp-sip0', u64(0x6f)), ('lp-sip1', u32(1))])])

    # SMP: all cores in physical-core order, one homogeneous cluster. The
    # kernel defaults to one and uses cpus=N to select an initial prefix.
    # QEMU retains its original single-CPU tree: its MPIDRs differ.
    clocks = cfg['cpu_clocks']
    cpu_compat = b''.join(cstr(c) for c in cfg['cpu_compatible'])
    cpu_nodes = []
    for core, clock in enumerate(clocks[:1] if qemu else clocks):
        cpu_nodes.append(node(f'cpu{core}', [
            ph(),
            ('device_type', cstr('cpu')),
            ('compatible', cpu_compat),
            ('reg', u32(core << 8)),  # actual MPIDR Aff1:Aff0, NOT logical ID
            ('cpu-id', u32(core)),   # descriptive: XNU assigns IDs by DT order
            ('cluster-id', u32(0)),  # descriptive; see ml_parse_cpu_topology
            ('cluster-type', u32(0)),  # homogeneous CLUSTER_TYPE_SMP, not AMP
            ('die-cluster-id', u32(0)),
            ('cluster-core-id', u32(core)),
            ('l2-cache-id', u32(core)),  # distinct per-core L2 nodes in live FDT
            ('l3-cache-id', u32(0)),     # shared L3
            ('state', cstr('running' if core == 0 else 'stopped')),
            ('timebase-frequency', u32(timebase)),
            ('clock-frequency', u32(clock)),
            ('bus-frequency', u32(100000000)),
            ('memory-frequency', u32(1866000000)),
            ('peripheral-frequency', u32(timebase)),
            ('fixed-frequency', u32(timebase)),
        ]))
    cpus = node('cpus', [ph(), ('#address-cells', u32(1)),
                         ('#size-cells', u32(0))], cpu_nodes)

    gic = node('gic', [
        ph(),
        ('compatible', cstr('arm,gic-v3')),
        ('interrupt-controller', b''),
        ('virtual-timer-intid', u32(QEMU_VTIMER_INTID if qemu else cfg['vtimer_intid'])),
        ('reg', u64(gicd - cfg['soc_base'], cfg['gicd_size'],
                    gicr - cfg['soc_base'], cfg['gicr_size'])),
    ])
    arm_io = node('arm-io', [
        ph(),
        ('device_type', cstr('sweet-io')),
        ('compatible', cstr('sweet-io') + cstr('arm-io')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
        ('ranges', u64(0, cfg['soc_base'], 0x100000000 - cfg['soc_base'])),
    ], [gic,
        # No hardware: watchdogd checks IODeviceTree:/arm-io/wdt for no-pmu and
        # otherwise crashes trying to set the PMU boot stage (customer builds
        # ignore the -wdt_no_pmu boot-arg).
        node('wdt', [ph(), ('no-pmu', u32(1))]),
        # Main display: IOMobileFramebuffer.framework counts /arm-io children
        # with this device_type and reads the canvas size from the node
        # (the presented iPhone X size, see SweetFramebuffer.cpp);
        # the kernel's AppleCLCD matches the compatible string.
        node('disp0', [
            ph(),
            ('device_type', cstr('display-subsystem')),
            ('compatible', cstr('sweet,display')),
            ('canvas-width', u32(cfg['canvas'][0])),
            ('canvas-height', u32(cfg['canvas'][1])),
        ])])

    fbcon = node('sweet-fbcon', [
        ph(),
        ('reg', u64(cfg['fb'], cfg['fb_size'])),
        ('width', u32(cfg['fb_w'])),
        ('height', u32(cfg['fb_h'])),
        ('stride', u32(cfg['fb_w'] * 4)),
    ])

    # bsd/dev/sweet_ufs.c: the UFS host ABL leaves running (absolute physical reg).
    ufs = node('sweet-ufs', [
        ph(),
        ('compatible', cstr('qcom,ufshc')),
        ('reg', u64(cfg['ufs'], cfg['ufs_size'])),
    ])

    # pmap_compute_max_asids requires the node even when no overrides are set.
    # An absent pmap-max-asids property selects XNU's own MAX_ASIDS default.
    # No effaceable storage: keybagd then reads /private/var/keybags/systembag.kb
    # as a plain plist instead of decrypting it with the effaceable key.
    defaults = node('defaults', [ph(), ('no-effaceable-storage', u32(1))])

    # MobileGestalt HomeButtonType = IODeviceTree:/buttons home-button-type
    # (D22AP: 2, no Home button; absent = 0 = Home-button device).
    buttons = node('buttons', [
        ph(),
        ('button-names', cstr('volup') + cstr('voldown') + cstr('hold')),
        ('home-button-type', u32(2)),
        ('opposed-power-vol-buttons', u32(1)),
    ])

    product_props = d22ap_product()
    product = [node('product', [ph()] + product_props)] if product_props else []

    # Panic log region. /pram reg = <phys size>; it sits just above the
    # dram-size we expose, so XNU maps it but never allocates into it.
    pram = node('pram', [ph(), ('reg', u64(panic_base) + u64(panic_log_size))])

    # Identity of the iPhone X whose userland runs here: compatible[0] is
    # hw.model/hw.target (D22AP), model is hw.machine/hw.product
    # (iPhone10,3). The board name stays for SweetPlatformExpert matching.
    return node('device-tree', [
        ph(),
        ('compatible', cstr('D22AP') + cstr('iPhone10,3') + cstr(cfg['board']) + cstr(cfg['soc']) + cstr('AppleARM')),
        ('model', cstr('iPhone10,3')),
        ('target-type', cstr('D22')),
        ('target-sub-type', cstr('D22AP')),
        ('manufacturer', cstr('Apple Inc.')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
    ], [chosen, defaults, cpus, arm_io, fbcon, buttons, pram] + product +
        ([] if qemu or not cfg['ufs_node'] else [ufs]))


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('out', help='output .adt')
    ap.add_argument('--profile', default=None,
                    help='device profile name (%s) or a JSON file' % ', '.join(PROFILES))
    ap.add_argument('--high-mem-size', type=lambda s: int(s, 0), default=None,
                    help='override the upper DDR bank span, e.g. 0x17cc00000')
    ap.add_argument('--phys-base', type=lambda s: int(s, 0), default=None,
                    help='override dram-base')
    ap.add_argument('--qemu', action='store_true')
    ap.add_argument('--highmem', action='store_true')
    ap.add_argument('--dump', action='store_true')
    a = ap.parse_args(argv)

    cfg = load_profile(a.profile)
    if a.high_mem_size is not None:
        cfg['high_mem_size'] = a.high_mem_size
    if a.phys_base is not None:
        cfg['high_phys_base'] = a.phys_base
    root = build(qemu=a.qemu, highmem=a.highmem, cfg=cfg)
    blob = adt.serialize(root)
    open(a.out, 'wb').write(blob)
    if a.dump:
        adt.dump(adt.parse(blob)[0])
    print(f'{a.out}: {len(blob)} bytes', file=sys.stderr)


if __name__ == '__main__':
    main(sys.argv[1:])
