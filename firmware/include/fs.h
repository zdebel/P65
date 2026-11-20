/*
 * P65 flash file system: a tiny, power-fail-safe, wear-levelling store for a
 * few hundred 8.3 files. Hardware independent: the three flash operations are
 * passed in, so the same code runs in the firmware and in the host tests.
 *
 *  - The region is split in 4 KB sectors. A file is a chain of sectors; every
 *    sector starts with a 32-byte header (magic, kind, sequence, next sector,
 *    generation, erase count, CRCs). The first sector also holds the file's
 *    name, type, load / exec address, size and CRC32.
 *  - A new file is written sector by sector; the FIRST sector is written last
 *    and its header page last of all, so a file either exists completely or
 *    not at all (a power failure leaves only unreachable sectors, which are
 *    reused later).
 *  - Deleting only clears a flag byte in the first sector (no erase).
 *  - Free sectors are picked by lowest erase count (wear levelling).
 *  - Overwriting a file writes the new one first and deletes the old one only
 *    after the new one is committed (if there is not enough room for both the
 *    old file is dropped first).
 *
 * Sizes: 4 KB sectors, 32 bytes of header per sector, 32 more in the first.
 */
#ifndef P65_FS_H
#define P65_FS_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#define FS_SECTOR_SIZE   4096u
#define FS_PAGE_SIZE     256u
#define FS_MAX_SECTORS   512u           /* up to 2 MB of storage */
#define FS_MAX_FILES     256u
#define FS_NAME_LEN      11             /* 8 + 3, space padded, upper case, no dot */

#define FS_FIRST_DATA    (FS_SECTOR_SIZE - 64u)     /* data bytes in a first sector */
#define FS_CONT_DATA     (FS_SECTOR_SIZE - 32u)     /* data bytes in a later sector */

/* results (negative = error) */
#define FS_OK             0
#define FS_ERR_IO        -1
#define FS_ERR_NOTFOUND  -2
#define FS_ERR_FULL      -3
#define FS_ERR_BADNAME   -4
#define FS_ERR_CORRUPT   -5
#define FS_ERR_ARG       -6
#define FS_ERR_EXISTS    -7

/* the flash the file system lives on; offsets are relative to its start */
typedef struct {
    int  (*read)(void *ctx, uint32_t off, void *buf, uint32_t len);
    int  (*erase)(void *ctx, uint32_t off);                                  /* one 4 KB sector */
    int  (*program)(void *ctx, uint32_t off, const void *buf, uint32_t len); /* page aligned, len a multiple of 256 */
    void *ctx;
    uint32_t size;                      /* bytes, multiple of FS_SECTOR_SIZE */
} fs_flash_t;

typedef struct {
    char     name[FS_NAME_LEN];         /* "GAME    BIN" */
    uint8_t  type;
    uint16_t load;
    uint16_t exec;
    uint32_t size;
    uint32_t crc;                       /* CRC32 of the data */
    uint16_t first;                     /* first sector (internal) */
    uint32_t gen;                       /* generation (internal) */
} fs_entry_t;

typedef struct {
    const fs_flash_t *dev;
    uint16_t nsec;
    uint16_t nfiles;
    uint32_t next_gen;
    uint8_t  used[FS_MAX_SECTORS];      /* 1 = belongs to a live file */
    uint32_t ecount[FS_MAX_SECTORS];    /* erase count per sector (best knowledge) */
    fs_entry_t files[FS_MAX_FILES];     /* sorted by name */
} fs_t;

/* streaming writer: begin / write... / finish (or abort) */
typedef struct {
    fs_t    *fs;
    bool     active;
    char     name[FS_NAME_LEN];
    uint8_t  type;
    uint16_t load, exec;
    uint32_t total, written, gen, crc;
    uint16_t nsec;                      /* sectors reserved */
    uint16_t cur;                       /* index (in list) of the continuation sector being filled */
    uint32_t fill;                      /* bytes in buf */
    int      old_first;                 /* first sector of the file being replaced, or -1 */
    uint16_t list[FS_MAX_SECTORS];
    uint8_t  first_buf[FS_SECTOR_SIZE];
    uint8_t  buf[FS_SECTOR_SIZE];
} fs_writer_t;

/* --- mount / format ------------------------------------------------------ */
int  fs_mount(fs_t *fs, const fs_flash_t *dev);     /* number of files, or <0 */
int  fs_format(fs_t *fs);                           /* quick: deletes every file (no erase) */

/* --- directory ------------------------------------------------------------ */
int  fs_count(const fs_t *fs);
const fs_entry_t *fs_entry(const fs_t *fs, int index);
const fs_entry_t *fs_find(const fs_t *fs, const char name[FS_NAME_LEN]);
uint32_t fs_free_bytes(const fs_t *fs);             /* data bytes that could still be written */
uint32_t fs_total_bytes(const fs_t *fs);

/* --- names ---------------------------------------------------------------- */
/* "game.bin" / "GAME" / "A.B" -> "GAME    BIN". default_ext (3 chars, may be NULL) is used when
   there is no dot. Returns FS_OK or FS_ERR_BADNAME. */
int  fs_parse_name(const char *in, size_t len, const char *default_ext, char out[FS_NAME_LEN]);
/* "GAME    BIN" -> "GAME.BIN\0" (out needs 13 bytes) */
void fs_format_name(const char name[FS_NAME_LEN], char out[13]);

/* --- reading ---------------------------------------------------------------- */
int  fs_read(const fs_t *fs, const fs_entry_t *e, uint32_t offset, void *buf, uint32_t len);   /* bytes read or <0 */
uint32_t fs_crc32(uint32_t crc, const void *data, size_t len);      /* start with 0 */

/* --- writing ---------------------------------------------------------------- */
int  fs_writer_begin(fs_writer_t *w, fs_t *fs, const char name[FS_NAME_LEN], uint8_t type,
                     uint16_t load, uint16_t exec, uint32_t total_size);
int  fs_writer_write(fs_writer_t *w, const void *data, uint32_t len);
int  fs_writer_finish(fs_writer_t *w);              /* commits */
void fs_writer_abort(fs_writer_t *w);

int  fs_delete(fs_t *fs, const char name[FS_NAME_LEN]);
int  fs_rename(fs_t *fs, const char from[FS_NAME_LEN], const char to[FS_NAME_LEN], fs_writer_t *scratch);

#endif
