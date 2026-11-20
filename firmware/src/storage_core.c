/* The storage mailbox executor. Pure C, no hardware: used by the firmware,
   the host tests (tests/fs_test.c). The emulator has the same logic in Python. */
#include "storage.h"
#include <string.h>

static uint16_t reg16(const uint8_t *r, int lo) { return (uint16_t)(r[lo] | (r[lo + 1] << 8)); }
static void set16(uint8_t *r, int lo, uint16_t v) { r[lo] = (uint8_t)v; r[lo + 1] = (uint8_t)(v >> 8); }

void sto_regs_init(uint8_t *r)
{
    memset(r, 0, STO_REG_COUNT);
    r[STO_ID] = STO_ID_VALUE;
    r[STO_VERSION] = STO_VERSION_NO;
}

int sto_result_from_fs(int r)
{
    switch (r) {
    case FS_OK:           return STO_OK;
    case FS_ERR_NOTFOUND: return STO_E_NOTFOUND;
    case FS_ERR_FULL:     return STO_E_FULL;
    case FS_ERR_BADNAME:  return STO_E_BADNAME;
    case FS_ERR_CORRUPT:  return STO_E_CORRUPT;
    case FS_ERR_EXISTS:   return STO_E_EXISTS;
    case FS_ERR_ARG:      return STO_E_BADCMD;
    default:              return STO_E_IO;
    }
}

const char *sto_result_text(int r)
{
    static const char *const t[STO_E_COUNT] = {
        "OK", "FILE NOT FOUND", "DISK FULL", "BAD FILE NAME", "FILE TYPE MISMATCH", "I/O ERROR",
        "BAD COMMAND", "END OF DIRECTORY", "ADDRESS RANGE", "FILE EXISTS", "FILE TOO BIG",
        "FILE CORRUPT", "NO STORAGE"};
    return (r >= 0 && r < STO_E_COUNT) ? t[r] : "ERROR";
}

static const char *default_ext(uint8_t type)
{
    switch (type) {
    case STO_TYPE_BAS: return "BAS";
    case STO_TYPE_TXT: return "TXT";
    default:           return "BIN";
    }
}

/* the name at (ptr, len) in CPU memory -> 8.3 */
static int get_name(const sto_ctx_t *c, int lo, int lenreg, uint8_t type, char out[FS_NAME_LEN])
{
    uint32_t p = reg16(c->regs, lo);
    uint32_t n = c->regs[lenreg];
    if (n == 0 || p + n > 0x10000u) return STO_E_BADNAME;
    char buf[40];
    if (n >= sizeof buf) return STO_E_BADNAME;
    memcpy(buf, c->mem + p, n);
    return fs_parse_name(buf, n, default_ext(type), out) == FS_OK ? STO_OK : STO_E_BADNAME;
}

static void report(uint8_t *r, const fs_entry_t *e, uint16_t addr)
{
    set16(r, STO_ADDR_L, addr);
    set16(r, STO_LEN_L, e->size > 0xFFFF ? 0xFFFF : (uint16_t)e->size);
    r[STO_SIZE_0] = (uint8_t)e->size;
    r[STO_SIZE_1] = (uint8_t)(e->size >> 8);
    r[STO_SIZE_2] = (uint8_t)(e->size >> 16);
    r[STO_TYPE] = e->type;
    set16(r, STO_EXEC_L, e->exec);
}

