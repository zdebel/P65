/* P65 flash file system - see fs.h for the design. Hardware independent. */
#include "fs.h"
#include <string.h>

/* ---- on-flash layout ------------------------------------------------------ */
#define HDR_MAGIC   0x32534650u         /* "PFS2" */
#define K_FIRST     1
#define K_CONT      2
#define H_KIND      4
#define H_FLAGS     5                   /* 0xFF live, 0x00 deleted (first sector only) */
#define H_SEQ       6
#define H_NEXT      8
#define H_USED      10
#define H_GEN       12
#define H_ECOUNT    16
#define H_DCRC      20
#define H_HCRC      28                  /* CRC32 of bytes 0..27 with flags = 0xFF */
#define M_NAME      32                  /* first sector only */
#define M_TYPE      43
#define M_LOAD      44
#define M_EXEC      46
#define M_SIZE      48
#define M_CRC       52
#define NONE16      0xFFFFu

static uint32_t rd32(const uint8_t *p) { return p[0] | (p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24); }
static uint16_t rd16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static void wr32(uint8_t *p, uint32_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24); }
static void wr16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }

uint32_t fs_crc32(uint32_t crc, const void *data, size_t len)
{
    static const uint32_t nib[16] = {
        0x00000000, 0x1DB71064, 0x3B6E20C8, 0x26D930AC, 0x76DC4190, 0x6B6B51F4, 0x4DB26158, 0x5005713C,
        0xEDB88320, 0xF00F9344, 0xD6D6A3E8, 0xCB61B38C, 0x9B64C2B0, 0x86D3D2D4, 0xA00AE278, 0xBDBDF21C};
    const uint8_t *p = (const uint8_t *)data;
    crc = ~crc;
    while (len--) {
        crc ^= *p++;
        crc = (crc >> 4) ^ nib[crc & 0xF];
        crc = (crc >> 4) ^ nib[crc & 0xF];
    }
    return ~crc;
}

typedef struct {
    uint8_t  kind, flags;
    uint16_t seq, next, used;
    uint32_t gen, ecount, dcrc;
} shdr_t;

static bool hdr_parse(const uint8_t *h, shdr_t *o)
{
    if (rd32(h) != HDR_MAGIC) return false;
    uint8_t t[28];
    memcpy(t, h, 28);
    t[H_FLAGS] = 0xFF;
    if (fs_crc32(0, t, 28) != rd32(h + H_HCRC)) return false;
    o->kind = h[H_KIND];
    o->flags = h[H_FLAGS];
    o->seq = rd16(h + H_SEQ);
    o->next = rd16(h + H_NEXT);
    o->used = rd16(h + H_USED);
    o->gen = rd32(h + H_GEN);
    o->ecount = rd32(h + H_ECOUNT);
    o->dcrc = rd32(h + H_DCRC);
    return o->kind == K_FIRST || o->kind == K_CONT;
}

static void hdr_build(uint8_t *sec, uint8_t kind, uint16_t seq, uint16_t next, uint16_t used,
                      uint32_t gen, uint32_t ecount, uint32_t dcrc)
{
    wr32(sec, HDR_MAGIC);
    sec[H_KIND] = kind;
    sec[H_FLAGS] = 0xFF;
    wr16(sec + H_SEQ, seq);
    wr16(sec + H_NEXT, next);
    wr16(sec + H_USED, used);
    wr32(sec + H_GEN, gen);
    wr32(sec + H_ECOUNT, ecount);
    wr32(sec + H_DCRC, dcrc);
    wr32(sec + 24, 0xFFFFFFFFu);
    wr32(sec + H_HCRC, fs_crc32(0, sec, 28));
}

static int rd(const fs_t *fs, uint32_t off, void *buf, uint32_t len)
{
    return fs->dev->read(fs->dev->ctx, off, buf, len) == 0 ? FS_OK : FS_ERR_IO;
}

