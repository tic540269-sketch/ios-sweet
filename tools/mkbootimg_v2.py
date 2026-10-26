#!/usr/bin/env python3
"""Pack an Android boot image (header v0-v2) without the AOSP mkbootimg script.

Ubuntu has no mkbootimg package, and shim/build.sh needs one. This is a
drop-in replacement for the subset of the CLI that build.sh uses:

    mkbootimg_v2.py --header_version 2 --kernel Image --ramdisk rd.bin \
        --dtb dtb --base 0 --kernel_offset 0x8000 \
        --ramdisk_offset 0x1000000 --tags_offset 0x100 \
        --dtb_offset 0x1f00000 --pagesize 4096 \
        --os_version 17.0.0 --os_patch_level 2035-06 \
        --cmdline "console=null" -o shim-boot.img

Layout (AOSP bootimg.h):
    page 0            header (v2 = 1660 bytes, rest of page zeroed)
    kernel            padded to page
    ramdisk           padded to page
    second            padded to page
    recovery_dtbo     padded to page   (v1+)
    dtb               padded to page   (v2+)
"""
import argparse
import hashlib
import struct
import sys

PAGE = 4096
BOOT_MAGIC = b'ANDROID!'
NAME_LEN, CMDLINE_LEN, ID_LEN, EXTRA_LEN = 16, 512, 32, 1024
# v2 header: 8 magic + 10 u32 + 16 name + 512 cmdline + 32 id + 1024 extra
#            + 16 recovery_dtbo (size u32, offset u64, header_size u32)
#            + 12 dtb (size u32, addr u64)
HEADER_V0 = 8 + 40 + NAME_LEN + CMDLINE_LEN + ID_LEN + EXTRA_LEN
HEADER_V1 = HEADER_V0 + 16
HEADER_V2 = HEADER_V1 + 12


def parse_os_version(text):
    """"17.0.0" -> (17, 0, 0)."""
    parts = [int(x) for x in text.split('.')]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def parse_patch_level(text):
    """"2035-06" -> (35, 6), the AOSP (year-2000, month) encoding."""
    year, month = text.split('-')
    y = int(year)
    return (y - 2000 if y >= 2000 else y), int(month)


def pack_os_version(major, minor, patch, year, month):
    if not (0 <= major <= 127 and 0 <= minor <= 127 and 0 <= patch <= 127):
        raise SystemExit('os_version components must fit in 7 bits')
    if not (0 <= year <= 99 and 0 <= month <= 12):
        raise SystemExit('os_patch_level must be YYYY-MM with month 1-12')
    return (major << 25) | (minor << 18) | (patch << 11) | (year << 4) | month


def pad(data, page=PAGE):
    return data + b'\x00' * ((-len(data)) % page)