void sto_execute(sto_ctx_t *c)
{
    uint8_t *r = c->regs;
    uint8_t cmd = r[STO_CMD];
    int res = STO_OK;
    char n1[FS_NAME_LEN], n2[FS_NAME_LEN];
    const fs_entry_t *e;

    if (cmd == STO_C_NOP) { r[STO_STATUS] = STO_OK; return; }
    if (!c->ready || !c->fs) { r[STO_STATUS] = STO_E_NOSTORAGE; return; }
    fs_t *fs = c->fs;

    switch (cmd) {
    case STO_C_LOAD:
    case STO_C_INFO:
        if ((res = get_name(c, STO_NAME_L, STO_NAMELEN, r[STO_TYPE], n1)) != STO_OK) break;
        e = fs_find(fs, n1);
        if (!e) { res = STO_E_NOTFOUND; break; }
        if (r[STO_TYPE] != STO_TYPE_ANY && r[STO_TYPE] != e->type) { res = STO_E_BADTYPE; break; }
        {
            uint32_t dest = (r[STO_FLAGS] & STO_F_ADDR) ? reg16(r, STO_ADDR_L) : e->load;
            if (cmd == STO_C_LOAD) {
                if (e->size > 0xFFFF) { res = STO_E_TOOBIG; break; }
                if (dest + e->size > c->ram_end + 1u) { res = STO_E_ADDRESS; break; }
                int got = fs_read(fs, e, 0, c->mem + dest, e->size);
                if (got != (int)e->size) { res = got < 0 ? sto_result_from_fs(got) : STO_E_IO; break; }
                if (fs_crc32(0, c->mem + dest, e->size) != e->crc) { res = STO_E_CORRUPT; break; }
            }
            report(r, e, (uint16_t)dest);
        }
        break;

    case STO_C_SAVE:
        if ((res = get_name(c, STO_NAME_L, STO_NAMELEN, r[STO_TYPE], n1)) != STO_OK) break;
        {
            uint32_t addr = reg16(r, STO_ADDR_L), len = reg16(r, STO_LEN_L);
            uint8_t type = r[STO_TYPE] == STO_TYPE_ANY ? STO_TYPE_BIN : r[STO_TYPE];
            if (addr + len > 0x10000u) { res = STO_E_ADDRESS; break; }
            int f = fs_writer_begin(c->wr, fs, n1, type, (uint16_t)addr, reg16(r, STO_EXEC_L), len);
            if (f == FS_OK) f = fs_writer_write(c->wr, c->mem + addr, len);
            if (f == FS_OK) f = fs_writer_finish(c->wr);
            else fs_writer_abort(c->wr);
            res = sto_result_from_fs(f);
        }
        break;

    case STO_C_DELETE:
        if ((res = get_name(c, STO_NAME_L, STO_NAMELEN, r[STO_TYPE], n1)) != STO_OK) break;
        res = sto_result_from_fs(fs_delete(fs, n1));
        break;

    case STO_C_RENAME:
        if ((res = get_name(c, STO_NAME_L, STO_NAMELEN, r[STO_TYPE], n1)) != STO_OK) break;
        if ((res = get_name(c, STO_NAME2_L, STO_NAME2LEN, r[STO_TYPE], n2)) != STO_OK) break;
        res = sto_result_from_fs(fs_rename(fs, n1, n2, c->wr));
        break;

    case STO_C_DIRENT:
        e = fs_entry(fs, r[STO_INDEX]);
        if (!e) { res = STO_E_END; break; }
        {
            uint32_t p = reg16(r, STO_NAME_L);
            if (p + 13 > 0x10000u) { res = STO_E_ADDRESS; break; }
            fs_format_name(e->name, (char *)c->mem + p);
            report(r, e, e->load);
        }
        break;

    case STO_C_FREE: {
        uint32_t fb = fs_free_bytes(fs);
        r[STO_SIZE_0] = (uint8_t)fb; r[STO_SIZE_1] = (uint8_t)(fb >> 8); r[STO_SIZE_2] = (uint8_t)(fb >> 16);
        set16(r, STO_LEN_L, (uint16_t)fs_count(fs));
        break;
    }

    case STO_C_FORMAT:
        if (reg16(r, STO_ADDR_L) != STO_FORMAT_MAGIC) { res = STO_E_BADCMD; break; }
        res = sto_result_from_fs(fs_format(fs));
        break;

    default:
        res = STO_E_BADCMD;
    }
    r[STO_STATUS] = (uint8_t)res;                   /* last: clears BUSY */
}
