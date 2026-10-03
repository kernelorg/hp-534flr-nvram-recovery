#!/usr/bin/env python3
"""
bnx2x_nvm - in-system access to the NVRAM (SPI flash) of Broadcom/QLogic BCM57810 (bnx2x)
adapters through PCI BAR0, for cards the bnx2x driver cannot attach to
("BAD MCP validity signature"). Uses the same NVRAM interface and sequence as the driver
(bnx2x_nvram_read / bnx2x_nvram_write), so no chip desoldering is needed.

Usage (as root; BDF = PCI address of function 0, e.g. 0000:05:00.0):
  bnx2x_nvm.py BDF trace              print the management CPU (MCP) trace buffer
  bnx2x_nvm.py BDF read OUT.bin       read the whole 2 MB NVRAM
  bnx2x_nvm.py BDF testpage OFFSET    write a test pattern to one 256-byte page, verify, restore
  bnx2x_nvm.py BDF write IMAGE.bin    write an image (only differing pages), then verify everything

The adapter must not be bound to a driver. After writing, power-cycle the server
(remove AC power) so the MCP reloads its bootcode.
"""
import mmap
import os
import struct
import sys
import time

MISC_REG_SHARED_MEM_ADDR = 0xA2B4
MCP_REG_MCPR_CPU_PROGRAM_COUNTER = 0x8501C
NVM_COMMAND, NVM_WRITE, NVM_ADDR, NVM_READ = 0x86400, 0x86408, 0x8640C, 0x86410
NVM_SW_ARB, NVM_ACCESS_ENABLE = 0x86420, 0x86424
CMD_DONE, CMD_DOIT, CMD_WR, CMD_FIRST, CMD_LAST = 1 << 3, 1 << 4, 1 << 5, 1 << 7, 1 << 8
ARB_REQ_SET1, ARB_REQ_CLR1, ARB_ARB1 = 1 << 1, 1 << 5, 1 << 9
ACCESS_EN_WR_EN = 0x3
MCP_TRACE_SIGNATURE = 0x54524342       # "TRCB"
NVM_MAGIC = 0x669955AA
FLASH_SIZE = 0x200000
PAGE = 256
PORT = 0
BAR0_SIZE = 0x800000


class Dev:
    def __init__(self, bdf, write=False):
        sysfs = f"/sys/bus/pci/devices/{bdf}"
        if not os.path.isdir(sysfs):
            sys.exit(f"error: no PCI device {bdf}")
        if os.path.exists(f"{sysfs}/driver"):
            drv = os.path.basename(os.readlink(f"{sysfs}/driver"))
            sys.exit(f"error: {bdf} is bound to driver '{drv}'; unbind it first "
                     f"(echo {bdf} > /sys/bus/pci/drivers/{drv}/unbind)")
        fd = os.open(f"{sysfs}/resource0", (os.O_RDWR if write else os.O_RDONLY) | os.O_SYNC)
        prot = mmap.PROT_READ | (mmap.PROT_WRITE if write else 0)
        self.m = mmap.mmap(fd, BAR0_SIZE, mmap.MAP_SHARED, prot)

    def rd(self, o):
        return struct.unpack("<I", self.m[o:o + 4])[0]

    def wr(self, o, v):
        self.m[o:o + 4] = struct.pack("<I", v & 0xFFFFFFFF)

    def poll(self, reg, mask, want, tries=200000):
        for _ in range(tries):
            if (self.rd(reg) & mask) == want:
                return True
        return False

    def lock(self):
        self.wr(NVM_SW_ARB, ARB_REQ_SET1 << PORT)
        if not self.poll(NVM_SW_ARB, ARB_ARB1 << PORT, ARB_ARB1 << PORT):
            raise RuntimeError("cannot get NVRAM arbitration")
        self.wr(NVM_ACCESS_ENABLE, self.rd(NVM_ACCESS_ENABLE) | ACCESS_EN_WR_EN)

    def unlock(self):
        self.wr(NVM_ACCESS_ENABLE, self.rd(NVM_ACCESS_ENABLE) & ~ACCESS_EN_WR_EN)
        self.wr(NVM_SW_ARB, ARB_REQ_CLR1 << PORT)
        self.poll(NVM_SW_ARB, ARB_ARB1 << PORT, 0)

    def _cmd(self, off, flags):
        self.wr(NVM_ADDR, off & 0xFFFFFF)
        self.wr(NVM_COMMAND, flags | CMD_DOIT)
        if not self.poll(NVM_COMMAND, CMD_DONE, CMD_DONE):
            raise RuntimeError(f"NVRAM timeout at {off:#x}")

    def read(self, off, size, chunk=0x1000):
        out = bytearray()
        self.lock()
        try:
            for base in range(off, off + size, chunk):
                n = min(chunk, off + size - base)
                for i in range(0, n, 4):
                    self.wr(NVM_COMMAND, CMD_DONE)
                    self._cmd(base + i, (CMD_FIRST if i == 0 else 0) | (CMD_LAST if i == n - 4 else 0))
                    out += struct.pack(">I", self.rd(NVM_READ))
        finally:
            self.unlock()
        return bytes(out)

    def write_pages(self, data, base, pages):
        """Write whole pages, FIRST..LAST per page; the NVRAM controller erases the page itself."""
        for pg in pages:
            self.lock()
            try:
                for i in range(0, PAGE, 4):
                    off = pg + i
                    self.wr(NVM_COMMAND, CMD_DONE)
                    self.wr(NVM_WRITE, struct.unpack(">I", data[off - base:off - base + 4])[0])
                    self._cmd(off, CMD_WR | (CMD_FIRST if i == 0 else 0) | (CMD_LAST if i == PAGE - 4 else 0))
            finally:
                self.unlock()


