#!/usr/bin/env python3
"""
nvmtool - inspect, sanitize and rebuild NVRAM (SPI flash) images of
Broadcom/QLogic BCM57810 (bnx2x) adapters such as HP FlexFabric 534FLR-SFP+.

Commands:
  info      FILE                        analyse an image: CRCs, VPD, MACs, firmware set
  sanitize  DUMP -o TEMPLATE            strip card identity (MACs, serial) from a working dump
  build     TEMPLATE -o OUT  (--from-dump OLD | --mac MAC --serial SN) [--date-code YYWW]
                                        produce a flashable image for a specific card

Only the Python 3 standard library is required.
"""
import argparse
import hashlib
import re
import struct
import sys
import zlib

MAGIC = 0x669955AA
CRC_RESIDUAL = 0x2144DF1C          # zlib.crc32(data + stored_crc) for a valid block
MCP_SRAM = 0x08000040              # load address of bootcode / MCP modules
MAC_SLOTS = 8                      # 2 ports x 4 Flex-10 functions
PLACEHOLDER_MAC = 0x024E564D0000   # 02:4e:56:4d:00:00 ("NVM"), locally administered
PLACEHOLDER_SN = "X"               # repeated to the template's serial length

# CRC-protected configuration blocks (offset, length, name)
CFG_REGIONS = [
    (0x000, 0x014, "bootstrap"),
    (0x014, 0x0EC, "directory"),
    (0x100, 0x350, "manuf_info port 0"),
    (0x450, 0x0F0, "feature_info port 0"),
    (0x640, 0x064, "upgrade_key_info"),
    (0x708, 0x070, "manuf_key_info"),
    (0x7E8, 0x350, "manuf_info port 1"),
    (0xB38, 0x0F0, "feature_info port 1"),
]
MAC_PORT0 = 0x13E                  # first MAC inside manuf_info port 0


def be32(d, o):
    return struct.unpack(">I", d[o:o + 4])[0]


def crc_ok(d, s, l):
    return l >= 4 and s + l <= len(d) and zlib.crc32(bytes(d[s:s + l])) == CRC_RESIDUAL


def fix_crc(d, s, l):
    d[s + l - 4:s + l] = struct.pack("<I", zlib.crc32(bytes(d[s:s + l - 4])))
    assert crc_ok(d, s, l)


def swap32(b):
    """Text in this NVRAM is stored as big-endian words; swap to read it."""
    b = bytes(b)
    return b"".join(b[i:i + 4][::-1] for i in range(0, len(b), 4))


def versions(b):
    found = re.findall(rb"(?<![0-9.])\d{1,2}\.\d{1,2}\.\d{1,3}(?![0-9.])", swap32(b) + bytes(b))
    return sorted({v.decode() for v in found})


def fmt_mac(v):
    return ":".join(f"{(v >> (8 * i)) & 0xFF:02x}" for i in range(5, -1, -1))


def parse_mac(s):
    h = re.sub(r"[^0-9a-fA-F]", "", s)
    if len(h) != 12:
        raise argparse.ArgumentTypeError(f"bad MAC address: {s}")
    return int(h, 16)


# --------------------------------------------------------------------------- images

class Image:
    def __init__(self, kind, sram, attr, nvm, length):
        self.kind, self.sram, self.attr, self.nvm, self.len = kind, sram, attr, nvm, length

    @property
    def type(self):
        return self.attr >> 24

    def contains(self, off):
        return self.nvm <= off < self.nvm + self.len


def images(d):
    out = [Image("bootcode", be32(d, 0x4), 0, be32(d, 0xC), be32(d, 0x8) * 4)]
    for o in range(0x14, 0x14 + 0xEC - 4 - 11, 12):
        sram, attr, nvm = be32(d, o), be32(d, o + 4), be32(d, o + 8)
        length = attr & 0x007FFFFC
        if attr == 0 or nvm == 0 or length == 0 or nvm + length > len(d):
            continue
        out.append(Image("main", sram, attr, nvm, length))
        if attr >> 28 == 0xE:                       # extended directory
            for i in range(be32(d, nvm)):
                p = nvm + 8 + i * 12
                s2, a2, n2 = be32(d, p), be32(d, p + 4), be32(d, p + 8)
                l2 = a2 & 0x007FFFFC
                if a2 and l2 and n2 + l2 <= len(d):
                    out.append(Image("ext", s2, a2, n2, l2))
    return out


def block_of(d, off, imgs):
    """Smallest CRC-protected block (config region or image) containing off."""
    cands = [(l, s) for s, l, _ in CFG_REGIONS if s <= off < s + l]
    cands += [(i.len, i.nvm) for i in imgs if i.contains(off) and i.kind != "bootcode"]
    if not cands:
        return None
    l, s = min(cands)
    return s, l


# --------------------------------------------------------------------------- VPD

