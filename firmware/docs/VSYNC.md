# P65: VSYNC and the frame interrupt

How to get a steady 60 Hz game tick on the P65 from 65C02 assembly.
Everything here was checked on the board or in the emulator; where a number
is an estimate it says so.

## 1. In short

* The VDP "frame flag" (**bit 7 of the status register at `$8003`**) is set
  **exactly 60 times a second** by a hardware timer in the Pico. It does not
  depend on the LCD refresh and it is also set while the display is blanked.
* If **bit 5 of VDP register 1** is set, the same event pulls the CPU's
  **/IRQ** pin low. It stays low until you **read `$8003`**.
* Reading `$8003` clears all status flags and releases /IRQ - but the release
  is **not instantaneous** (see 4). Your handler **will sometimes be entered
  again with bit 7 clear** - ignore those entries. This is the one rule you
  must follow.
* There is no vertical-blank period to wait for: VRAM can be written at any
  time and the VDP has no wait states. VSYNC is a *clock tick*, not a raster
  position (see 5).

## 2. What happens, step by step

```
Pico timer, every 16.667 ms
   -> VDP status bit 7 (F) = 1
   -> if (F and R1 bit 5):  Pico drives /IRQ low (through the I2C port expander)
   -> 65C02 finishes its instruction, enters your IRQ handler
   -> handler reads $8003: F (and 5S, C) cleared
   -> Pico notices the cleared flag and releases /IRQ (about 150-170 us later)
```

Numbers, 65C02 at 2.000 MHz:

| | |
|---|---|
| frame period | 16.667 ms = **33 333 CPU cycles** |
| F set -> /IRQ low | up to ~100 us (estimate: one I2C write, possibly queued behind another) |
| status read -> /IRQ high | ~150-170 us (measured indirectly on the board, see 4) |
| extra empty handler entries per frame | **7-8** (measured on the board with the full firmware: LCD streaming and keyboard scan running) |
| cost of those | ~8 x 21 us = ~170 us per frame, **about 1 % of the CPU** |

Interrupt timing therefore has a few tens of microseconds of jitter. Do not
use it for cycle-exact effects.

## 3. The registers

`$8002` VDP data, `$8003` VDP status (read) / address and register (write).

**Enable the interrupt** - write the value, then `$80 | register number`:

```
        lda #$e0        ; R1 = 16K + display on + IRQ enable  (Graphics I / II)
        sta $8003
        lda #$81        ; ... into register 1
        sta $8003
```

`$E0` assumes Graphics I/II. In general: **OR `$20` into whatever R1 value your
mode already uses** (e.g. text mode `$D0` becomes `$F0`). Clearing bit 5
disables the interrupt, the frame flag itself keeps being set.

**Status register `$8003` (read-only, reading clears it):**

| bit | name | meaning |
|---|---|---|
| 7 | F | frame flag: set 60 times a second by the timer |
| 6 | 5S | fifth sprite on a line (set by the renderer while it draws) |
| 5 | C | sprite collision (set by the renderer while it draws) |
| 4-0 | | number of the fifth sprite (valid when 5S is set) |

The 5S and C flags are produced by the line renderer, not by the VSYNC timer,
and they stay latched until the status register is read. Because **a read
clears everything**, whichever code reads `$8003` must keep the bits it needs.
That matters because of the empty handler entries (next section): they read the
register too, and may be the ones that pick up a C flag.

## 4. Writing the handler

Rules:

1. **Read `$8003` first, always.** That is what releases /IRQ.
2. **Test bit 7.** If it is clear this is an "empty" entry caused by the
   delayed release: do nothing and `RTI`.
3. **Accumulate the flag bits from every read** (the example below does it
   with `TSB`), so a collision seen by an empty entry is not lost.
4. Save and restore every register you use. The 65C02 only pushes P and PC.
5. Keep it short (well under a millisecond). It runs 60 times a second.
6. **Do not touch `$8002` / write `$8003` in the handler if the main program
   also uses the VDP.** The VRAM address pointer and the two-byte
   address/register write latch are shared; the interrupt would corrupt a
   transfer in progress and you cannot read the address back. Either do all
   VDP access in the handler, all in the main program, or wrap main-program
   VDP sequences in `SEI` ... `CLI`.
