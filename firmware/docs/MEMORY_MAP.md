# P65: memory map and I/O registers

The 65C02 sees 64 KB. All of it lives in one array in the Pico (`cpu_memory[]`);
there is no address decoder in hardware, so the map below is decided purely by
the firmware (core 1 bus loop, `src/65c02.c`, `include/mapper.h`).
Names come from `include/mapper.h`, `emulator/memmap.py` and the equates at the
top of `rom/p65_basic.a65` - keep the three in sync.

## 1. Map

| Address        | What |
|----------------|------|
| `$0000-$7FFF`  | RAM, 32 KB (zero page, stack, BASIC, programs) |
| `$8000-$80FF`  | I/O page (registers below; unused offsets read the stored byte, writes ignored) |
| `$8100-$FFFF`  | ROM image (BASIC, 32512 B), writes ignored - **or RAM when ROMCTL bit 0 = 1** (section 3) |

## 2. Registers (offset from `$8000`)

| Addr    | Name    | Access | Meaning |
|---------|---------|--------|---------|
| `$8000` | LEDS    | W: bits 0-2 = LED0..2 (1 = on); R: `$F8` or leds | the three LEDs |
| `$8001` | KEY     | R: bit 7 = key waiting, bits 0-6 = ASCII; W: any value acknowledges | keyboard (8-char queue in the Pico) |
| `$8002` | VDATA   | R/W | TMS9918 data port, VRAM address auto-increments |
| `$8003` | VCTRL   | W: address / register; R: status | TMS9918 control / status. Status read clears flags and releases /IRQ; bit 7 = 60 Hz frame flag. See `VSYNC.md` |
| `$8004` | ROMCTL  | W: bit 0; R: bit 0, bit 7 | ROM on/off, see section 3 |
| `$8010-$802F` | STORAGE | mailbox | flash file system, see `STORAGE.md` |
| other   |         | | unused |

## 3. ROMCTL (`$8004`): switching the ROM off

Gives the program 32 KB more RAM at `$8100-$FFFF`.

| Bit | Write | Read |
|-----|-------|------|
| 0   | `1` = ROM off (area above the I/O page is RAM), `0` = ROM on (image restored) | current state (1 = ROM off) |
| 7   | - | **busy**: the ROM image is being copied back, do not run ROM code yet |

* Switching off changes nothing in memory: the ROM image stays at `$8100-$FFFF`
  until the program overwrites it, so ROM routines can still be called as long as
  their bytes are intact.
* Switching on makes core 0 copy the 32 KB image back (about 0.3 ms, estimate,
  not measured). Poll bit 7 until it is clear.
* `$8000-$80FF` stays I/O in both modes, so RAM is split: `$0000-$7FFF` and
  `$8100-$FFFF`.
* With the ROM off, `$FFFA-$FFFF` (NMI / RESET / IRQ vectors) are RAM too:
  **the program must write its own vectors**, and an IRQ before that jumps
  into garbage. Keep interrupts disabled (`SEI`) across the switch.
* Storage `LOAD` / `BLOAD` / `BRUN` may write above `$8100` while the ROM is off
  (otherwise such a load is refused with "address"). A file must not cross the
  I/O page.
* Not restored by: Ctrl+= and the CPU reset done by `main.c` (only at power up).
  If a program with the ROM off hangs, power-cycle the Pico.

Exit to BASIC from a program that switched the ROM off:

```asm
        sei
        stz $8004           ; ROM on
wait:   lda $8004
        bmi wait            ; bit 7 = still copying
        jmp ($FFFC)         ; cold start; or jump to the BASIC warm start
```

Switching off:

```asm
        sei
        lda #1
        sta $8004           ; ROM off
        ; ... write your vectors at $FFFA-$FFFF, IRQ handler, etc. ...
        cli                 ; only if your IRQ vector is set up
```

Status: emulator tested (`tests/test_romctl.py`); firmware builds, **not tested on
the board** at the time of writing.

## 4. Where this is implemented

* Firmware: `include/mapper.h` (map, `rom_ram`), `src/mapper.c` (`romctl_write`,
  `mapper_rom_poll`), `src/65c02.c` (write path), `src/video.c` (calls the poll).
* Emulator: `emulator/memmap.py`, `emulator/machine.py` (`_romctl_write`,
  `_update_rom`), `emulator/cpu65c02.py`.