def find_vpd(d):
    """Return list of (offset, fields, field_positions) for HP VPD blocks (word-swapped)."""
    b = swap32(d[:0x2000])
    res = []
    for m in re.finditer(rb"\x82", b):
        o = m.start()
        L = b[o + 1] | b[o + 2] << 8
        name = b[o + 3:o + 3 + L]
        if not name.startswith(b"HP") or b[o + 3 + L] != 0x90:
            continue
        fields, pos = {"name": name.rstrip(b"\0").decode(errors="replace")}, {}
        p = o + 3 + L
        L2 = b[p + 1] | b[p + 2] << 8
        q, end = p + 3, p + 3 + L2
        while q < end - 2:
            k, l = b[q:q + 2].decode(errors="replace"), b[q + 2]
            fields[k] = b[q + 3:q + 3 + l].decode(errors="replace")
            pos[k] = (q + 3, l)
            if k == "RV":
                break
            q += 3 + l
        res.append((o, fields, pos))
    return res


def card_vpd(d):
    """The per-card VPD: the one carrying a MAC (V4); the other is a generic stub."""
    for o, f, pos in find_vpd(d):
        if "V4" in f and "SN" in f:
            return o, f, pos
    sys.exit("error: no per-card HP VPD (with SN and V4) found")


def set_vpd_field(d, imgs, key, value):
    o, f, pos = card_vpd(d)
    if key not in pos:
        sys.exit(f"error: VPD field {key} not present in template")
    start, l = pos[key]
    if len(value) != l:
        sys.exit(f"error: VPD {key} must be {l} characters (got {len(value)}: {value!r})")
    sw = bytearray(swap32(d[0:0x2000]))
    sw[start:start + l] = value.encode()
    rv_off, _ = pos["RV"]                       # first RV byte is the checksum
    sw[rv_off] = (-sum(sw[o:rv_off])) & 0xFF
    assert sum(sw[o:rv_off + 1]) & 0xFF == 0
    d[0:0x2000] = swap32(sw)
    blk = block_of(d, o, imgs)
    if blk:
        fix_crc(d, *blk)


# --------------------------------------------------------------------------- identity

def identity(d):
    base = int.from_bytes(d[MAC_PORT0:MAC_PORT0 + 6], "big")
    _, f, _ = card_vpd(d)
    return {"mac": base, "serial": f.get("SN", ""), "date": f.get("V2", ""),
            "vpd_mac": f.get("V4", "")}


def replace_macs(d, imgs, old_base, new_base):
    patched = []
    for i in range(MAC_SLOTS):
        old = (old_base + i).to_bytes(6, "big")
        new = (new_base + i).to_bytes(6, "big")
        for m in re.finditer(re.escape(old), bytes(d)):
            off = m.start()
            if off % 4 != 2:                        # MACs live in the low 48 bits of a 64-bit pair
                continue
            blk = block_of(d, off, imgs)
            if blk is None:
                continue
            d[off:off + 6] = new
            patched.append((off, blk))
    for blk in {b for _, b in patched}:
        fix_crc(d, *blk)
    return patched


# --------------------------------------------------------------------------- checks

def check(d, verbose=True):
    ok_all = be32(d, 0) == MAGIC
    if verbose:
        print(f"Size: {len(d)} bytes, magic {be32(d, 0):#010x} {'OK' if ok_all else 'BAD'}")
        print("Configuration blocks:")
    for s, l, n in CFG_REGIONS:
        good = crc_ok(d, s, l)
        ok_all &= good
        if verbose:
            print(f"  {s:#07x} +{l:#06x}  {n:22} {'OK' if good else 'BAD CRC'}")
    imgs = images(d)
    if verbose:
        print(f"Images ({len(imgs)}):")
        print("  kind      type  load addr   offset    length    CRC  versions")
    for i in imgs:
        good = crc_ok(d, i.nvm, i.len)
        ok_all &= good
        if verbose:
            t = "--" if i.kind == "bootcode" else f"{i.type:02x}"
            print(f"  {i.kind:8}  {t:>4}  {i.sram:#010x}  {i.nvm:#08x}  {i.len:#08x}  "
                  f"{'OK ' if good else 'BAD'}  {' '.join(versions(d[i.nvm:i.nvm + i.len]))}")
    return ok_all, imgs


def mcp_consistency(d, imgs):
    boot = imgs[0]
    bv = set(versions(d[boot.nvm:boot.nvm + boot.len]))
    mods = [i for i in imgs if i.kind == "ext" and i.sram == MCP_SRAM and i.nvm != boot.nvm]
    mv = set()
    for i in mods:
        mv |= set(versions(d[i.nvm:i.nvm + i.len]))
    return bv, mv, len(mods)


# --------------------------------------------------------------------------- commands

def load(path):
    with open(path, "rb") as f:
        return bytearray(f.read())


def save(path, d):
    with open(path, "wb") as f:
        f.write(d)
    print(f"Written {path} ({len(d)} bytes), sha256 {hashlib.sha256(d).hexdigest()}")