static bool sector_blank(const fs_t *fs, uint16_t s)
{
    uint8_t b[FS_PAGE_SIZE];
    for (uint32_t pg = 0; pg < FS_SECTOR_SIZE / FS_PAGE_SIZE; pg++) {
        if (rd(fs, s * FS_SECTOR_SIZE + pg * FS_PAGE_SIZE, b, FS_PAGE_SIZE) != FS_OK) return false;
        for (uint32_t i = 0; i < FS_PAGE_SIZE; i++)
            if (b[i] != 0xFF) return false;
    }
    return true;
}

static bool page_blank(const uint8_t *p)
{
    for (uint32_t i = 0; i < FS_PAGE_SIZE; i++)
        if (p[i] != 0xFF) return false;
    return true;
}

/* Erase (if needed) and write a whole sector image. The sector only becomes valid
   when its header is complete, and the header is written last. Page 0 holds the
   header AND the first bytes of data, so it is programmed in two steps: first
   everything but the header, then the full page again. The second step only
   changes the header bytes (the rest is programmed with the values it already has),
   so a power failure in the middle of it can leave at worst a header that fails
   its CRC - never a valid header on top of incomplete data. */
static int flush_sector(fs_t *fs, uint16_t s, const uint8_t *img, bool need_erase)
{
    const fs_flash_t *d = fs->dev;
    uint32_t base = (uint32_t)s * FS_SECTOR_SIZE;
    if (need_erase && d->erase(d->ctx, base) != 0) return FS_ERR_IO;
    for (uint32_t pg = 1; pg < FS_SECTOR_SIZE / FS_PAGE_SIZE; pg++) {
        const uint8_t *p = img + pg * FS_PAGE_SIZE;
        if (!page_blank(p) && d->program(d->ctx, base + pg * FS_PAGE_SIZE, p, FS_PAGE_SIZE) != 0) return FS_ERR_IO;
    }
    uint8_t pg[FS_PAGE_SIZE];
    memcpy(pg, img, FS_PAGE_SIZE);
    memset(pg, 0xFF, 32);                                   /* no header yet */
    if (!page_blank(pg) && d->program(d->ctx, base, pg, FS_PAGE_SIZE) != 0) return FS_ERR_IO;
    if (d->program(d->ctx, base, img, FS_PAGE_SIZE) != 0) return FS_ERR_IO;
    return FS_OK;
}

static int mark_deleted(fs_t *fs, uint16_t first)
{
    uint8_t pg[FS_PAGE_SIZE];
    uint32_t base = (uint32_t)first * FS_SECTOR_SIZE;
    if (rd(fs, base, pg, FS_PAGE_SIZE) != FS_OK) return FS_ERR_IO;
    pg[H_FLAGS] = 0x00;                         /* only clears bits: no erase needed */
    return fs->dev->program(fs->dev->ctx, base, pg, FS_PAGE_SIZE) == 0 ? FS_OK : FS_ERR_IO;
}

static uint32_t sectors_for(uint32_t size)
{
    if (size <= FS_FIRST_DATA) return 1;
    return 1 + (size - FS_FIRST_DATA + FS_CONT_DATA - 1) / FS_CONT_DATA;
}

/* Walk the chain of a file, checking every header. Fills list[] (if given). */
static bool chain_walk(const fs_t *fs, const fs_entry_t *e, uint16_t *list)
{
    uint32_t need = sectors_for(e->size);
    uint16_t cur = e->first;
    uint8_t h[32];
    shdr_t hd;
    for (uint32_t i = 0; i < need; i++) {
        if (list) list[i] = cur;
        if (i == need - 1) break;
        if (rd(fs, (uint32_t)cur * FS_SECTOR_SIZE, h, 32) != FS_OK || !hdr_parse(h, &hd)) return false;
        uint16_t nxt = hd.next;
        if (nxt >= fs->nsec || nxt == cur) return false;
        if (rd(fs, (uint32_t)nxt * FS_SECTOR_SIZE, h, 32) != FS_OK || !hdr_parse(h, &hd)) return false;
        if (hd.kind != K_CONT || hd.seq != i + 1 || hd.gen != e->gen) return false;
        cur = nxt;
    }
    return true;
}

static int cmp_name(const char *a, const char *b) { return memcmp(a, b, FS_NAME_LEN); }

