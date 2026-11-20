/* Host tests for src/fs.c: RAM-backed NOR flash simulator with real flash
   constraints and power-failure injection at every flash operation.
   Build: cc -O1 -g -Wall -Wextra -Isrc tests/fs_test.c src/fs.c -o fs_test */
#include "fs.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <setjmp.h>

static int failures = 0, checks = 0;
#define CHECK(cond, ...) do { checks++; if (!(cond)) { failures++; printf("FAIL %s:%d: ", __FILE__, __LINE__); printf(__VA_ARGS__); printf("\n"); } } while (0)

/* ---------------------------------------------------------------- flash simulator */
#define SECTORS 64
static uint8_t flash[SECTORS * FS_SECTOR_SIZE];
static long ops = 0, fault_at = -1;
static int fault_mode = 0;       /* 0 = before, 1 = half applied, 2 = fully applied, 3 = random subset of the bits */
static uint32_t rng = 1;
static uint8_t rnd8(void) { rng = rng * 1664525u + 1013904223u; return (uint8_t)(rng >> 24); }
static jmp_buf fault_jmp;
static uint32_t erases[SECTORS];

static int f_read(void *c, uint32_t off, void *buf, uint32_t len)
{
    (void)c;
    if (off + len > sizeof flash) return -1;
    memcpy(buf, flash + off, len);
    return 0;
}
static int f_erase(void *c, uint32_t off)
{
    (void)c;
    if (off % FS_SECTOR_SIZE || off >= sizeof flash) { printf("BAD ERASE ALIGN %u\n", off); failures++; return -1; }
    if (ops++ == fault_at) {
        if (fault_mode == 1) memset(flash + off, 0xFF, FS_SECTOR_SIZE / 2);
        if (fault_mode == 2) { memset(flash + off, 0xFF, FS_SECTOR_SIZE); erases[off / FS_SECTOR_SIZE]++; }
        longjmp(fault_jmp, 1);
    }
    memset(flash + off, 0xFF, FS_SECTOR_SIZE);
    erases[off / FS_SECTOR_SIZE]++;
    return 0;
}
static int f_program(void *c, uint32_t off, const void *buf, uint32_t len)
{
    (void)c;
    if (off % FS_PAGE_SIZE || len % FS_PAGE_SIZE || off + len > sizeof flash) { printf("BAD PROGRAM ALIGN %u %u\n", off, len); failures++; return -1; }
    uint32_t n = len;
    if (ops++ == fault_at) {
        if (fault_mode == 0) n = 0;
        else if (fault_mode == 1) n = len / 2;
        for (uint32_t i = 0; i < n; i++) flash[off + i] &= ((const uint8_t *)buf)[i];
        if (fault_mode == 3)        /* every byte only partly programmed: bits flip 1 -> 0 at random */
            for (uint32_t i = 0; i < len; i++) { uint8_t want = ((const uint8_t *)buf)[i]; flash[off + i] &= (uint8_t)(want | rnd8()); }
        longjmp(fault_jmp, 1);
    }
    for (uint32_t i = 0; i < n; i++) flash[off + i] &= ((const uint8_t *)buf)[i];   /* only 1 -> 0 */
    return 0;
}
static fs_flash_t dev = { f_read, f_erase, f_program, NULL, sizeof flash };

static void flash_reset(void) { memset(flash, 0xFF, sizeof flash); memset(erases, 0, sizeof erases); ops = 0; fault_at = -1; }

/* ---------------------------------------------------------------- helpers */
static fs_t fs;
static fs_writer_t wr;

static void pattern(uint8_t *p, uint32_t n, uint32_t seed)
{
    uint32_t x = seed * 2654435761u + 12345;
    for (uint32_t i = 0; i < n; i++) { x = x * 1664525u + 1013904223u; p[i] = (uint8_t)(x >> 24); }
}
static void nm(const char *s, char out[FS_NAME_LEN]) { fs_parse_name(s, strlen(s), "BIN", out); }

static int put(const char *name, uint32_t size, uint32_t seed, uint8_t type)
{
    char n[FS_NAME_LEN]; nm(name, n);
    uint8_t *d = malloc(size ? size : 1);
    pattern(d, size, seed);
    int r = fs_writer_begin(&wr, &fs, n, type, 0x0800 + (uint16_t)seed, 0x0900, size);
    if (r == FS_OK) {
        /* feed in odd-sized chunks to exercise the streaming path */
        uint32_t off = 0;
        while (off < size && r == FS_OK) { uint32_t c = 1 + (off * 7 + 13) % 997; if (c > size - off) c = size - off; r = fs_writer_write(&wr, d + off, c); off += c; }
        if (r == FS_OK) r = fs_writer_finish(&wr);
    }
    free(d);
    return r;
}

