# P65

**P65** is a small handheld computer: a Raspberry Pi Pico (RP2040) drives a real WDC W65C02S
and emulates a TMS9918 video chip on an ILI9341 LCD. This repository holds the **firmware** (`firmware/`) and the **desktop emulator**
of the P65 (Python + Qt6, `emulator/`); the hardware files are not included yet.

## Run it

Python 3.10+ is needed.

```
pip install -r emulator/requirements.txt
python emulator/p65emu.py
```

Useful options:

```
python emulator/p65emu.py --rom my_rom.bin       # ROM image loaded at $8100 (32512 bytes)
python emulator/p65emu.py --type program.bas     # types a BASIC program in after start-up
```

Press **F1** in the emulator for the keyboard layout and shortcuts.

## What is in it

* WDC 65C02 core (RMB/SMB/BBR/BBS, WAI, STP, decimal mode), validated with Klaus Dormann's test suites;
* TMS9918 (all modes, sprites, 60 Hz VSYNC interrupt on /IRQ) with a VDP viewer (tiles, sprites, VRAM map);
* debugger with colored disassembly, breakpoints, a scrollable memory view and register highlighting;
* flash storage like the real device (same format, in pure Python) (`SAVE`, `LOAD`, `BSAVE`, `BLOAD`, `BRUN`, `CATALOG`, ...) and a
  storage manager (Ctrl+Shift+T) that copies files to and from the real P65 over USB.

## Memory map

| address | |
|---|---|
| `$0000-$7FFF` | RAM |
| `$8000` | LEDs (bits 0-2) |
| `$8001` | keyboard |
| `$8002 / $8003` | VDP data / control and status |
| `$8004` | ROM control: write 1 = ROM off (RAM at `$8100-$FFFF`), 0 = ROM on |
| `$8010-$802F` | storage mailbox |
| `$8100-$FFFF` | ROM (BASIC at `$C000`, monitor at `$F000`, vectors at `$FFFA`) |

## Notes

* **Storage** is the firmware's file system, mailbox and USB protocol ported to pure Python
  (`emulator/p65fs.py`). No compiler, DLL or extra library is needed, so everything runs on Windows,
  Linux and macOS. It keeps the real device's on-flash format, so a storage image can be moved between the
  emulator and the device.
* `emulator/rom/p65_basic.bin` is the BASIC ROM of the P65. Its interpreter descends from
  [mist64/msbasic](https://github.com/mist64/msbasic) (Microsoft BASIC), whose copyright belongs to
  its original authors; the MIT license of this repository covers the P65 code only.

## License

MIT, see [LICENSE](LICENSE).
