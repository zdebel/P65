#include "video.h"
#include "tms9918.h"
#include "ili9341.h"
#include "keyboard.h"
#include "storage_fw.h"
#include "mapper.h"
#include <stdint.h>
#include "pico/stdlib.h"
#include "hardware/spi.h"
#include "hardware/dma.h"
#include "hardware/irq.h"
#include "hardware/sync.h"
#include <stdio.h>

/* FPS overlay (debug). Off by default; enable with e.g.
   target_compile_definitions(p65 PRIVATE VIDEO_SHOW_FPS=1) */
#ifndef VIDEO_SHOW_FPS
#define VIDEO_SHOW_FPS 0
#endif

/* IRQ / LCD diagnostics printed once a second on USB serial (build with
   -DVIDEO_IRQ_DEBUG=1) */
#ifndef VIDEO_IRQ_DEBUG
#define VIDEO_IRQ_DEBUG 0
#endif

VrEmuTms9918 *vdp;

/* ---- LCD pipeline ---------------------------------------------------------
   Core 0 renders lines into a small ring of buffers; the DMA streams them to
   the ILI9341 and its completion interrupt immediately starts the next queued
   line, so the SPI link is kept busy no matter what core 0 is doing at that
   moment (USB, I2C keyboard scan, ...). CS stays low for good and the
   controller wraps its write pointer at the end of the 256x192 window, so one
   frame follows the next without any gap. The theoretical limit is the SPI
   link: 98304 bytes at 50 MHz = 15.73 ms per frame (63.6 fps). */
#define LINE_BUFFERS    4           /* power of two */
#define LINE_BYTES      (256 * 2)
#ifndef VIDEO_RESYNC_FRAMES
#define VIDEO_RESYNC_FRAMES 64      /* power of two: send RAMWR again every N frames */
#endif
#ifndef VIDEO_LOCK_TO_VSYNC
#define VIDEO_LOCK_TO_VSYNC 1       /* start frames on the 60 Hz tick when close to it */
#endif
/* SPI mode 3 (CPOL=1, CPHA=1) for the pixel stream. In mode 0 the RP2040's SPI
   (PL022) leaves a gap of ~1.5 clock periods between bytes: 50 MHz behaves like
   ~42 MHz (measured: 53 fps instead of 63). With CPHA=1 the frames follow each
   other back to back. The ILI9341 samples on the rising edge in both modes.
   Set to 0 to go back to mode 0 if the panel does not like it. */
#ifndef VIDEO_SPI_MODE3
#define VIDEO_SPI_MODE3 1
#endif

#define LCD_OFFSET_X    31          /* picture position on the panel */
#define LCD_OFFSET_Y    23

static uint16_t linebuf[LINE_BUFFERS][256];
static volatile uint32_t lines_produced = 0;    /* buffers filled by core 0 */
static volatile uint32_t lines_started = 0;     /* buffers handed to the DMA */
static volatile uint32_t lines_sent = 0;        /* buffers the DMA has finished */
static volatile bool dma_running = false;
static volatile bool video_resyncing = false;
static int scanline = 0;
static uint32_t lcd_frames = 0;
static int dma_chan = -1;
static dma_channel_config dma_cfg;
#if VIDEO_IRQ_DEBUG
static volatile uint32_t dbg_underruns = 0;     /* DMA ran out of queued lines */
static uint64_t dbg_render_us = 0, dbg_wait_us = 0;   /* core 0: rendering / waiting for a free buffer */
static uint32_t dbg_render_max = 0, dbg_lines = 0;
#endif

static const uint16_t tmspal_bgr16[16] = {
    0x0000, 0x0000, 0x4826, 0xEF5E, 0xBD52, 0xBF7B, 0x89D2, 0x5E47,
    0xAAFA, 0xCFFB, 0x0AD6, 0x70E6, 0x8725, 0xD7CA, 0x79CE, 0xFFFF
};

