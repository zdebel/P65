/*
 * P65 storage: the mailbox through which the 65C02 talks to the flash file
 * system, and the executor that carries out its commands.
 *
 * The mailbox is a block of STO_REG_COUNT registers in the I/O page, starting at
 * IO_STORAGE (include/mapper.h). Nothing here depends on where that is: the
 * executor gets a pointer to the register block and to the CPU's memory, so a
 * different memory map only changes mapper.h, the equates at the top of
 * asm_examples/p65_basic.a65 and emulator/memmap.py.
 *
 * How the CPU uses it:
 *     write the arguments to the registers, write the command to STO_CMD,
 *     poll STO_STATUS until bit 7 (BUSY) is clear; the low 7 bits are then the
 *     result code (0 = OK). The Pico copies file data straight to / from CPU
 *     memory - nothing is moved over the bus.
 */
#ifndef P65_STORAGE_H
#define P65_STORAGE_H

#include <stdint.h>
#include <stdbool.h>
#include "fs.h"

/* ---- registers: offsets from the start of the mailbox ------------------------- */
#define STO_CMD         0x00    /* W: start a command (value = command)              */
#define STO_STATUS      0x01    /* R: bit 7 = BUSY, bits 0-6 = result of the last command */
#define STO_ADDR_L      0x02    /* in: CPU address (LOAD with STO_F_ADDR, SAVE source) / out: where it was loaded */
#define STO_ADDR_H      0x03
#define STO_LEN_L       0x04    /* in: SAVE length / out: file size (low 16 bits) */
#define STO_LEN_H       0x05
#define STO_NAME_L      0x06    /* pointer (in CPU memory) to the file name ... */
#define STO_NAME_H      0x07
#define STO_NAMELEN     0x08    /* ... and its length. "NAME.EXT", no terminator needed */
#define STO_TYPE        0x09    /* in: file type / STO_TYPE_ANY / out: type of the file */
#define STO_EXEC_L      0x0A    /* in: SAVE exec address / out: file's exec address */
#define STO_EXEC_H      0x0B
#define STO_INDEX       0x0C    /* DIRENT: entry number */
#define STO_FLAGS       0x0D    /* STO_F_* */
#define STO_ID          0x0E    /* R: STO_ID_VALUE when the storage hardware is there */
#define STO_VERSION     0x0F    /* R: interface version */
#define STO_NAME2_L     0x10    /* RENAME: pointer / length of the new name */
#define STO_NAME2_H     0x11
#define STO_NAME2LEN    0x12
#define STO_SIZE_0      0x13    /* out: exact size / FREE: free bytes (24 bit, low byte first) */
#define STO_SIZE_1      0x14
#define STO_SIZE_2      0x15
#define STO_REG_COUNT   0x20    /* 0x16-0x1F reserved */

#define STO_ID_VALUE    0x53    /* 'S' */
#define STO_VERSION_NO  1

/* ---- commands ------------------------------------------------------------------ */
#define STO_C_NOP       0
#define STO_C_LOAD      1       /* file -> memory                                   */
#define STO_C_SAVE      2       /* memory -> file (replaces an existing one)        */
#define STO_C_DELETE    3
#define STO_C_DIRENT    4       /* entry STO_INDEX -> "NAME.EXT",0 written at the name pointer (>= 13 bytes) */
#define STO_C_INFO      5       /* like LOAD but only reports type / address / size  */
#define STO_C_FREE      6       /* free bytes -> STO_SIZE, number of files -> STO_LEN */
#define STO_C_RENAME    7       /* name -> name2                                    */
#define STO_C_FORMAT    8       /* deletes every file; needs STO_ADDR = STO_FORMAT_MAGIC */
#define STO_FORMAT_MAGIC 0xF0A5

/* ---- flags ----------------------------------------------------------------------- */
#define STO_F_ADDR      0x01    /* LOAD: use STO_ADDR instead of the file's own load address */

/* ---- file types -------------------------------------------------------------------- */
#define STO_TYPE_BIN    0       /* machine code / data, default extension .BIN */
#define STO_TYPE_BAS    1       /* tokenized BASIC program, .BAS               */
#define STO_TYPE_TXT    2       /* text, .TXT                                  */
#define STO_TYPE_ANY    0xFF    /* LOAD / INFO: accept any type (default ext .BIN) */

/* ---- results (low 7 bits of STO_STATUS) ---------------------------------------------- */
#define STO_BUSY        0x80
#define STO_OK          0
#define STO_E_NOTFOUND  1
#define STO_E_FULL      2
#define STO_E_BADNAME   3
#define STO_E_BADTYPE   4
#define STO_E_IO        5
#define STO_E_BADCMD    6
#define STO_E_END       7       /* DIRENT: no such entry */
#define STO_E_ADDRESS   8       /* outside RAM (LOAD) or beyond 64 KB */
#define STO_E_EXISTS    9
#define STO_E_TOOBIG    10      /* more than 64 KB */
#define STO_E_CORRUPT   11
#define STO_E_NOSTORAGE 12      /* nothing mounted / not formatted */
#define STO_E_COUNT     13

/* ---- the executor ----------------------------------------------------------------------- */
typedef struct {
    uint8_t *mem;               /* the CPU's 64 KB address space */
    uint8_t *regs;              /* the mailbox inside it (STO_REG_COUNT bytes) */
    uint32_t ram_end;           /* last address a LOAD may write */
    fs_t *fs;                   /* NULL / not ready: every command reports STO_E_NOSTORAGE */
    fs_writer_t *wr;
    bool ready;
} sto_ctx_t;

void sto_regs_init(uint8_t *regs);              /* ID / version / idle status */
void sto_execute(sto_ctx_t *c);                 /* runs regs[STO_CMD]; ends by writing STO_STATUS (BUSY cleared) */
int  sto_result_from_fs(int fs_result);         /* FS_* -> STO_* */
const char *sto_result_text(int result);        /* "FILE NOT FOUND" ... */

#endif