def cmd_trace(bdf):
    d = Dev(bdf)
    shm = d.rd(MISC_REG_SHARED_MEM_ADDR)
    pcs = {d.rd(MCP_REG_MCPR_CPU_PROGRAM_COUNTER) for _ in range(8)}
    print(f"shmem base {shm:#x}, MCP PC {', '.join(hex(p) for p in sorted(pcs))}"
          f"{' (stuck)' if len(pcs) == 1 else ' (running)'}")
    tb = shm - 0x800
    if not 0xA0000 <= tb < 0xC0000 or d.rd(tb) != MCP_TRACE_SIGNATURE:
        sys.exit("no MCP trace buffer found")
    p = d.rd(tb + 4)
    mark = 0xA0000 + ((p + 3) & ~3) - 0x08000000
    words = list(range(mark, shm, 4)) + list(range(tb + 8, mark, 4))
    txt = b"".join(d.m[x:x + 4][::-1] for x in words).replace(b"\0", b"").decode("latin1")
    start = txt.find("MFW")
    print(txt[start:] if start >= 0 else txt)


def cmd_read(bdf, out):
    t = time.time()
    data = Dev(bdf, write=True).read(0, FLASH_SIZE)   # control registers are written to issue reads
    with open(out, "wb") as f:
        f.write(data)
    print(f"read {len(data)} bytes in {time.time() - t:.1f}s -> {out}")


def cmd_testpage(bdf, off):
    if off % PAGE:
        sys.exit("error: offset must be 256-byte aligned")
    d = Dev(bdf, write=True)
    orig = d.read(off, PAGE)
    pat = bytes((i * 37 + 0x5A) & 0xFF for i in range(PAGE))
    d.write_pages(pat, off, [off])
    got = d.read(off, PAGE)
    d.write_pages(orig, off, [off])
    back = d.read(off, PAGE)
    print("pattern", "OK" if got == pat else "MISMATCH", "| restore", "OK" if back == orig else "MISMATCH")
    return 0 if got == pat and back == orig else 1


def cmd_write(bdf, path):
    img = open(path, "rb").read()
    if len(img) != FLASH_SIZE or struct.unpack(">I", img[:4])[0] != NVM_MAGIC:
        sys.exit(f"error: {path} is not a 2 MB bnx2x NVRAM image")
    d = Dev(bdf, write=True)
    cur = d.read(0, FLASH_SIZE)
    pages = [p for p in range(0, FLASH_SIZE, PAGE) if cur[p:p + PAGE] != img[p:p + PAGE]]
    print(f"{len(pages)} of {FLASH_SIZE // PAGE} pages differ, writing...")
    t = time.time()
    d.write_pages(img, 0, pages)
    after = d.read(0, FLASH_SIZE)
    bad = [p for p in range(0, FLASH_SIZE, PAGE) if after[p:p + PAGE] != img[p:p + PAGE]]
    print(f"written in {time.time() - t:.1f}s; verify:",
          "OK" if not bad else f"{len(bad)} BAD pages, first at {bad[0]:#x}")
    if not bad:
        print("Now power-cycle the server (remove AC power for 30 s) so the MCP reloads its bootcode.")
    return 0 if not bad else 1


def main():
    a = sys.argv[1:]
    if len(a) < 2 or a[1] not in ("trace", "read", "testpage", "write") or (a[1] != "trace" and len(a) < 3):
        sys.exit(__doc__)
    bdf, cmd = a[0], a[1]
    if os.geteuid() != 0:
        sys.exit("error: run as root")
    if cmd == "trace":
        return cmd_trace(bdf)
    if cmd == "read":
        return cmd_read(bdf, a[2])
    if cmd == "testpage":
        return cmd_testpage(bdf, int(a[2], 0))
    return cmd_write(bdf, a[2])


if __name__ == "__main__":
    sys.exit(main() or 0)