#if VIDEO_SHOW_FPS
/* Simple 8x8 font for the digits and the word FPS */
static const uint8_t font_digits[10][8] = {
    {0x3C, 0x66, 0x6E, 0x76, 0x66, 0x66, 0x3C, 0x00}, // 0
    {0x18, 0x38, 0x18, 0x18, 0x18, 0x18, 0x3C, 0x00}, // 1
    {0x3C, 0x66, 0x06, 0x0C, 0x18, 0x30, 0x7E, 0x00}, // 2
    {0x3C, 0x66, 0x06, 0x1C, 0x06, 0x66, 0x3C, 0x00}, // 3
    {0x0C, 0x1C, 0x34, 0x64, 0x7E, 0x04, 0x04, 0x00}, // 4
    {0x7E, 0x60, 0x7C, 0x06, 0x06, 0x66, 0x3C, 0x00}, // 5
    {0x1C, 0x30, 0x60, 0x7C, 0x66, 0x66, 0x3C, 0x00}, // 6
    {0x7E, 0x66, 0x06, 0x0C, 0x18, 0x18, 0x18, 0x00}, // 7
    {0x3C, 0x66, 0x66, 0x3C, 0x66, 0x66, 0x3C, 0x00}, // 8
    {0x3C, 0x66, 0x66, 0x3E, 0x06, 0x0C, 0x38, 0x00}, // 9
};
static const uint8_t font_F[8] = {0x7E, 0x60, 0x60, 0x7C, 0x60, 0x60, 0x60, 0x00};
static const uint8_t font_P[8] = {0x7C, 0x66, 0x66, 0x7C, 0x60, 0x60, 0x60, 0x00};
static const uint8_t font_S[8] = {0x3C, 0x66, 0x60, 0x3C, 0x06, 0x66, 0x3C, 0x00};

static const uint8_t* get_glyph(char c) {
    if (c >= '0' && c <= '9') return font_digits[c - '0'];
    if (c == 'F') return font_F;
    if (c == 'P') return font_P;
    if (c == 'S') return font_S;
    static const uint8_t empty[8] = {0};
    return empty;
}

static uint32_t fps_value = 0;
static uint32_t fps_frame_count = 0;
static uint64_t fps_last_time_us = 0;
static char fps_text[16] = "-- FPS";
#endif /* VIDEO_SHOW_FPS */

/* Hand the next queued line buffer to the DMA. Call from the DMA interrupt or
   with interrupts disabled. */
static void __not_in_flash_func(video_dma_start_next)(void)
{
    dma_channel_configure(dma_chan, &dma_cfg, &spi_get_hw(LCD_SPI)->dr,
                          linebuf[lines_started & (LINE_BUFFERS - 1)], LINE_BYTES, true);
    lines_started++;
    dma_running = true;
}

/* A line has been sent: start the next one straight away. */
static void __not_in_flash_func(video_dma_isr)(void)
{
    dma_hw->ints0 = 1u << dma_chan;
    lines_sent++;
    if (lines_started != lines_produced) {
        video_dma_start_next();
    } else {
        dma_running = false;
#if VIDEO_IRQ_DEBUG
        if (!video_resyncing) {
            dbg_underruns++;
        }
#endif
    }
}

/* Queue the buffer core 0 has just finished rendering. */
static inline void __not_in_flash_func(video_publish_line)(void)
{
    uint32_t irq = save_and_disable_interrupts();
    lines_produced++;
    if (!dma_running) {
        video_dma_start_next();
    }
    restore_interrupts(irq);
}

void video_init()
{
    /* vdp stuff */
    vdp = vrEmuTms9918New();
    vrEmuTms9918Reset(vdp);
    ili9341_init();

    // Setup DMA for SPI TX
    dma_chan = dma_claim_unused_channel(true);
    dma_cfg = dma_channel_get_default_config(dma_chan);
    channel_config_set_transfer_data_size(&dma_cfg, DMA_SIZE_8);
    channel_config_set_dreq(&dma_cfg, spi_get_dreq(LCD_SPI, true));
    channel_config_set_read_increment(&dma_cfg, true);
    channel_config_set_write_increment(&dma_cfg, false);

    /* centre the 256x192 picture on the 320x240 panel and start a RAMWR */
    ili9341_set_window(LCD_OFFSET_X, LCD_OFFSET_Y,
                       LCD_OFFSET_X + TMS9918_PIXELS_X - 1, LCD_OFFSET_Y + TMS9918_PIXELS_Y - 1);

#if VIDEO_SPI_MODE3
    /* CS is high here, so the clock edge caused by the polarity change is not seen */
    spi_set_format(LCD_SPI, 8, SPI_CPOL_1, SPI_CPHA_1, SPI_MSB_FIRST);
#endif

    /* From here on CS stays low and DC high: the picture is one endless stream. */
    gpio_put(LCD_PIN_DC, 1);            // DC = DATA
    gpio_put(LCD_PIN_CS, 0);            // CS active

    dma_channel_set_irq0_enabled(dma_chan, true);
    irq_set_exclusive_handler(DMA_IRQ_0, video_dma_isr);
    irq_set_priority(DMA_IRQ_0, 0);     // above USB and the other core-0 interrupts
    irq_set_enabled(DMA_IRQ_0, true);
}

