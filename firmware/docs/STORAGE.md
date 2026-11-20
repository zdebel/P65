# P65 flash storage

About 1 MB of the Pico's flash (the last megabyte) is a small file system. BASIC
programs and binaries are saved to it and loaded from it by name; the emulator's
storage manager copies files between the PC, the emulator and the real device
over USB.

* 8.3 file names, upper case. Three file types: **BIN** (machine code / data,
  with a load and an exec address), **BAS** (tokenized BASIC program), **TXT**.
* No subdirectories. Up to 256 files. One file is at most 64 KB through BASIC
  (the CPU's address space); bigger ones can be stored over USB but not loaded.
* Survives power loss at any moment (see 6), spreads the wear over all sectors,
  keeps its files when the firmware is reflashed through BOOTSEL.

> **Status.** The file system, the mailbox executor, the USB protocol, the ROM
> routines, the tokenizer and the manager are tested on the PC (37 tests, see 7),
> including the real ROM running in the emulator. The firmware builds, but the
> flash driver, the USB transfers and the interplay with the second core have
> **not been run on the board yet** - see 8 for what to look at first.

## 1. From BASIC

| command | does |
|---|---|
| `SAVE "NAME"` | the current program -> `NAME.BAS` |
| `LOAD "NAME"` | `NAME.BAS` replaces the current program (relinked, variables cleared) |
| `BSAVE "NAME",addr,len[,exec]` | memory -> `NAME.BIN` (exec 0 = start at the load address; the manager shows it as "= load" and stores the load address explicitly when importing a BIN) |
| `BLOAD "NAME"[,addr]` | `NAME.BIN` -> memory, at its own address unless `addr` is given |
| `BRUN "NAME"` | `BLOAD` at its own address, then call exec; the program may end with `RTS` |
| `CATALOG` | list files (name, type, size) and the free space |
| `ERASE "NAME"` | delete (without an extension `.BAS`, `.BIN`, `.TXT` are tried in turn) |
| `RENAME "OLD","NEW"` | rename |

Without an extension `SAVE` / `LOAD` use `.BAS` and `BSAVE` / `BLOAD` / `BRUN` use `.BIN`.
Names are strings, so `SAVE "GAME"+STR$(L)` works.

```
10 PRINT "HELLO"
SAVE "HELLO"
NEW
LOAD "HELLO"
RUN
POKE 2048,169 : POKE 2049,66 : POKE 2050,141 : POKE 2051,0 : POKE 2052,9 : POKE 2053,96
BSAVE "PROG",2048,6,2048
BRUN "PROG" : PRINT PEEK(2304)
CATALOG
```

Errors are normal BASIC errors (they work with `ONERR GOTO`; `PEEK(222)` is 160 + the code):
`File not found`, `Disk full`, `Bad file name`, `File type mismatch`, `Storage I/O`,
`Bad storage command`, `Address range` (a `BLOAD` must stay in RAM), `File exists`,
`File too big`, `File corrupt`, `No storage`. `LOAD` also checks that the program fits
below `HIMEM`.

The four statements `CATALOG`, `ERASE`, `RENAME`, `BRUN` use the keyword slots that
used to be `HLIN`, `VLIN`, `ROT=`, `SCALE=` (they only raised "Advanced feature
error"). Build the ROM with `-D STORAGE=0` for the old behaviour.

## 2. From the PC: the storage manager

Emulator: **Machine -> Storage manager** (Ctrl+Shift+T). Left: the emulator's storage
image (`emulator/storage.img`, or the file chosen with *Choose storage image*; it is the very
storage the emulated ROM sees). Right: the device. Pick the serial port of the Pico,
*Connect*. Then

* `->` / `<-` copy the selected files between the two sides (type and addresses are kept);
* **Import**: BASIC text (`.bas` with line numbers) is *tokenized* exactly as the ROM does it;
  an already tokenized `.bas`, binaries (asks for load / exec address) and text are taken as they are.
  Dropping files on a panel does the same;
* **Export**: a BASIC program can be saved as a text listing or as the raw tokenized file;
* **View** (or double click): a BASIC listing, text, or a hex dump; **Rename**, **Delete**, **Format**.

The USB side needs the firmware with storage support and works over the same USB serial
port that carries the debug text. Do not keep another terminal open on that port.

## 3. From machine code: the mailbox

The CPU talks to the storage through 32 registers in the I/O page at `IO_STORAGE`
(`IOBASE+$10` = `$8010` today). Write the arguments, write the command to `STO_CMD`,
wait for bit 7 of `STO_STATUS` to clear; its low 7 bits are then the result (0 = OK).
The Pico copies the data straight into / out of CPU memory.

| offset | name | meaning |
|---|---|---|
| `$00` | `STO_CMD` | write starts a command |
| `$01` | `STO_STATUS` | bit 7 = BUSY, low bits = result of the last command |
| `$02/$03` | `STO_ADDR` | in: CPU address (SAVE source, LOAD with `STO_F_ADDR`) - out: where it went |
| `$04/$05` | `STO_LEN` | in: SAVE length - out: file size (low 16 bits) |
| `$06/$07`, `$08` | `STO_NAME`, `STO_NAMELEN` | pointer to the name in CPU memory, and its length |
| `$09` | `STO_TYPE` | in: type (`0` BIN, `1` BAS, `2` TXT, `$FF` any) - out: the file's type |
| `$0A/$0B` | `STO_EXEC` | in: SAVE exec address - out: the file's |
| `$0C` | `STO_INDEX` | `DIRENT`: entry number |
| `$0D` | `STO_FLAGS` | bit 0 = `STO_F_ADDR` |
| `$0E`, `$0F` | `STO_ID`, `STO_VERSION` | read `$53` ('S') when the hardware is there |
| `$10-$12` | `STO_NAME2` | `RENAME`: the new name |
| `$13-$15` | `STO_SIZE` | out: exact size (24 bit) / `FREE`: free bytes |

Commands: `1` LOAD, `2` SAVE, `3` DELETE, `4` DIRENT (writes `"NAME.EXT",0` at the name
pointer, needs 13 bytes), `5` INFO (LOAD without moving data), `6` FREE (free bytes in
`STO_SIZE`, file count in `STO_LEN`), `7` RENAME, `8` FORMAT (`STO_ADDR` must be `$F0A5`).
Results: see `include/storage.h`. `storage.a65` is a complete, commented user of this interface.

## 4. Moving things in the memory map

Nothing in the storage code knows an address. The three places that define the map are

* `include/mapper.h` (firmware): `RAM_END`, `IO_START`, `ROM_START`, the register offsets
  (`IO_REG_STORAGE`) - all firmware code uses these names;
* `IOBASE` / `ROMBASE` at the top of `rom/p65_basic.a65` (ROM);
* `emulator/memmap.py` (emulator).

To move the I/O page or to add RAM change those three (the I/O page must stay 256-byte
aligned). `LOAD` is limited by `RAM_END`, the mailbox is found through `IO_STORAGE`, and
`tests/test_py_vs_c.py` checks that the Python port behaves like the C code.

## 5. Where things are

| | |
|---|---|
| `include/fs.h`, `src/fs.c` | the flash file system (hardware independent) |
| `include/storage.h`, `src/storage_core.c` | mailbox definitions and executor |
| `include/usb_proto.h`, `src/usb_proto.c` | USB protocol (frames, commands) |
| `src/storage_fw.c` | firmware glue: flash driver (`pico_flash`), mailbox on core 0, USB |
| `rom/storage.a65` | the BASIC commands (ROM) |
| `emulator/p65fs.py` | the same file system, mailbox executor and USB protocol in Python (for the emulator) |
| `tools/p65store.c`, `tools/build_storelib.py` | the C code as a shared library: the reference for `tests/test_py_vs_c.py` |
| `emulator/storage.py`, `p65dev.py`, `storageio.py`, `storagewin.py`, `basictok.py` | emulator side (in the P65 repo next to the firmware) |

The emulator runs a **Python port of the firmware's storage code** (`emulator/p65fs.py`): the same
on-flash format (an image from the emulator can be copied to the device and back), the same results and
even the same flash timing model (an erase takes 45 ms of emulated time, so a `SAVE` really takes a while).
It needs no compiler or DLL, so it works on every system.

**Keeping the two in step.** The C code is the reference. `tests/test_py_vs_c.py` builds it as a shared
library (`tools/build_storelib.py`, needs `cc`/`gcc`/`clang`) and runs the same operations on both: random
file operations, every mailbox command, the USB protocol, disk-full cases. It compares the results, the
directory, the flash operation counts, the USB reply bytes and the **whole flash image byte for byte**.
After any change to `src/fs.c`, `src/storage_core.c`, `src/usb_proto.c` or `emulator/p65fs.py` run
`python -m unittest tests.test_py_vs_c`; a mismatch shows which side has to follow the other. The
power-failure sweep (a power cut at every flash operation) runs for the C code in `tests/fs_test.c` and for
the Python code in the same test file.

## 6. How it works inside

* **Flash**: the last `STORAGE_SIZE_BYTES` (1 MB, `include/storage_fw.h`) of the flash. The
  firmware refuses to mount it if the firmware image reaches that far.
* **Sectors** (4 KB): every sector starts with a 32-byte header (magic, kind, sequence, next
  sector, generation, erase count, CRC32 of the data and of the header). A file is a chain of
  sectors; the first one also holds name, type, addresses, size and the CRC32 of the file.
* **Atomic writes**: the sectors of a new file are written first, the *first* sector last, and in
  it the header page last of all (the page holding header and first data is programmed in two
  steps, so a torn write can only damage the header, which fails its CRC). A file therefore exists
  completely or not at all. An overwritten file is retired only after the new one is committed.
* **Deleting** only clears a flag byte (no erase). Dead sectors are erased when they are reused.
* **Wear levelling**: a new file takes the free sectors with the lowest erase counts.
* The `tests/fs_test.c` simulator cuts the power at every one of the 240 flash operations of a
  mixed workload in four ways (before, half way, after, random bits) and checks that every
  file is intact afterwards and the disk is still usable (960 cases).
* **USB protocol** (`include/usb_proto.h`): CRC-16 framed requests: hello, list, read, put
  (begin / data / end with a CRC32 of the whole file), delete, rename, format. Bytes outside
  frames are ignored, so debug text can share the port.

## 7. Tests

```
python -m unittest discover -s tests -v
```

37 tests: the file system with power failures (needs a C compiler), the mailbox, the USB protocol
and its client, the BASIC tokenizer (compared against the ROM's own tokenizer), import / export,
the manager window, and the real ROM running `SAVE` / `LOAD` / `BSAVE` / `BLOAD` / `BRUN` / `CATALOG`
/ `ERASE` / `RENAME` and the error cases in the emulator (needs `ca65`).

## 8. Caveats and what to check on the board

* **Flash erase stalls things.** Erasing a sector takes ~45 ms; during it the Pico cannot run
  code from flash. `flash_safe_execute` stops core 1 (it waits in RAM; the 65C02 just waits, its
  clock is stretched) and disables interrupts on core 0, so the LCD freezes and USB is silent for
  that time. A `SAVE` of 30 KB is about half a second. Expect a visible hiccup, not a crash.
* **First things to try**: flash the firmware, open the USB serial port - at start it prints
  `storage: N files, M KB free` (or why it is disabled). Then `CATALOG`, `SAVE "T"`, `CATALOG`,
  `NEW`, `LOAD "T"`; then connect the manager and list the files. If `SAVE` reports
  `Storage I/O error`, `flash_safe_execute` was refused (check that core 1 runs
  `flash_safe_execute_core_init`).
* The storage area is assumed to be the last 1 MB of a 2 MB flash (the standard Pico). Other
  sizes: `PICO_FLASH_SIZE_BYTES` / `STORAGE_SIZE_BYTES`.
* `FORMAT` is quick: it only marks the files deleted.
* `tools/p65tok.c` is outdated (its token table does not match the ROM); use `emulator/basictok.py`.
