# HP 534FLR-SFP+ (BCM57810) NVRAM recovery

**English** · [Polski](docs/README.pl.md) · [Українська](docs/README.uk.md) · [Русский](docs/README.ru.md)

Tools and instructions for recovering HP FlexFabric 10Gb 2-port **534FLR-SFP+** adapters (Broadcom/QLogic **BCM57810**, `14e4:168e`, subsystem `103c:1930`) whose SPI flash contains a broken firmware set. The method also applies to other BCM57810/bnx2x adapters with the same NVRAM layout.

This repository contains:

| Path | Contents |
|---|---|
| `firmware/esp-serprog/` | Firmware that turns an **ESP32 / ESP8266** into an SPI flash programmer for [flashrom](https://flashrom.org) (`serprog` protocol) |
| `tools/nvmtool.py` | Image tool: analyse a dump, make a template without card identity, build an image for a given card (Python 3, no dependencies) |
| `tools/bnx2x_nvm.py` | In-system NVRAM access from Linux through the adapter's PCI BAR0: MCP trace, read, write (no desoldering) |
| `templates/` | A sanitized template image for 534FLR-SFP+ (firmware 7.14.62, no card identity) |

> The template in `templates/` contains no card identity (MACs, serial number). Original dumps of real cards are never stored here: each contains the serial number and MAC addresses of a specific card. See [Getting a template](#5-getting-a-template).

---

## 1. Symptoms

The adapter is not visible to iLO and the OS, and `dmesg` shows:

```
bnx2x: [bnx2x_init_shmem:...]BAD MCP validity signature
bnx2x 0000:05:00.0: part number 394D4342-31383735-31543030-47303030
bnx2x: [bnx2x_get_igu_cam_info:...]CAM configuration error
bnx2x 0000:05:00.0: probe with driver bnx2x failed with error -22
pci 0000:05:00.0: VPD access failed.  This is likely a firmware bug on this device.
```

The management CPU (MCP) in the BCM57810 starts its bootcode from the SPI flash but never completes initialisation. Its own trace buffer (`bnx2x_nvm.py trace`, section 3) shows why:

```
MFW1 7.16.15 Begin, TS 0x965d72 SI 0x8020458.
...
no img 0x30000003
load failure
```

In the case that led to this project, the card was broken by an **interrupted HPE firmware update**: *HPE QLogic NX2 Online Firmware Upgrade Utility* (CP064333, MBI 7.14.79 → 7.19.27) wrote the new bootcode 7.16.15, removed the old MCP modules and then failed (`Return code: 7`) before writing the new ones. Every image in the flash still had a valid CRC, but the bootcode did not find the module set it requires. `nvmtool.py info` detects this situation. The fix is to write a complete, consistent firmware set from a working card while keeping this card's own MAC addresses and serial number.

> ⚠️ The same update failed the same way a second time on the same server (Debian 13 / Proxmox VE, kernel 7.0 — not a supported OS). After recovery, do **not** run that NX2 firmware component there again.

If the card is still visible on the PCI bus, it can be recovered **from Linux without removing the flash chip** (section 3). Otherwise the chip has to be removed and programmed with an external programmer (section 4).

## 2. The flash chip: M45PE16

- Marking: **4SPE16**, ST/Micron **M45PE16**, 16 Mbit (2 MB), SO8W (208 mil), 2.7–3.6 V.
- JEDEC ID: `0x20 0x40 0x15`.

⚠️ **The M45PE16 pinout is NOT the standard 25-series pinout.** Every standard SOIC8 adapter, clip and socket wiring is wrong for this chip:

```
          ┌───────┐
    D   1 │●      │ 8  Q       D  = data in (MOSI)      Q   = data out (MISO)
    C   2 │       │ 7  VSS     C  = clock (SCK)         VSS = ground
 Reset  3 │       │ 6  VCC     S  = chip select (CS#)   VCC = 3.3 V
    S   4 │       │ 5  W       W  = write protect       Reset = reset, active low
          └───────┘
```

Reset (pin 3) **must** be held high (3.3 V), otherwise the chip stays in reset and reads back `0xFF`/`0x00`.

The M45PE family also has **no Bulk/Chip Erase command** (only Page Erase `0xDB` and Sector Erase `0xD8`). This matters for the choice of programmer.

## 3. In-system recovery from Linux (no desoldering)

When `lspci -nn -d 14e4:168e` still shows the card but the driver fails, its NVRAM can be read and written through the adapter's own NVRAM interface in PCI BAR0, with the same register sequence the bnx2x driver uses. `tools/bnx2x_nvm.py` needs only Python 3 and root. The function must not be bound to a driver; after a failed probe it is not.

```
B=0000:05:00.0                                    # PCI address of function 0

python3 tools/bnx2x_nvm.py $B trace               # MCP state and trace buffer: why it stopped
python3 tools/bnx2x_nvm.py $B read backup1.bin    # whole 2 MB NVRAM, about 2 s
python3 tools/bnx2x_nvm.py $B read backup2.bin
cmp backup1.bin backup2.bin
python3 tools/nvmtool.py info backup1.bin
```

Build the image for your card (section 6), then write it:

```
python3 tools/bnx2x_nvm.py $B testpage 0x180000   # write test on one page, then restores it
python3 tools/bnx2x_nvm.py $B write my_card_new.bin
python3 tools/bnx2x_nvm.py $B read check.bin
sha256sum my_card_new.bin check.bin               # must be identical
```

- `write` rewrites only the 256-byte pages that differ (typically a few hundred, a few seconds) and then reads back and compares the whole flash.
- The adapter's NVRAM controller erases every page itself, so this works with the M45PE16 even though the TL866II+ cannot write it.
- After writing, **shut the server down and remove AC power for 30–60 s**. The MCP runs on standby power and reloads its bootcode only after a full power cycle.
- If a write is interrupted, the card may disappear from the PCI bus; then use an external programmer (section 4) with the backup.

## 4. Programmers

### 4.1 TL866II+ (minipro): read only

The TL866II+ can **read** the chip, but **cannot write it**: its firmware erases SPI flash only with Bulk Erase (`0xC7`), which the M45PE16 ignores. minipro still reports `Erasing... OK`, then writes over unerased data and verification fails. The chip ends up holding `old AND new` data. The `M45PE16` profile in minipro's database is only defined for T48/T56; there is no TL866II+ algorithm for it, and a firmware update does not add one.

To read with the TL866II+, wire the chip to the positions a standard 25-series SOIC8 chip would occupy in the ZIF socket (top of the socket, lever side):

| M45PE16 pin | Signal | ZIF contact | (standard flash position) |
|---|---|---|---|
| 4 | S | 1 | CS |
| 8 | Q | 2 | DO |
| 5 | W | 3 | WP |
| 7 | VSS | 4 | GND |
| 1 | D | 37 | DI |
| 2 | C | 38 | CLK |
| 3 | Reset | 39 | HOLD (driven high) |
| 6 | VCC | 40 | VCC (3.3 V) |

```
minipro -p "M45PE16@SOIC8" -D            # expect: Chip ID: 0x204015  OK
minipro -p "M45PE16@SOIC8" -r dump.bin
```

### 4.2 ESP32 / ESP8266 + flashrom: read and write ✅

flashrom supports the M45PE16 (sector erase included) and talks to the ESP over USB-UART using the `serprog` protocol.

#### Building and flashing the ESP firmware

Requires [PlatformIO](https://platformio.org):

```
cd firmware/esp-serprog
pio run -e esp32   -t upload --upload-port /dev/ttyUSB0     # ESP32 DevKit
pio run -e esp8266 -t upload --upload-port /dev/ttyUSB0     # NodeMCU / Wemos D1 mini
```

Check that flashrom sees the programmer (no chip connected yet):

```
flashrom -p serprog:dev=/dev/ttyUSB0:921600
# serprog: Programmer name is "esp-serprog"
```

`Warning: NAK to query serial buffer size` is expected and harmless.

#### Wiring (directly to the chip's own pins)

| M45PE16 pin | Signal | ESP32 | ESP8266 (NodeMCU) |
|---|---|---|---|
| 1 | D (MOSI) | GPIO23 | D7 (GPIO13) |
| 2 | C (SCK) | GPIO18 | D5 (GPIO14) |
| 3 | Reset | **3V3** | **3V3** |
| 4 | S (CS) | GPIO5 | D1 (GPIO5) |
| 5 | W | **3V3** | **3V3** |
| 6 | VCC | **3V3** | **3V3** |
| 7 | VSS | GND | GND |
| 8 | Q (MISO) | GPIO19 | D6 (GPIO12) |

- Use **3V3 only**, never 5V/VIN.
- Keep wires short (≤ 10–15 cm). Default SPI clock is 4 MHz (capped at 8 MHz).
- Unplug USB while wiring.
- The ESP32 build was tested on real hardware with an M45PE16. The ESP8266 build compiles but has not been tested on hardware.

#### Reading and writing

```
P="serprog:dev=/dev/ttyUSB0:921600"

flashrom -p $P -c M45PE16                       # probe: Found ... "M45PE16"
flashrom -p $P -c M45PE16 -r backup1.bin        # read (≈30 s)
flashrom -p $P -c M45PE16 -r backup2.bin
cmp backup1.bin backup2.bin && echo "reads are stable"

flashrom -p $P -c M45PE16 -w new.bin            # erase + write + verify (≈3 min)
flashrom -p $P -c M45PE16 -r check.bin
sha256sum new.bin check.bin                     # must be identical
```

If the probe returns `id1 0xff` (MISO pulled up, chip silent), check power and Reset on pins 6/3, and the wiring. `0xff` on the TL866 too usually means the wiring is wrong.

## 5. Getting a template

You need a full 2 MB NVRAM dump of a **working** card of the same model (same PCI subsystem ID, `103c:1930` for 534FLR-SFP+). On a working card in a Linux server:

```
ethtool -e <iface> raw on > working_dump.bin      # 2097152 bytes
```

or read its flash chip with a programmer as described above.

### Included template

`templates/534FLR-SFP+_fw7.14.62_template.bin` is ready to use. It is a sanitized dump of a working 534FLR-SFP+ (`103c:1930`): bootcode 7.14.62 with a complete, consistent MCP module set (7.14.62/7.14.37), MBA 7.14.10. All checksums are valid. The card identity is replaced with placeholders: MAC `02:4e:56:4d:00:00`…`07`, SN `XXXXXXXXXX`, date code `0000`. Check its integrity with `sha256sum -c templates/SHA256SUMS`.

> The template contains proprietary HP/QLogic firmware. It is provided only to recover your own hardware.

### Making your own template

Turn a working dump into a template without that card's identity:

```
python3 tools/nvmtool.py sanitize working_dump.bin -o template.bin
# MAC -> 02:4e:56:4d:00:00, SN -> XXXXXXXXXX, date code -> 0000
```

## 6. Building an image for your card

Always read and keep the original content of your chip first (sections 3–4), and analyse it:

```
python3 tools/nvmtool.py info my_card_original.bin
```

`info` checks the magic, every CRC-protected configuration block, the main and extended directories and every image. It prints VPD, PCI IDs and MAC addresses, and warns when the bootcode and MCP modules come from different firmware releases.

Build an image with your card's identity:

```
# identity (MACs, serial number, date code) taken from your card's own dump
python3 tools/nvmtool.py build template.bin --from-dump my_card_original.bin -o my_card_new.bin

# or given explicitly (e.g. from the label on the card)
python3 tools/nvmtool.py build template.bin --mac aa:bb:cc:dd:ee:00 --serial CN0000ABCD --date-code 1234 -o my_card_new.bin
```

What `build` does:

- replaces the 8 consecutive MAC addresses (2 ports × 4 Flex-10 functions) everywhere they occur: manuf_info of both ports and the multi-function table (Ethernet, iSCSI and FCoE entries);
- writes the serial number (SN), MAC (V4) and optionally the date code (V2) into the card VPD and recalculates the VPD checksum (RV);
- recalculates the CRC of every block it changed and re-verifies the whole image before writing it.

`--mac` is the base MAC address: port 0, function 0, the lowest of the eight (VPD field V4). The serial number must have the same length as in the template (10 characters for HP).

The firmware versions and configuration come from the template. Version fields V1/V3/V6 in VPD therefore describe the template's firmware, which is correct.

## 7. Full recovery procedure

1. Back up the flash twice and compare: from Linux with `bnx2x_nvm.py read` (section 3), or with a programmer after removing the chip (section 4).
2. `nvmtool.py info backup.bin` and `bnx2x_nvm.py trace`: confirm the diagnosis.
3. Take the included template or make one from a working card (`sanitize`), then `nvmtool.py build ... --from-dump backup.bin`.
4. Write the image: `bnx2x_nvm.py write` in the OS, or ESP + flashrom. Read back and compare SHA-256.
5. If the chip was removed, solder it back (pin 1 = dot, match the PCB marking).
6. Remove AC power from the server for 30–60 s, then boot.
7. `dmesg | grep -iE 'bnx2x|MCP'` must show no `BAD MCP validity signature`, and both ports must appear with your MAC addresses.

Update firmware later only with HPE tools on a supported OS, never with `ethtool -f`, and dump the flash before every update.

## Disclaimer

Use at your own risk. Writing a wrong image, a wrong voltage or wrong wiring can permanently damage the flash chip or the adapter. Keep the original dump of your chip.

HP, HPE, FlexFabric, Broadcom and QLogic are trademarks of their respective owners. This project is not affiliated with them.

## License

[MIT](LICENSE)