void __not_in_flash_func(video_write_addr)(uint8_t addr)
{
    vrEmuTms9918WriteAddr(vdp, addr);
}

/* ---- VSYNC: 60 Hz frame flag from a timer, independent of the LCD refresh.
   The frame flag lives in the VDP status register; the INT output (flag AND
   R1 bit 5) drives the 65C02 /IRQ pin through the I2C expander (P2_1). The
   flag, and so the line, stays asserted until the CPU reads $8003. ---- */
#define VSYNC_HZ 60

#if VIDEO_IRQ_DEBUG
/* once a second on USB serial: how many VSYNC flags, status reads by the CPU,
   IRQ-line assertions and P2 writes there were */
extern volatile uint32_t tms_dbg_status_reads, tms_dbg_int_reads;
extern volatile uint32_t kbd_dbg_irq_asserts, kbd_dbg_p2_writes;
extern volatile uint32_t pcal6524_aborts;
static uint32_t dbg_vsyncs = 0;
static uint64_t dbg_next_print_us = 0;
#endif
static uint64_t vsync_start_us = 0;
static uint32_t vsync_count = 0;
static uint64_t next_vsync_us = 0;

void __not_in_flash_func(video_poll)()
{
    keyboard_scan();
    mapper_rom_poll();              /* ROM restore after the CPU switched the ROM back on */
    storage_poll();                 /* storage commands from the CPU, USB file transfers */

    uint64_t now = time_us_64();
    if (next_vsync_us == 0) {
        vsync_start_us = now;
        vsync_count = 1;
        next_vsync_us = now + 1000000u / VSYNC_HZ;
    } else if (now >= next_vsync_us) {
        vrEmuTms9918SetInterrupt(vdp);
#if VIDEO_IRQ_DEBUG
        dbg_vsyncs++;
#endif
        vsync_count++;
        /* from the start time, so the 16.667 ms period never drifts */
        next_vsync_us = vsync_start_us + (uint64_t)vsync_count * 1000000u / VSYNC_HZ;
    }

    keyboard_set_irq(vrEmuTms9918InterruptPending(vdp));

#if VIDEO_IRQ_DEBUG
    if (now >= dbg_next_print_us && !storage_usb_busy()) {
        static uint32_t p_v, p_r, p_i, p_a, p_w, p_f, p_u;
        uint32_t v = dbg_vsyncs, r = tms_dbg_status_reads, i = tms_dbg_int_reads;
        uint32_t a = kbd_dbg_irq_asserts, w = kbd_dbg_p2_writes;
        printf("IRQDBG /s: vsync=%lu status_reads=%lu with_INT=%lu irq_asserts=%lu p2_writes=%lu lcd_fps=%lu underruns=%lu render=%lu/%lu us(avg/max per line) core0_waiting=%lu%% spi=%lu Hz mode3=%d i2c_aborts=%lu\r\n",
               (unsigned long)(v - p_v), (unsigned long)(r - p_r), (unsigned long)(i - p_i),
               (unsigned long)(a - p_a), (unsigned long)(w - p_w),
               (unsigned long)(lcd_frames - p_f), (unsigned long)(dbg_underruns - p_u),
               (unsigned long)(dbg_lines ? dbg_render_us / dbg_lines : 0), (unsigned long)dbg_render_max,
               (unsigned long)(dbg_wait_us / 10000), (unsigned long)spi_get_baudrate(LCD_SPI), VIDEO_SPI_MODE3,
               (unsigned long)pcal6524_aborts);
        dbg_render_us = 0; dbg_wait_us = 0; dbg_render_max = 0; dbg_lines = 0;
        p_v = v; p_r = r; p_i = i; p_a = a; p_w = w; p_f = lcd_frames; p_u = dbg_underruns;
        dbg_next_print_us = now + 1000000;
    }
#endif
}

#if VIDEO_SHOW_FPS
/* small FPS badge in the top-right corner, 1:1 8x8 font */
#define FPS_BADGE_LINES  12     /* 2 px padding + 8 px glyph + 2 px padding */

static inline void render_fps_badge(uint16_t *buf, int line)
{
    if (line >= FPS_BADGE_LINES) {
        return;
    }

    int text_len = 0;
    while (fps_text[text_len]) text_len++;
    int text_width = text_len * 8;
    int box_x1 = 256;
    int box_x0 = box_x1 - text_width - 6;

    for (int x = box_x0; x < box_x1; x++) {
        buf[x] = (line == FPS_BADGE_LINES - 1 || x == box_x0) ? 0x7BEF : 0x0000;
    }

    if (line >= 2 && line < 10) {
        int row = line - 2;
        int text_x0 = box_x0 + 3;
        for (int i = 0; i < text_len; i++) {
            uint8_t bits = get_glyph(fps_text[i])[row];
            for (int col = 0; col < 8; col++) {
                if ((bits >> (7 - col)) & 1) {
                    buf[text_x0 + i * 8 + col] = 0xFFFF;
                }
            }
        }
    }
}
#endif

