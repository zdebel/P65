/* Firmware side of the flash storage. The file system, the mailbox executor and
   the USB protocol are hardware independent (fs.c, storage_core.c, usb_proto.c,
   all tested on the PC); this file only supplies:
     - the flash driver (pico_flash: safe against the other core running code),
     - the mailbox registers, which core 1 (the CPU bus) writes and core 0 executes,
     - the USB serial transport. */
#include "storage_fw.h"
#include "storage.h"
#include "usb_proto.h"
#include "mapper.h"
#include <stdio.h>
#include <string.h>
#include "pico/stdlib.h"
#include "pico/stdio_usb.h"
#include "pico/stdio/driver.h"
#include "pico/flash.h"
#include "hardware/flash.h"
#include "hardware/sync.h"

#define STORAGE_OFFSET   (PICO_FLASH_SIZE_BYTES - STORAGE_SIZE_BYTES)

extern char __flash_binary_end;

static fs_t fs;
static fs_writer_t wr_cpu, wr_usb;
static fs_flash_t flash_dev;
static proto_t proto;
static sto_ctx_t ctx;
static volatile uint8_t pending = 0;        /* set by core 1, cleared by core 0 */
static bool mounted = false;

/* ---- flash driver ---------------------------------------------------------------- */
static int fl_read(void *c, uint32_t off, void *buf, uint32_t len)
{
    (void)c;
    memcpy(buf, (const void *)(XIP_BASE + STORAGE_OFFSET + off), len);
    return 0;
}

typedef struct { uint32_t off; const uint8_t *data; uint32_t len; } fl_op_t;

static void do_erase(void *p)
{
    fl_op_t *o = (fl_op_t *)p;
    flash_range_erase(STORAGE_OFFSET + o->off, FLASH_SECTOR_SIZE);
}

static void do_program(void *p)
{
    fl_op_t *o = (fl_op_t *)p;
    flash_range_program(STORAGE_OFFSET + o->off, o->data, o->len);
}

/* flash_safe_execute stops the other core (it sits in a RAM loop meanwhile; the
   65C02 just waits, its clock is stretched) and disables interrupts here */
static int fl_erase(void *c, uint32_t off)
{
    (void)c;
    fl_op_t o = { off, NULL, 0 };
    return flash_safe_execute(do_erase, &o, 1000) == PICO_OK ? 0 : -1;
}

static int fl_program(void *c, uint32_t off, const void *buf, uint32_t len)
{
    (void)c;
    fl_op_t o = { off, (const uint8_t *)buf, len };
    return flash_safe_execute(do_program, &o, 1000) == PICO_OK ? 0 : -1;
}

/* ---- USB transport ------------------------------------------------------------------ */
static void usb_send(void *c, const uint8_t *data, size_t len)
{
    (void)c;
    stdio_usb.out_chars((const char *)data, (int)len);      /* raw bytes straight to the CDC port */
}

static uint32_t now_ms(void) { return to_ms_since_boot(get_absolute_time()); }

/* ---- API --------------------------------------------------------------------------------- */
void storage_init(void)
{
    sto_regs_init(&cpu_memory[IO_STORAGE]);
    stdio_set_translate_crlf(&stdio_usb, false);        /* binary frames must not get CRs */

    flash_dev.read = fl_read;
    flash_dev.erase = fl_erase;
    flash_dev.program = fl_program;
    flash_dev.ctx = NULL;
    flash_dev.size = STORAGE_SIZE_BYTES;

    uint32_t fw_end = (uint32_t)(uintptr_t)&__flash_binary_end - XIP_BASE;
    if (fw_end > STORAGE_OFFSET) {
        printf("storage: firmware (%u bytes) overlaps the storage area at %u - storage disabled\r\n",
               (unsigned)fw_end, (unsigned)STORAGE_OFFSET);
    } else if (fs_mount(&fs, &flash_dev) < 0) {
        printf("storage: cannot mount\r\n");
    } else {
        mounted = true;
        printf("storage: %d files, %u KB free\r\n", fs_count(&fs), (unsigned)(fs_free_bytes(&fs) / 1024));
    }

    ctx.mem = cpu_memory;
    ctx.regs = &cpu_memory[IO_STORAGE];
    ctx.ram_end = RAM_END;
    ctx.fs = &fs;
    ctx.wr = &wr_cpu;
    ctx.ready = mounted;
    proto_init(&proto, &fs, &wr_usb, &wr_cpu, usb_send, NULL);
    proto_set_ready(&proto, mounted);
}

/* core 1, from the CPU bus loop: keep it tiny */
void __not_in_flash_func(storage_reg_write)(uint8_t off, uint8_t val)
{
    uint8_t *r = &cpu_memory[IO_STORAGE];
    switch (off) {
    case STO_CMD:
        if (r[STO_STATUS] & STO_BUSY) return;           /* a command is running: ignore */
        r[STO_CMD] = val;
        r[STO_STATUS] = STO_BUSY;                       /* visible to the CPU before its next read */
        __dmb();
        pending = 1;
        break;
    case STO_STATUS:
    case STO_ID:
    case STO_VERSION:
        break;                                          /* read only */
    default:
        if (off < STO_REG_COUNT) r[off] = val;
        break;
    }
}

void storage_poll(void)
{
    if (pending) {
        pending = 0;
        ctx.ram_end = rom_ram ? 0xFFFF : RAM_END;       /* ROM off: a LOAD may go above the I/O page */
        sto_execute(&ctx);                             /* may block for a while: erasing flash */
    }

    static uint32_t last_us = 0;
    uint32_t t = time_us_32();
    if ((uint32_t)(t - last_us) < 300) return;
    last_us = t;

    uint8_t buf[64];
    int n = 0, c;
    while (n < (int)sizeof buf && (c = getchar_timeout_us(0)) != PICO_ERROR_TIMEOUT) buf[n++] = (uint8_t)c;
    uint32_t ms = now_ms();
    if (n) proto_rx(&proto, buf, (size_t)n, ms);
    proto_tick(&proto, ms);
}

bool storage_usb_busy(void)
{
    return proto_busy(&proto, now_ms());
}