7. Reading `$8003` in this implementation does **not** reset the address
   latch (the real TMS9918 does). Do not rely on either behaviour.

Complete skeleton (ca65; assembled and run in the emulator, 120 interrupts and
119 game frames in 2 s as expected):

```
.setcpu "W65C02"                ; WAI/STP need the WDC variant in ca65

frames    = $00                 ; incremented once per frame by the handler
vdp_flags = $01                 ; F/5S/C collected by the handler

.org $8100
reset:
        sei
        cld
        ldx     #$ff
        txs
        stz     frames
        stz     vdp_flags
        ; ... set up VDP mode, tables, sprites here ...
        lda     #$e0            ; R1: enable the frame interrupt (see above)
        sta     $8003
        lda     #$81
        sta     $8003
        lda     $8003           ; discard a stale frame flag
        cli

main:   lda     frames          ; wait for the next frame
@wait:  wai                     ; sleep until an interrupt arrives
        cmp     frames
        beq     @wait           ; empty entry (or other IRQ): keep waiting

        ; ---------- one game frame ----------
        sei                     ; take the flags the handler collected
        lda     vdp_flags       ;   (A: bit 5 = collision, bit 6 = 5th sprite)
        stz     vdp_flags
        cli
        ; ... game logic, VDP updates ...
        bra     main

irq:
        pha
        lda     $8003           ; read + clear status, releases /IRQ
        and     #$e0            ; keep F, 5S, C
        tsb     vdp_flags       ; accumulate; TSB leaves the N flag alone
        bpl     done            ; bit 7 (F) clear: empty entry
        inc     frames
done:
        pla
        rti

.res    $FFFA-*
.word   reset, reset, irq       ; NMI, RESET, IRQ/BRK
```

If your game logic can take longer than one frame, `frames` will have advanced
by more than one: compare the new value with the one you saw last time to
count the frames you missed (the F flag itself is a single bit).

### Polling instead of interrupts

If you do not want an interrupt:

```
@vsync: bit     $8003           ; reads (and clears) the status
        bpl     @vsync          ; wait for bit 7
```

A late poll still sees the flag (it stays set until read) but you cannot
tell how many frames went by. Remember that this read also clears 5S and C.

## 5. What VSYNC is not

* The LCD is updated by the other Pico core, streamed to the panel by DMA.
  The SPI link limits it: one full frame is ~98 KB, **15.73 ms** at 50 MHz, so
  the ceiling is ~63 fps. When a frame finishes shortly before the next 60 Hz
  tick the renderer waits for the tick and starts the next frame on it, so the
  LCD normally runs at exactly 60 fps in step with VSYNC; when frames run slow
  it simply runs free. (Added later than the rest of this document and not yet
  measured on the board.) The renderer reads VRAM line by line while your
  program keeps running, so a half-finished update can show up on screen.
  Write related changes (e.g. all sprite attributes) together and quickly,
  right after the tick, and expect rare tearing rather than a guaranteed clean
  frame.
* There is no raster position and no per-line interrupt.
* The frame flag keeps ticking with the display blanked (R1 bit 6 = 0).

## 6. The stock ROM (monitor + BASIC)

If you write a standalone ROM like the skeleton above, you own the vectors and
can ignore this section.

The P65 monitor/BASIC ROM (`rom/p65_basic.a65` + `fpp65.a65`) is built
with **`VSYNC_IRQ = 1`**, which turns the frame interrupt on at start-up:

* the text and graphics register tables have R1 bit 5 set, and `CLI` is
  executed at cold start;
* the IRQ vector reaches a small handler through `usrirq` (`$03FE/$03FF`) that
  does exactly what section 4 describes and counts frames;
* the BASIC `VDP 1,n` command keeps bit 5 set, so `VDP 1,194` (16x16 sprites)
  does not switch the interrupt off by accident.

What BASIC can use (RAM that is otherwise unused; `PEEK` the decimal address):