/* returns 1 if the file is present and its content is pattern(size, seed) */
static int verify(const char *name, uint32_t size, uint32_t seed)
{
    char n[FS_NAME_LEN]; nm(name, n);
    const fs_entry_t *e = fs_find(&fs, n);
    if (!e || e->size != size) return 0;
    uint8_t *got = malloc(size ? size : 1), *want = malloc(size ? size : 1);
    int got_n = fs_read(&fs, e, 0, got, size);
    pattern(want, size, seed);
    int ok = got_n == (int)size && memcmp(got, want, size) == 0 && fs_crc32(0, got, size) == e->crc;
    free(got); free(want);
    return ok;
}

/* ---------------------------------------------------------------- tests */
static void test_basics(void)
{
    CHECK(fs_crc32(0, "123456789", 9) == 0xCBF43926u, "crc32 check value");
    CHECK(fs_crc32(fs_crc32(0, "1234", 4), "56789", 5) == 0xCBF43926u, "crc32 chaining");

    char n[FS_NAME_LEN], txt[13];
    CHECK(fs_parse_name("game.bin", 8, "BAS", n) == FS_OK && memcmp(n, "GAME    BIN", 11) == 0, "parse game.bin");
    CHECK(fs_parse_name("Hello", 5, "BAS", n) == FS_OK && memcmp(n, "HELLO   BAS", 11) == 0, "default ext");
    CHECK(fs_parse_name("A.B", 3, NULL, n) == FS_OK && memcmp(n, "A       B  ", 11) == 0, "short names");
    CHECK(fs_parse_name("TOOLONGNAME", 11, NULL, n) == FS_ERR_BADNAME, "9+ chars rejected");
    CHECK(fs_parse_name("X.ABCD", 6, NULL, n) == FS_ERR_BADNAME, "4 char ext rejected");
    CHECK(fs_parse_name("A B", 3, NULL, n) == FS_ERR_BADNAME, "space rejected");
    CHECK(fs_parse_name("", 0, NULL, n) == FS_ERR_BADNAME, "empty rejected");
    CHECK(fs_parse_name(".BIN", 4, NULL, n) == FS_ERR_BADNAME, "no base name rejected");
    CHECK(fs_parse_name("A.B.C", 5, NULL, n) == FS_ERR_BADNAME, "two dots rejected");
    fs_parse_name("game.bin", 8, NULL, n); fs_format_name(n, txt);
    CHECK(strcmp(txt, "GAME.BIN") == 0, "format name: %s", txt);

    flash_reset();
    CHECK(fs_mount(&fs, &dev) == 0, "empty flash mounts with 0 files");
    CHECK(fs_free_bytes(&fs) == SECTORS * FS_CONT_DATA, "all free");

    /* sizes around the sector boundaries */
    uint32_t sizes[] = {0, 1, 100, FS_FIRST_DATA - 1, FS_FIRST_DATA, FS_FIRST_DATA + 1,
                        FS_FIRST_DATA + FS_CONT_DATA - 1, FS_FIRST_DATA + FS_CONT_DATA, FS_FIRST_DATA + FS_CONT_DATA + 1,
                        20000, 65535};
    for (unsigned i = 0; i < sizeof sizes / sizeof *sizes; i++) {
        char name[16]; snprintf(name, sizeof name, "F%u", i);
        CHECK(put(name, sizes[i], 100 + i, 1) == FS_OK, "write size %u", sizes[i]);
    }
    for (unsigned i = 0; i < sizeof sizes / sizeof *sizes; i++) {
        char name[16]; snprintf(name, sizeof name, "F%u", i);
        CHECK(verify(name, sizes[i], 100 + i), "verify size %u", sizes[i]);
    }
    /* metadata and persistence across a remount */
    CHECK(fs_mount(&fs, &dev) == (int)(sizeof sizes / sizeof *sizes), "remount finds all files");
    for (unsigned i = 0; i < sizeof sizes / sizeof *sizes; i++) {
        char name[16]; snprintf(name, sizeof name, "F%u", i);
        CHECK(verify(name, sizes[i], 100 + i), "verify after remount size %u", sizes[i]);
    }
    nm("F3", n);
    const fs_entry_t *e = fs_find(&fs, n);
    CHECK(e && e->type == 1 && e->load == 0x0800 + 103 && e->exec == 0x0900, "metadata kept");
    /* random access reads */
    nm("F9", n); e = fs_find(&fs, n);
    uint8_t *want = malloc(20000), got[500];
    pattern(want, 20000, 109);
    int ok = 1;
    for (uint32_t off = 0; off < 20000; off += 777) {
        int g = fs_read(&fs, e, off, got, 500);
        uint32_t expect = 20000 - off < 500 ? 20000 - off : 500;
        if (g != (int)expect || memcmp(got, want + off, expect)) ok = 0;
    }
    CHECK(ok, "random access reads");
    free(want);
    /* directory is sorted */
    int sorted = 1;
    for (int i = 1; i < fs_count(&fs); i++) if (memcmp(fs_entry(&fs, i - 1)->name, fs_entry(&fs, i)->name, 11) >= 0) sorted = 0;
    CHECK(sorted, "directory sorted");
}

