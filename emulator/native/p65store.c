/*
 * p65store: the firmware's storage code (flash file system, mailbox executor,
 * USB protocol) built as a shared library for the PC. The emulator loads it
 * with ctypes, so it runs exactly the same code - and the same on-flash
 * format - as the device. The "flash" is an image file.
 *
 * Build (MSYS2 / any gcc):   see emulator/build_storelib.py
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "fs.h"
#include "storage.h"
#include "usb_proto.h"

#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

typedef struct {
    char path[512];
    uint8_t *img;
    uint32_t size;
    bool dirty;
    uint32_t erases, programs;          /* since the last p65s_ops() */
    fs_flash_t dev;
    fs_t fs;
    fs_writer_t wr_mailbox, wr_usb;
    proto_t proto;
    /* reply bytes produced by the protocol, collected for p65s_proto() */
    uint8_t *out; size_t out_cap, out_len;
} store_t;

static int h_read(void *c, uint32_t off, void *buf, uint32_t len)
{
    store_t *s = c;
    if (off + len > s->size) return -1;
    memcpy(buf, s->img + off, len);
    return 0;
}
static int h_erase(void *c, uint32_t off)
{
    store_t *s = c;
    if (off % FS_SECTOR_SIZE || off + FS_SECTOR_SIZE > s->size) return -1;
    memset(s->img + off, 0xFF, FS_SECTOR_SIZE);
    s->erases++; s->dirty = true;
    return 0;
}
static int h_program(void *c, uint32_t off, const void *buf, uint32_t len)
{
    store_t *s = c;
    if (off % FS_PAGE_SIZE || len % FS_PAGE_SIZE || off + len > s->size) return -1;
    for (uint32_t i = 0; i < len; i++) s->img[off + i] &= ((const uint8_t *)buf)[i];   /* NOR: bits only go 1 -> 0 */
    s->programs += len / FS_PAGE_SIZE; s->dirty = true;
    return 0;
}

static void proto_out(void *ctx, const uint8_t *d, size_t n)
{
    store_t *s = ctx;
    if (s->out && s->out_len + n <= s->out_cap) { memcpy(s->out + s->out_len, d, n); s->out_len += n; }
}

EXPORT void *p65s_open(const char *path, uint32_t size)
{
    if (size < 2 * FS_SECTOR_SIZE || size % FS_SECTOR_SIZE) return NULL;
    store_t *s = calloc(1, sizeof *s);
    if (!s) return NULL;
    snprintf(s->path, sizeof s->path, "%s", path ? path : "");
    s->size = size;
    s->img = malloc(size);
    if (!s->img) { free(s); return NULL; }
    memset(s->img, 0xFF, size);
    if (s->path[0]) {
        FILE *f = fopen(s->path, "rb");
        if (f) { size_t n = fread(s->img, 1, size, f); (void)n; fclose(f); }
    }
    s->dev.read = h_read; s->dev.erase = h_erase; s->dev.program = h_program; s->dev.ctx = s; s->dev.size = size;
    if (fs_mount(&s->fs, &s->dev) < 0) { free(s->img); free(s); return NULL; }
    proto_init(&s->proto, &s->fs, &s->wr_usb, &s->wr_mailbox, proto_out, s);
    proto_set_ready(&s->proto, true);
    s->erases = s->programs = 0;
    return s;
}

EXPORT int p65s_flush(void *h)
{
    store_t *s = h;
    if (!s->path[0] || !s->dirty) return 0;
    FILE *f = fopen(s->path, "wb");
    if (!f) return -1;
    size_t n = fwrite(s->img, 1, s->size, f);
    fclose(f);
    if (n != s->size) return -1;
    s->dirty = false;
    return 0;
}

EXPORT void p65s_close(void *h)
{
    store_t *s = h;
    if (!s) return;
    p65s_flush(s);
    free(s->img);
    free(s);
}

/* run the mailbox command found in mem + regs_off (the emulator's 64 KB) */
EXPORT void p65s_exec(void *h, uint8_t *mem, uint32_t ram_end, uint32_t regs_off)
{
    store_t *s = h;
    sto_ctx_t c = { mem, mem + regs_off, ram_end, &s->fs, &s->wr_mailbox, true };
    sto_execute(&c);
}