#if VIDEO_LOCK_TO_VSYNC
/* The frame we have just queued needs ~16 ms on the wire, so the next 60 Hz
   tick is usually close: start the next frame exactly on it. The LCD then runs
   at exactly 60 fps and a game that updates VRAM from its VSYNC handler gets a
   consistent picture. When the tick is far away (frames running slow) do not
   wait, so a late frame can never cost a whole tick (30 fps). */
static void video_frame_pace(void)
{
    int64_t left = (int64_t)(next_vsync_us - time_us_64());
    if (next_vsync_us != 0 && left > 0 && left <= 2000) {
        uint32_t tick = vsync_count;
        while (vsync_count == tick) {
            video_poll();
        }
    }
}
#endif

/* Safety net: let everything drain and send RAMWR again, so that a glitch on
   the SPI link can never leave the picture shifted for good. ~15 us, rarely. */
static void video_resync(void)
{
    video_resyncing = true;
    while (lines_sent != lines_produced) {
        video_poll();
    }
    while (spi_is_busy(LCD_SPI)) {
        video_poll();
    }
    video_resyncing = false;
    gpio_put(LCD_PIN_CS, 1);            // CS inactive
#if VIDEO_SPI_MODE3
    spi_set_format(LCD_SPI, 8, SPI_CPOL_0, SPI_CPHA_0, SPI_MSB_FIRST);   // commands in mode 0, as at init
#endif
    ili9341_command(ILI9341_RAMWR);     // write pointer back to the window origin
#if VIDEO_SPI_MODE3
    spi_set_format(LCD_SPI, 8, SPI_CPOL_1, SPI_CPHA_1, SPI_MSB_FIRST);
#endif
    gpio_put(LCD_PIN_DC, 1);            // DC = DATA
    gpio_put(LCD_PIN_CS, 0);            // CS active
}

void __not_in_flash_func(video_cycle)()
{
#if VIDEO_IRQ_DEBUG
    uint64_t dbg_t0 = time_us_64();
#endif
    // A free line buffer appears every ~82 us (the time one line needs on the wire)
    while ((uint32_t)(lines_produced - lines_sent) >= LINE_BUFFERS) {
        video_poll();
    }
#if VIDEO_IRQ_DEBUG
    uint64_t dbg_t1 = time_us_64();
#endif

    uint16_t *buf = linebuf[lines_produced & (LINE_BUFFERS - 1)];

    vrEmuTms9918ScanLine16(vdp, scanline, buf, tmspal_bgr16);

#if VIDEO_SHOW_FPS
    render_fps_badge(buf, scanline);
#endif
#if VIDEO_IRQ_DEBUG
    {
        uint64_t dbg_t2 = time_us_64();
        uint32_t r = (uint32_t)(dbg_t2 - dbg_t1);
        dbg_wait_us += dbg_t1 - dbg_t0;
        dbg_render_us += r;
        if (r > dbg_render_max) dbg_render_max = r;
        dbg_lines++;
    }
#endif

    video_publish_line();

    scanline++;
    if (scanline >= 192) {
        scanline = 0;
        lcd_frames++;

#if VIDEO_SHOW_FPS
        fps_frame_count++;
        uint64_t now = time_us_64();
        uint64_t diff = now - fps_last_time_us;
        if (diff >= 500000ULL) {
            fps_value = (uint32_t)((fps_frame_count * 1000000ULL + diff / 2) / diff);
            fps_frame_count = 0;
            fps_last_time_us = now;

            char *p = fps_text;
            uint32_t v = fps_value;
            if (v >= 100) { *p++ = '0' + (char)((v / 100) % 10); }
            if (v >= 10)  { *p++ = '0' + (char)((v / 10) % 10); }
            *p++ = '0' + (char)(v % 10);
            *p++ = ' ';
            *p++ = 'F';
            *p++ = 'P';
            *p++ = 'S';
            *p = '\0';
        }
#endif

        if ((lcd_frames & (VIDEO_RESYNC_FRAMES - 1)) == 0) {
            video_resync();
        }
#if VIDEO_LOCK_TO_VSYNC
        video_frame_pace();
#endif
    }
}