def cmd_info(a):
    d = load(a.file)
    ok, imgs = check(d)
    print(f"PCI ID: {d[0x12C:0x12E].hex()}:{d[0x12E:0x130].hex()}  "
          f"subsystem {d[0x132:0x134].hex()}:{d[0x130:0x132].hex()}")
    print(f"Part number (manuf_info): {swap32(d[0x104:0x114]).decode(errors='replace')}")
    for o, f, _ in find_vpd(d):
        print(f"VPD @{o:#x}: " + ", ".join(f"{k}={v}" for k, v in f.items() if k != "RV"))
    idn = identity(d)
    print(f"Base MAC (manuf_info): {fmt_mac(idn['mac'])}  ..  {fmt_mac(idn['mac'] + MAC_SLOTS - 1)}")
    bv, mv, n = mcp_consistency(d, imgs)
    print(f"Bootcode version: {', '.join(sorted(bv)) or '?'};  MCP modules ({n}): {', '.join(sorted(mv)) or '?'}")
    if bv and mv and not (bv & mv):
        print("WARNING: bootcode and MCP modules come from different firmware releases.\n"
              "         A mixed set typically causes 'BAD MCP validity signature' in bnx2x.")
    print("RESULT:", "all checksums OK" if ok else "CHECKSUM ERRORS FOUND")
    return 0 if ok else 1


def cmd_sanitize(a):
    d = load(a.dump)
    ok, imgs = check(d, verbose=False)
    if not ok:
        sys.exit("error: source dump has checksum errors, refusing to make a template from it")
    idn = identity(d)
    replace_macs(d, imgs, idn["mac"], PLACEHOLDER_MAC)
    set_vpd_field(d, imgs, "V4", f"{PLACEHOLDER_MAC:012X}")
    set_vpd_field(d, imgs, "SN", PLACEHOLDER_SN * len(idn["serial"]))
    if idn["date"]:
        set_vpd_field(d, imgs, "V2", "0" * len(idn["date"]))   # donor card's date code
    leftover = bytes.fromhex(f"{idn['mac']:012x}")[:5]
    if leftover in d or idn["serial"].encode() in swap32(d) + bytes(d):
        sys.exit("error: identity still present after sanitizing - unknown layout, aborting")
    ok, _ = check(d, verbose=False)
    assert ok
    save(a.output, d)
    print(f"Template created: MAC -> {fmt_mac(PLACEHOLDER_MAC)}, SN -> {PLACEHOLDER_SN * len(idn['serial'])}, "
          f"date code -> {'0' * len(idn['date'])}")


def cmd_build(a):
    d = load(a.template)
    ok, imgs = check(d, verbose=False)
    if not ok:
        sys.exit("error: template has checksum errors")
    if a.from_dump:
        src = load(a.from_dump)
        idn = identity(src)
        mac, serial = idn["mac"], idn["serial"]
        date = a.date_code or idn["date"]
        print(f"Identity from {a.from_dump}: MAC {fmt_mac(mac)}, SN {serial}, date code {date}")
    else:
        if a.mac is None or not a.serial:
            sys.exit("error: give --from-dump, or both --mac and --serial")
        mac, serial, date = a.mac, a.serial, a.date_code
    if mac & 0xFF > 0xFF - (MAC_SLOTS - 1):
        sys.exit("error: base MAC too close to xx:ff, the 8 consecutive addresses would wrap")
    tmpl = identity(d)
    patched = replace_macs(d, imgs, tmpl["mac"], mac)
    set_vpd_field(d, imgs, "V4", f"{mac:012X}")
    set_vpd_field(d, imgs, "SN", serial)
    if date:
        set_vpd_field(d, imgs, "V2", date)
    ok, _ = check(d, verbose=False)
    if not ok:
        sys.exit("error: result failed checksum verification (not written)")
    print(f"MAC addresses written: {len(patched)} ({fmt_mac(mac)} .. {fmt_mac(mac + MAC_SLOTS - 1)})")
    save(a.output, d)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("info", help="analyse an NVRAM image")
    p.add_argument("file")
    p.set_defaults(fn=cmd_info)
    p = sub.add_parser("sanitize", help="make a template from a working dump")
    p.add_argument("dump")
    p.add_argument("-o", "--output", required=True)
    p.set_defaults(fn=cmd_sanitize)
    p = sub.add_parser("build", help="build an image for a specific card from a template")
    p.add_argument("template")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--from-dump", help="take MAC/serial/date code from this card's own (broken) dump")
    p.add_argument("--mac", type=parse_mac, help="base MAC (port 0, function 0), e.g. aa:bb:cc:dd:ee:00")
    p.add_argument("--serial", help="serial number, same length as in the template (HP: 10 chars)")
    p.add_argument("--date-code", help="VPD V2 date code (4 chars, YYWW)")
    p.set_defaults(fn=cmd_build)
    a = ap.parse_args()
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