| address | decimal | meaning |
|---|---|---|
| `$03E0-$03E2` | 992-994 | frame counter, 24 bit, low byte first, 60 per second |
| `$03E3` | 995 | VDP status bits collected since you last cleared it: 128 = frame, 64 = fifth sprite, 32 = collision |
| `$03E4` | 996 | non-zero (default 1): `VDP 1,n` keeps the interrupt bit |

```
10 T=PEEK(992)
20 IF PEEK(992)=T THEN 20 : REM wait for the next frame
30 FRAMES=PEEK(992)+256*PEEK(993)
```

Collision flag: `POKE 995,0`, run the sprites, then `IF PEEK(995) AND 32 THEN ...`
(bit 128 is set every frame). Switch the interrupt off with
`POKE 996,0 : VDP 1,208` (text mode value without bit 5).

Your own machine-code handler: put its address in `$03FE/$03FF`. The monitor's
`irq:` stub (which separates BRK from a hardware IRQ) has already done
`STA $45` (its `acc` variable) and clobbered A, so **A is garbage on entry and
the real A is in zero page `$45`**; end the handler with `LDA $45` / `RTI`
instead of `PLA`. X and Y are untouched. Follow the rules of section 4.

To build the old ROM without the interrupt: assemble with `-D VSYNC_IRQ=0`
(`ca65 -D VSYNC_IRQ=0 p65_basic.a65`; it then comes out bit-identical to the
previous version). The new code lives in the unused area at `$8100`, so
nothing else moved: only 80 bytes of the ROM differ from the old one.

## 7. Running and testing

* **Emulator:** `python tools/asm.py examples/vsync.a65`, then
  `python ../emulator/p65emu.py --rom vsync.bin` (needs PySide6 and numpy, see the
  emulator's `requirements.txt`). The debugger shows the IRQ pin
  and the VDP interrupt-enable bit. **Machine -> VDP interrupt: IRQ release
  delay** switches between an ideal interrupt (0 us), the board-like delay
  (the default, ~7-8 empty entries per frame like the board), and slower ones. Test your handler with the
  board-like setting - with 0 us the empty entries never happen and a handler
  that forgets to test bit 7 would look fine.
* **Demo:** `examples/vdpdemo.a65` (assembles to a 3 KB `vdpdemo.bin`) is a bigger VSYNC example: waving
  rolling rainbow, sine-bouncing sprite text, a smooth pixel scroller and a LED
  chaser, all paced by the frame interrupt (about half a frame of CPU time per
  update). It runs from RAM under the BASIC ROM: import `vdpdemo.bin` into the
  storage (storage manager, load address `$0800`) and type `BRUN "VDPDEMO"`;
  any key (or RESET) gets BASIC back: the main loop polls `$8001` and jumps through the reset vector. It is a good template for a RAM program that hooks
  the IRQ through `$03FE` (see section 6).
* **Example:** `examples/vsync.a65`: the three LEDs count frames. Build any
  source with `python tools/asm.py yourfile.a65` (cc65 2.19 or newer). The firmware
  embeds the ROM through `include/rom.h`, generated by `python tools/make_rom.py`.
* **On the board:** the firmware can be built with `make IRQ_DEBUG=1`; it
  then prints once a second on the USB serial port:
  `IRQDBG /s: vsync=60 status_reads=... with_INT=60 irq_asserts=60 p2_writes=...`
  (`status_reads - with_INT` = empty handler entries).

## 8. Hardware background (for the curious)

* /IRQ of the W65C02S is not wired to a Pico pin. It is output P2_1 of the
  PCAL6524 I2C expander (1 MHz I2C), pulled up by 10 k. The Pico's core 0
  watches the VDP state (`video_poll()` in `src/video.c`) and writes the
  expander; that is where the ~100 us delays come from.
* The VDP is emulated by the Pico; VRAM is a plain 16 KB array. The CPU bus is
  served by the other core; clock stretching in the PIO program is the only
  way the bus can be slowed (RDY is tied high).
* NMI is on P2_2 and is **not used** by the firmware (it idles high).
* The frame timer is derived from the Pico's 1 MHz system timer
  (`time_us_64()`), so the period does not drift.