static int find_index(const fs_t *fs, const char *name)
{
    for (int i = 0; i < fs->nfiles; i++)
        if (cmp_name(fs->files[i].name, name) == 0) return i;
    return -1;
}

static void insert_sorted(fs_t *fs, const fs_entry_t *e)
{
    int pos = fs->nfiles;
    while (pos > 0 && cmp_name(fs->files[pos - 1].name, e->name) > 0) {
        fs->files[pos] = fs->files[pos - 1];
        pos--;
    }
    fs->files[pos] = *e;
    fs->nfiles++;
}

static void remove_index(fs_t *fs, int idx)
{
    for (int i = idx; i + 1 < fs->nfiles; i++) fs->files[i] = fs->files[i + 1];
    fs->nfiles--;
}

/* ---------------------------------------------------------------------------- */
int fs_mount(fs_t *fs, const fs_flash_t *dev)
{
    if (!dev || dev->size < 2 * FS_SECTOR_SIZE || (dev->size % FS_SECTOR_SIZE)) return FS_ERR_ARG;
    memset(fs, 0, sizeof *fs);
    fs->dev = dev;
    fs->nsec = (uint16_t)(dev->size / FS_SECTOR_SIZE > FS_MAX_SECTORS ? FS_MAX_SECTORS : dev->size / FS_SECTOR_SIZE);

    uint8_t h[64];
    shdr_t hd;
    for (uint16_t s = 0; s < fs->nsec; s++) {
        if (rd(fs, (uint32_t)s * FS_SECTOR_SIZE, h, 32) != FS_OK) return FS_ERR_IO;
        if (hdr_parse(h, &hd)) fs->ecount[s] = hd.ecount;
    }

    uint32_t max_gen = 0;
    for (uint16_t s = 0; s < fs->nsec; s++) {
        if (rd(fs, (uint32_t)s * FS_SECTOR_SIZE, h, 64) != FS_OK) return FS_ERR_IO;
        if (!hdr_parse(h, &hd) || hd.kind != K_FIRST) continue;
        if (hd.gen > max_gen) max_gen = hd.gen;
        if (hd.flags != 0xFF) continue;                         /* deleted */
        fs_entry_t e;
        memcpy(e.name, h + M_NAME, FS_NAME_LEN);
        e.type = h[M_TYPE];
        e.load = rd16(h + M_LOAD);
        e.exec = rd16(h + M_EXEC);
        e.size = rd32(h + M_SIZE);
        e.crc = rd32(h + M_CRC);
        e.first = s;
        e.gen = hd.gen;
        if (e.size > 0x00FFFFFFu || !chain_walk(fs, &e, NULL)) continue;   /* torn / orphaned */
        int dup = find_index(fs, e.name);
        if (dup >= 0) {                                         /* crash while overwriting: newest wins */
            if (fs->files[dup].gen >= e.gen) { mark_deleted(fs, s); continue; }
            mark_deleted(fs, fs->files[dup].first);
            remove_index(fs, dup);
        }
        insert_sorted(fs, &e);
    }
    fs->next_gen = max_gen + 1;

    uint16_t list[FS_MAX_SECTORS];
    for (int i = 0; i < fs->nfiles; i++) {
        if (chain_walk(fs, &fs->files[i], list)) {
            uint32_t n = sectors_for(fs->files[i].size);
            for (uint32_t k = 0; k < n; k++) fs->used[list[k]] = 1;
        }
    }
    return fs->nfiles;
}

int fs_count(const fs_t *fs) { return fs->nfiles; }

const fs_entry_t *fs_entry(const fs_t *fs, int index)
{
    return (index >= 0 && index < fs->nfiles) ? &fs->files[index] : NULL;
}

const fs_entry_t *fs_find(const fs_t *fs, const char name[FS_NAME_LEN])
{
    int i = find_index(fs, name);
    return i >= 0 ? &fs->files[i] : NULL;
}

static uint32_t free_sectors(const fs_t *fs)
{
    uint32_t n = 0;
    for (uint16_t s = 0; s < fs->nsec; s++)
        if (!fs->used[s]) n++;
    return n;
}