/* flash operations since the last call, for the emulator's timing model */
EXPORT void p65s_ops(void *h, uint32_t *erases, uint32_t *programs)
{
    store_t *s = h;
    *erases = s->erases; *programs = s->programs;
    s->erases = s->programs = 0;
}

EXPORT void p65s_init_regs(uint8_t *regs) { sto_regs_init(regs); }

EXPORT int p65s_count(void *h) { return fs_count(&((store_t *)h)->fs); }
EXPORT uint32_t p65s_free(void *h) { return fs_free_bytes(&((store_t *)h)->fs); }
EXPORT uint32_t p65s_total(void *h) { return fs_total_bytes(&((store_t *)h)->fs); }

/* entry: name[11] type load(2) exec(2) size(4) crc(4) = 24 bytes */
EXPORT int p65s_entry(void *h, int idx, uint8_t *out)
{
    const fs_entry_t *e = fs_entry(&((store_t *)h)->fs, idx);
    if (!e) return FS_ERR_NOTFOUND;
    memcpy(out, e->name, 11);
    out[11] = e->type;
    out[12] = (uint8_t)e->load; out[13] = (uint8_t)(e->load >> 8);
    out[14] = (uint8_t)e->exec; out[15] = (uint8_t)(e->exec >> 8);
    for (int i = 0; i < 4; i++) { out[16 + i] = (uint8_t)(e->size >> (8 * i)); out[20 + i] = (uint8_t)(e->crc >> (8 * i)); }
    return FS_OK;
}

EXPORT int p65s_read(void *h, const char *name11, uint32_t off, uint8_t *buf, uint32_t len)
{
    store_t *s = h;
    const fs_entry_t *e = fs_find(&s->fs, name11);
    if (!e) return FS_ERR_NOTFOUND;
    return fs_read(&s->fs, e, off, buf, len);
}

EXPORT int p65s_write(void *h, const char *name11, int type, int load, int exec, const uint8_t *data, uint32_t len)
{
    store_t *s = h;
    int r = fs_writer_begin(&s->wr_usb, &s->fs, name11, (uint8_t)type, (uint16_t)load, (uint16_t)exec, len);
    if (r == FS_OK) r = fs_writer_write(&s->wr_usb, data, len);
    if (r == FS_OK) r = fs_writer_finish(&s->wr_usb); else fs_writer_abort(&s->wr_usb);
    return r;
}

EXPORT int p65s_delete(void *h, const char *name11) { return fs_delete(&((store_t *)h)->fs, name11); }
EXPORT int p65s_rename(void *h, const char *a, const char *b)
{
    store_t *s = h;
    return fs_rename(&s->fs, a, b, &s->wr_mailbox);
}
EXPORT int p65s_format(void *h) { return fs_format(&((store_t *)h)->fs); }

/* the USB protocol as a pure function: feed received bytes, get the reply bytes */
EXPORT size_t p65s_proto(void *h, const uint8_t *in, size_t n, uint8_t *out, size_t cap, uint32_t now_ms)
{
    store_t *s = h;
    s->out = out; s->out_cap = cap; s->out_len = 0;
    proto_rx(&s->proto, in, n, now_ms);
    proto_tick(&s->proto, now_ms);
    size_t r = s->out_len;
    s->out = NULL;
    return r;
}

EXPORT int p65s_parse_name(const char *in, int len, const char *default_ext, char *out11)
{
    return fs_parse_name(in, (size_t)len, default_ext, out11);
}
EXPORT void p65s_format_name(const char *name11, char *out13) { fs_format_name(name11, out13); }
EXPORT uint32_t p65s_crc32(uint32_t crc, const uint8_t *d, size_t n) { return fs_crc32(crc, d, n); }
EXPORT const char *p65s_result_text(int r) { return sto_result_text(r); }
EXPORT int p65s_reg_count(void) { return STO_REG_COUNT; }