static void test_overwrite_delete_rename_full(void)
{
    flash_reset(); fs_mount(&fs, &dev);
    CHECK(put("A", 9000, 1, 1) == FS_OK, "A");
    CHECK(put("B", 300, 2, 0) == FS_OK, "B");
    uint32_t free1 = fs_free_bytes(&fs);
    CHECK(put("A", 5000, 3, 1) == FS_OK, "overwrite A");
    CHECK(verify("A", 5000, 3) && verify("B", 300, 2), "A new, B untouched");
    CHECK(fs_count(&fs) == 2, "still 2 files");
    CHECK(fs_free_bytes(&fs) > free1 - 1, "overwrite freed the old sectors");

    char n[FS_NAME_LEN], m[FS_NAME_LEN];
    nm("B", n); nm("C", m);
    CHECK(fs_rename(&fs, n, m, &wr) == FS_OK, "rename");
    CHECK(!fs_find(&fs, n) && verify("C", 300, 2), "renamed content intact");
    nm("A", n);
    CHECK(fs_rename(&fs, n, m, &wr) == FS_ERR_EXISTS, "rename onto existing refused");
    nm("NOPE", n);
    CHECK(fs_delete(&fs, n) == FS_ERR_NOTFOUND, "delete missing");
    nm("C", n);
    CHECK(fs_delete(&fs, n) == FS_OK && !fs_find(&fs, n), "delete");
    fs_mount(&fs, &dev);
    CHECK(fs_count(&fs) == 1 && verify("A", 5000, 3), "deleted file stays deleted after remount");

    /* fill the disk */
    flash_reset(); fs_mount(&fs, &dev);
    int made = 0;
    for (;;) { char name[16]; snprintf(name, sizeof name, "X%d", made); if (put(name, 30000, 500 + made, 1) != FS_OK) break; made++; }
    CHECK(made >= 6 && made <= 8, "filled with %d files of 30000 bytes", made);
    CHECK(put("BIG", SECTORS * FS_SECTOR_SIZE, 1, 1) == FS_ERR_FULL, "too big -> FULL");
    for (int i = 0; i < made; i++) { char name[16]; snprintf(name, sizeof name, "X%d", i); CHECK(verify(name, 30000, 500 + i), "file %d intact after FULL", i); }
    /* overwrite when there is no room for both copies: the old one is dropped first */
    CHECK(put("X0", 30000, 900, 1) == FS_OK && verify("X0", 30000, 900), "overwrite on a full disk");
    fs_format(&fs);
    CHECK(fs_count(&fs) == 0 && fs_free_bytes(&fs) == fs_total_bytes(&fs), "format frees everything");
    fs_mount(&fs, &dev);
    CHECK(fs_count(&fs) == 0, "format survives remount");
    CHECK(put("AFTER", 40000, 7, 1) == FS_OK && verify("AFTER", 40000, 7), "usable after format");
}

static void test_wear(void)
{
    flash_reset(); fs_mount(&fs, &dev);
    put("KEEP", 20000, 1, 1);                    /* a few static files */
    put("KEEP2", 20000, 2, 1);
    for (int i = 0; i < 400; i++) put("TEMP", 3000 + (i % 5) * 4000, i, 0);
    uint32_t mn = 0xFFFFFFFF, mx = 0;
    int used_sectors = 0;
    for (int s = 0; s < SECTORS; s++) { if (fs.used[s]) { used_sectors++; continue; } if (erases[s] < mn) mn = erases[s]; if (erases[s] > mx) mx = erases[s]; }
    printf("   wear: free-pool sectors erased between %u and %u times (static files hold %d sectors)\n", mn, mx, used_sectors);
    CHECK(mx - mn <= 3, "erase counts are level (spread %u)", mx - mn);
}