uint32_t fs_free_bytes(const fs_t *fs) { return free_sectors(fs) * FS_CONT_DATA; }
uint32_t fs_total_bytes(const fs_t *fs) { return (uint32_t)fs->nsec * FS_CONT_DATA; }

/* ---- names ----------------------------------------------------------------- */
static bool name_char_ok(char c)
{
    return (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || (c != 0 && strchr("_-$#@!%&'()", c) != NULL);
}

int fs_parse_name(const char *in, size_t len, const char *default_ext, char out[FS_NAME_LEN])
{
    memset(out, ' ', FS_NAME_LEN);
    size_t i = 0, n = 0;
    while (i < len && in[i] != '.') {
        char c = in[i];
        if (c >= 'a' && c <= 'z') c = (char)(c - 32);
        if (!name_char_ok(c) || n >= 8) return FS_ERR_BADNAME;
        out[n++] = c;
        i++;
    }
    if (n == 0) return FS_ERR_BADNAME;
    if (i < len) {                                              /* a dot: explicit extension */
        i++;
        size_t e = 0;
        while (i < len) {
            char c = in[i++];
            if (c >= 'a' && c <= 'z') c = (char)(c - 32);
            if (!name_char_ok(c) || e >= 3) return FS_ERR_BADNAME;
            out[8 + e++] = c;
        }
    } else if (default_ext) {
        for (size_t k = 0; k < 3 && default_ext[k]; k++) out[8 + k] = default_ext[k];
    }
    return FS_OK;
}

void fs_format_name(const char name[FS_NAME_LEN], char out[13])
{
    int n = 0;
    for (int i = 0; i < 8 && name[i] != ' '; i++) out[n++] = name[i];
    out[n++] = '.';
    for (int i = 8; i < 11 && name[i] != ' '; i++) out[n++] = name[i];
    out[n] = 0;
}

/* ---- reading --------------------------------------------------------------------- */
int fs_read(const fs_t *fs, const fs_entry_t *e, uint32_t offset, void *buf, uint32_t len)
{
    if (offset > e->size) return FS_ERR_ARG;
    if (len > e->size - offset) len = e->size - offset;
    uint8_t *out = (uint8_t *)buf;
    uint32_t done = 0;
    uint16_t cur = e->first;
    uint32_t pos = 0;                                           /* file offset of the current sector's data */
    uint32_t idx = 0;
    uint8_t h[32];
    shdr_t hd;
    while (done < len) {
        uint32_t cap = idx == 0 ? FS_FIRST_DATA : FS_CONT_DATA;
        uint32_t here = cap;
        if (pos + here > e->size) here = e->size - pos;
        uint32_t start = offset + done;
        if (start < pos + here) {
            uint32_t inner = start - pos;
            uint32_t n = here - inner;
            if (n > len - done) n = len - done;
            uint32_t sec_off = (idx == 0 ? 64u : 32u) + inner;
            if (rd(fs, (uint32_t)cur * FS_SECTOR_SIZE + sec_off, out + done, n) != FS_OK) return FS_ERR_IO;
            done += n;
        }
        if (done >= len) break;
        if (rd(fs, (uint32_t)cur * FS_SECTOR_SIZE, h, 32) != FS_OK || !hdr_parse(h, &hd)) return FS_ERR_CORRUPT;
        if (hd.next >= fs->nsec) return FS_ERR_CORRUPT;
        pos += here;
        cur = hd.next;
        idx++;
    }
    return (int)done;
}

/* ---- writing ---------------------------------------------------------------------- */
int fs_writer_begin(fs_writer_t *w, fs_t *fs, const char name[FS_NAME_LEN], uint8_t type,
                    uint16_t load, uint16_t exec, uint32_t total_size)
{
    w->active = false;
    if (total_size > 0x00FFFFFFu) return FS_ERR_ARG;
    uint32_t need = sectors_for(total_size);
    if (need > FS_MAX_SECTORS) return FS_ERR_FULL;
    uint32_t freeb = free_sectors(fs);
    int old = find_index(fs, name);
    uint32_t old_n = old >= 0 ? sectors_for(fs->files[old].size) : 0;
    if (need > freeb) {
        if (old >= 0 && need <= freeb + old_n) {                /* not room for both: drop the old one first */
            int r = fs_delete(fs, name);
            if (r != FS_OK) return r;
            old = -1;
        } else {
            return FS_ERR_FULL;
        }
    }
    w->fs = fs;
    w->old_first = old >= 0 ? fs->files[old].first : -1;
    /* pick the least-worn free sectors */
    for (uint32_t k = 0; k < need; k++) {
        int best = -1;
        for (uint16_t s = 0; s < fs->nsec; s++) {
            if (fs->used[s]) continue;
            if (best < 0 || fs->ecount[s] < fs->ecount[best]) best = s;
        }
        if (best < 0) {                                          /* cannot happen after the check above */
            for (uint32_t j = 0; j < k; j++) fs->used[w->list[j]] = 0;
            return FS_ERR_FULL;
        }
        fs->used[best] = 1;
        w->list[k] = (uint16_t)best;
    }
    w->nsec = (uint16_t)need;
    memcpy(w->name, name, FS_NAME_LEN);
    w->type = type; w->load = load; w->exec = exec;
    w->total = total_size; w->written = 0; w->crc = 0;
    w->gen = fs->next_gen++;
    w->cur = 1; w->fill = 0;
    memset(w->first_buf, 0xFF, FS_SECTOR_SIZE);
    memset(w->buf, 0xFF, FS_SECTOR_SIZE);
    w->active = true;
    return FS_OK;
}

/* write sector list[idx] from img: returns the erase count recorded in it via *ec */
static int write_sector(fs_t *fs, uint16_t s, uint8_t *img, uint8_t kind, uint16_t seq, uint16_t next,
                        uint16_t used, uint32_t gen, uint32_t dcrc)
{
    bool need_erase = !sector_blank(fs, s);
    uint32_t ec = fs->ecount[s] + (need_erase ? 1 : 0);
    hdr_build(img, kind, seq, next, used, gen, ec, dcrc);
    int r = flush_sector(fs, s, img, need_erase);
    if (r == FS_OK) fs->ecount[s] = ec;
    return r;
}

static int flush_cont(fs_writer_t *w)
{
    uint16_t next = (w->cur + 1u < w->nsec) ? w->list[w->cur + 1] : NONE16;
    uint32_t dcrc = fs_crc32(0, w->buf + 32, w->fill);
    int r = write_sector(w->fs, w->list[w->cur], w->buf, K_CONT, w->cur, next, (uint16_t)w->fill, w->gen, dcrc);
    memset(w->buf, 0xFF, FS_SECTOR_SIZE);
    w->fill = 0;
    w->cur++;
    return r;
}

int fs_writer_write(fs_writer_t *w, const void *data, uint32_t len)
{
    if (!w->active || w->written + len > w->total) return FS_ERR_ARG;
    const uint8_t *p = (const uint8_t *)data;
    w->crc = fs_crc32(w->crc, p, len);
    while (len) {
        if (w->written < FS_FIRST_DATA) {
            uint32_t n = FS_FIRST_DATA - (uint32_t)w->written;
            if (n > len) n = len;
            memcpy(w->first_buf + 64 + w->written, p, n);
            w->written += n; p += n; len -= n;
        } else {
            uint32_t room = FS_CONT_DATA - w->fill;
            uint32_t n = room < len ? room : len;
            memcpy(w->buf + 32 + w->fill, p, n);
            w->fill += n; w->written += n; p += n; len -= n;
            if (w->fill == FS_CONT_DATA) {
                int r = flush_cont(w);
                if (r != FS_OK) { fs_writer_abort(w); return r; }
            }
        }
    }
    return FS_OK;
}

int fs_writer_finish(fs_writer_t *w)
{
    if (!w->active || w->written != w->total) { fs_writer_abort(w); return FS_ERR_ARG; }
    fs_t *fs = w->fs;
    int r;
    if (w->fill > 0) {
        r = flush_cont(w);
        if (r != FS_OK) { fs_writer_abort(w); return r; }
    }
    /* the first sector last: this is the commit */
    uint8_t *f = w->first_buf;
    memcpy(f + M_NAME, w->name, FS_NAME_LEN);
    f[M_TYPE] = w->type;
    wr16(f + M_LOAD, w->load);
    wr16(f + M_EXEC, w->exec);
    wr32(f + M_SIZE, w->total);
    wr32(f + M_CRC, w->crc);
    uint32_t in_first = w->total < FS_FIRST_DATA ? w->total : FS_FIRST_DATA;
    uint16_t next = w->nsec > 1 ? w->list[1] : NONE16;
    r = write_sector(fs, w->list[0], f, K_FIRST, 0, next, (uint16_t)in_first, w->gen, fs_crc32(0, f + 64, in_first));
    if (r != FS_OK) { fs_writer_abort(w); return r; }

    fs_entry_t e;
    memcpy(e.name, w->name, FS_NAME_LEN);
    e.type = w->type; e.load = w->load; e.exec = w->exec;
    e.size = w->total; e.crc = w->crc; e.first = w->list[0]; e.gen = w->gen;

    if (w->old_first >= 0) {                                    /* the new file is safe: retire the old one */
        for (int i = 0; i < fs->nfiles; i++) {
            if (fs->files[i].first == (uint16_t)w->old_first) {
                uint16_t list[FS_MAX_SECTORS];
                bool ok = chain_walk(fs, &fs->files[i], list);
                uint32_t n = sectors_for(fs->files[i].size);
                mark_deleted(fs, fs->files[i].first);
                if (ok) for (uint32_t k = 0; k < n; k++) fs->used[list[k]] = 0;
                else fs->used[fs->files[i].first] = 0;
                remove_index(fs, i);
                break;
            }
        }
    }
    insert_sorted(fs, &e);
    w->active = false;
    return FS_OK;
}

void fs_writer_abort(fs_writer_t *w)
{
    if (!w->active) return;
    for (uint16_t k = 0; k < w->nsec; k++) w->fs->used[w->list[k]] = 0;
    w->active = false;
}

int fs_delete(fs_t *fs, const char name[FS_NAME_LEN])
{
    int idx = find_index(fs, name);
    if (idx < 0) return FS_ERR_NOTFOUND;
    uint16_t list[FS_MAX_SECTORS];
    fs_entry_t e = fs->files[idx];
    bool ok = chain_walk(fs, &e, list);
    int r = mark_deleted(fs, e.first);
    if (r != FS_OK) return r;
    if (ok) {
        uint32_t n = sectors_for(e.size);
        for (uint32_t k = 0; k < n; k++) fs->used[list[k]] = 0;
    } else {
        fs->used[e.first] = 0;
    }
    remove_index(fs, idx);
    return FS_OK;
}

int fs_format(fs_t *fs)
{
    while (fs->nfiles > 0) {
        char name[FS_NAME_LEN];
        memcpy(name, fs->files[fs->nfiles - 1].name, FS_NAME_LEN);
        int r = fs_delete(fs, name);
        if (r != FS_OK) return r;
    }
    return FS_OK;
}

int fs_rename(fs_t *fs, const char from[FS_NAME_LEN], const char to[FS_NAME_LEN], fs_writer_t *w)
{
    int si = find_index(fs, from);
    if (si < 0) return FS_ERR_NOTFOUND;
    if (find_index(fs, to) >= 0) return FS_ERR_EXISTS;
    fs_entry_t src = fs->files[si];
    int r = fs_writer_begin(w, fs, to, src.type, src.load, src.exec, src.size);
    if (r != FS_OK) return r;
    uint8_t chunk[FS_PAGE_SIZE * 4];
    uint32_t off = 0;
    while (off < src.size) {
        uint32_t n = src.size - off > sizeof chunk ? (uint32_t)sizeof chunk : src.size - off;
        int got = fs_read(fs, &src, off, chunk, n);
        if (got != (int)n) { fs_writer_abort(w); return got < 0 ? got : FS_ERR_IO; }
        r = fs_writer_write(w, chunk, n);
        if (r != FS_OK) return r;
        off += n;
    }
    r = fs_writer_finish(w);
    if (r != FS_OK) return r;
    return fs_delete(fs, from);
}