def build(args):
    kernel = open(args.kernel, 'rb').read() if args.kernel else b''
    ramdisk = open(args.ramdisk, 'rb').read() if args.ramdisk else b''
    second = open(args.second, 'rb').read() if args.second else b''
    dtb = open(args.dtb, 'rb').read() if args.dtb else b''
    recover = open(args.recovery_dtbo, 'rb').read() if args.recovery_dtbo else b''

    page = args.pagesize
    version = args.header_version

    if version < 1 and recover:
        raise SystemExit('recovery_dtbo needs --header_version 1 or 2')
    if version < 2 and dtb:
        raise SystemExit('--dtb needs --header_version 2')

    # AOSP: every *_addr is base + its *_offset. build.sh passes base 0.
    kernel_addr = args.base + args.kernel_offset
    ramdisk_addr = args.base + args.ramdisk_offset
    second_addr = args.base + args.second_offset
    tags_addr = args.base + args.tags_offset
    dtb_addr = args.base + args.dtb_offset

    header_size = {0: HEADER_V0, 1: HEADER_V1, 2: HEADER_V2}[version]

    # Same digest AOSP computes: length-prefixed sha1 over each section.
    sha = hashlib.sha1()
    for blob in (kernel, ramdisk, second, recover, dtb):
        sha.update(struct.pack('<I', len(blob)))
        sha.update(blob)
    img_id = sha.digest()[:ID_LEN]

    cmdline = args.cmdline.encode()
    extra = args.extra_cmdline.encode()
    if len(cmdline) > CMDLINE_LEN - 1:
        raise SystemExit('--cmdline longer than %d bytes' % (CMDLINE_LEN - 1))
    if len(extra) > EXTRA_LEN - 1:
        raise SystemExit('--extra_cmdline longer than %d bytes' % (EXTRA_LEN - 1))

    major, minor, patch = parse_os_version(args.os_version)
    year, month = parse_patch_level(args.os_patch_level)
    os_version = pack_os_version(major, minor, patch, year, month)

    hdr = bytearray()
    hdr += BOOT_MAGIC
    # NOTE: the v2 struct puts recovery_dtbo_size *after* the fixed part, so
    # these first ten fields are the same for v0/v1/v2.
    hdr += struct.pack(
        '<10I',
        len(kernel), kernel_addr,
        len(ramdisk), ramdisk_addr,
        len(second), second_addr,
        tags_addr, page, version, os_version,
    )
    hdr += args.board.encode()[:NAME_LEN].ljust(NAME_LEN, b'\x00')
    hdr += cmdline.ljust(CMDLINE_LEN, b'\x00')
    hdr += img_id.ljust(ID_LEN, b'\x00')
    hdr += extra.ljust(EXTRA_LEN, b'\x00')

    if version >= 1:
        # recovery_dtbo_offset is absolute in the file: it follows second,
        # so it depends on where second ends after padding.
        sec_end = page + len(pad(kernel, page)) + len(pad(ramdisk, page))
        sec_end += len(pad(second, page))
        hdr += struct.pack('<IQI', len(recover), sec_end if recover else 0, header_size)
    if version >= 2:
        hdr += struct.pack('<IQ', len(dtb), dtb_addr)

    if len(hdr) != header_size:
        raise SystemExit('internal: header %d != expected %d' % (len(hdr), header_size))

    out = pad(bytes(hdr), page)
    out += pad(kernel, page) if kernel else b''
    out += pad(ramdisk, page) if ramdisk else b''
    out += pad(second, page) if second else b''
    out += pad(recover, page) if recover else b''
    out += pad(dtb, page) if dtb else b''
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description='pack an Android boot image')
    ap.add_argument('--kernel')
    ap.add_argument('--ramdisk')
    ap.add_argument('--second')
    ap.add_argument('--dtb')
    ap.add_argument('--recovery_dtbo')
    ap.add_argument('--base', type=lambda s: int(s, 0), default=0)
    ap.add_argument('--kernel_offset', type=lambda s: int(s, 0), default=0x00008000)
    ap.add_argument('--ramdisk_offset', type=lambda s: int(s, 0), default=0x01000000)
    ap.add_argument('--second_offset', type=lambda s: int(s, 0), default=0x00f00000)
    ap.add_argument('--tags_offset', type=lambda s: int(s, 0), default=0x00000100)
    ap.add_argument('--dtb_offset', type=lambda s: int(s, 0), default=0x01f00000)
    ap.add_argument('--pagesize', type=int, default=PAGE)
    ap.add_argument('--header_version', type=int, default=0)
    ap.add_argument('--os_version', default='0.0.0')
    ap.add_argument('--os_patch_level', default='2000-01')
    ap.add_argument('--cmdline', default='')
    ap.add_argument('--extra_cmdline', default='')
    ap.add_argument('--board', default='')
    ap.add_argument('-o', '--output', required=True)
    args = ap.parse_args(argv)

    if args.pagesize & (args.pagesize - 1):
        raise SystemExit('--pagesize must be a power of two')

    blob = build(args)
    with open(args.output, 'wb') as fh:
        fh.write(blob)
    print('%s: %d bytes (header v%d, page %d)'
          % (args.output, len(blob), args.header_version, args.pagesize),
          file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