/* power failure at every flash operation of a mixed workload */
static int scenario_ops(int cut, int mode)
{
    flash_reset();
    fs_mount(&fs, &dev);
    put("A", 10000, 11, 1);
    put("B", 200, 12, 0);
    fs_format(&fs); fs_mount(&fs, &dev);        /* leave junk behind to be recycled */
    put("A", 10000, 11, 1);
    put("B", 200, 12, 0);
    put("D2", 9000, 14, 1);
    ops = 0; fault_at = cut; fault_mode = mode;
    if (setjmp(fault_jmp) == 0) {
        char n[FS_NAME_LEN], m[FS_NAME_LEN];
        put("A", 6000, 21, 1);                   /* overwrite */
        nm("B", n); fs_delete(&fs, n);           /* delete */
        put("C", 20000, 31, 1);                  /* create */
        nm("C", n); nm("D", m); fs_rename(&fs, n, m, &wr);   /* copy + delete */
        nm("D2", n); nm("D3", m); fs_rename(&fs, n, m, &wr);
        fault_at = -1;
        return (int)ops;                         /* no fault happened: ops = total */
    }
    return -1;
}

static void test_power_fail(void)
{
    int total = scenario_ops(1000000, 0);
    CHECK(total > 50, "scenario has %d flash operations", total);
    printf("   power-fail sweep: %d flash operations x 4 failure modes\n", total);
    int bad = 0, recovered_new = 0, kept_old = 0;
    for (int mode = 0; mode < 4; mode++) {
        for (int cut = 0; cut < total; cut++) {
            if (scenario_ops(cut, mode) != -1) { CHECK(0, "fault did not fire at %d", cut); continue; }
            fault_at = -1;
            int n = fs_mount(&fs, &dev);
            char what[160] = "";
            int ok = n >= 0;
            /* A is either the old or the new version, never anything else */
            int a_old = verify("A", 10000, 11), a_new = verify("A", 6000, 21);
            if (!(a_old || a_new)) { ok = 0; strcat(what, "A corrupt;"); }
            if (a_new) recovered_new++; else kept_old++;
            const fs_entry_t *e;
            char nb[FS_NAME_LEN]; nm("B", nb);
            if ((e = fs_find(&fs, nb)) != NULL && !verify("B", 200, 12)) { ok = 0; strcat(what, "B corrupt;"); }
            nm("C", nb);
            if (fs_find(&fs, nb) && !verify("C", 20000, 31)) { ok = 0; strcat(what, "C corrupt;"); }
            nm("D", nb);
            if (fs_find(&fs, nb) && !verify("D", 20000, 31)) { ok = 0; strcat(what, "D corrupt;"); }
            /* a rename must never lose the file */
            nm("D2", nb); char nb3[FS_NAME_LEN]; nm("D3", nb3);
            int d2 = verify("D2", 9000, 14), d3 = verify("D3", 9000, 14);
            if (!d2 && !d3) { ok = 0; strcat(what, "D2 lost;"); }
            for (int i = 0; i < fs_count(&fs); i++) {      /* every listed file must pass its CRC */
                const fs_entry_t *x = fs_entry(&fs, i);
                uint8_t *buf = malloc(x->size ? x->size : 1);
                if (fs_read(&fs, x, 0, buf, x->size) != (int)x->size || fs_crc32(0, buf, x->size) != x->crc) { ok = 0; strcat(what, "crc;"); }
                free(buf);
            }
            /* the disk must still be fully usable (dirty sectors get recycled) */
            if (put("Z", 3000, 99, 1) != FS_OK || !verify("Z", 3000, 99)) { ok = 0; strcat(what, "unusable after recovery;"); }
            if (put("BIGZ", 60000, 98, 1) != FS_OK && put("BIGZ", 30000, 98, 1) != FS_OK) { ok = 0; strcat(what, "no space after recovery;"); }
            if (!ok) { bad++; if (bad < 6) printf("   FAIL cut=%d mode=%d: %s\n", cut, mode, what); }
        }
    }
    CHECK(bad == 0, "%d of %d power-failure cases ended inconsistent", bad, total * 4);
    printf("   after a failure the overwritten file was the OLD version %d times and the NEW one %d times\n", kept_old, recovered_new);
}

int main(void)
{
    test_basics();
    test_overwrite_delete_rename_full();
    test_wear();
    test_power_fail();
    printf("\n%d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
