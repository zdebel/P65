#include "keyboard.h"
#include "pcal6524.h"
#include "mapper.h"
#include "pico/platform.h"
#include "pico/time.h"
#include "hardware/sync.h"
#include <stdio.h>

/* Key matrix: 7 rows x 8 columns. Codes below 32 are ASCII control codes; the
   cursor keys send 17 (left) and 18 (right), and Ctrl (130) and Shift (131) are
   modifiers that never generate a character themselves (see SHIFT_/CTRL_ROW). */

static const char r0[] = {27, '1', '2', '3', '4', '5', '6', '7'};
static const char r1[] = {'8', '9', '0', '-', '=', 8, '\t', 'Q'};
static const char r2[] = {'W', 'E', 'R', 'T', 'Y', 'U', 'I', 'O'};
static const char r3[] = {'P', '[', ']', 17, 130, 'A', 'S', 'D'};
static const char r4[] = {'F', 'G', 'H', 'J', 'K', 'L', ';', '\''};
static const char r5[] = {'`', 18, 131, 'Z', 'X', 'C', 'V', 'B'};
static const char r6[] = {'N', 'M', ',', '.', '/', '\\', ' ', '\r'};
static const char *const rows[] = {r0, r1, r2, r3, r4, r5, r6};

static const char lr0[] = {27, '!', '@', '#', '$', '%', '^', '&'};
static const char lr1[] = {'*', '(', ')', '_', '+', 8, '\t', 'q'};
static const char lr2[] = {'w', 'e', 'r', 't', 'y', 'u', 'i', 'o'};
static const char lr3[] = {'p', '{', '}', 17, 130, 'a', 's', 'd'};
static const char lr4[] = {'f', 'g', 'h', 'j', 'k', 'l', ':', '"'};
static const char lr5[] = {'~', 18, 131, 'z', 'x', 'c', 'v', 'b'};
static const char lr6[] = {'n', 'm', '<', '>', '?', '|', ' ', '\r'};
static const char *const lrows[] = {lr0, lr1, lr2, lr3, lr4, lr5, lr6};

typedef enum
{
    WRITE_ROW = 0,
    WRITE_ROW2,
    SCHEDULE_READ_COLUMN,
    READ_COLUMN,
    WRITE_P2
} keyboard_states;

#define KEY_ROWS                    7

/* Time between the starts of two full matrix scans (a scan itself takes
   ~1 ms on the 1 MHz I2C bus). */
#define KEYBOARD_SCAN_INTERVAL_US   5000
/* After a row is switched on the column lines need time to settle (the previous
   row goes high-impedance and 100k pull-downs plus cable capacitance discharge
   the columns). Reading too early shows the previous row's keys again as
   phantom keys in the new row: wrong / doubled characters. This used to be
   hidden by the slow polling; now the delay is enforced explicitly. */
#define KEYBOARD_ROW_SETTLE_US      170
/* A new key must be seen in this many consecutive scans before it is reported
   (filters one-off misreads; adds ~5 ms of latency). */
#define KEYBOARD_PRESS_SCANS        2
/* A key counts as released only after it has been absent for this many
   consecutive scans (6 x 5 ms = 30 ms). A press is reported on the first scan
   that sees it. Filters contact bounce, which caused double characters. */
#define KEYBOARD_RELEASE_SCANS      6
/* Pressed keys waiting for the CPU to take the previous one (fast typing). */
#define KEYBOARD_QUEUE_SIZE         8

/* modifier positions in the matrix - never generate characters themselves */
#define CTRL_ROW    3
#define CTRL_MASK   0x10
#define SHIFT_ROW   5
#define SHIFT_MASK  0x04

static int current_row = 0;
static keyboard_states state = 0;
/* Output port 2 of the expander. It has two owners so that read-modify-write
   never races between the cores:
     p2_ctrl - bits 0..4 (CPU reset / IRQ / NMI, LCD reset): core 0 only
     p2_leds - bits 5..7 (LEDs, active low): core 1 (CPU writes to $8000)
   p2_written = 0 means "the expander still has to be updated". */
static volatile uint8_t p2_ctrl = 0x1f;
static volatile uint8_t p2_leds = 0x00;
static volatile uint8_t p2_written = 1;

/* IRQ diagnostics, see video.c (build with -DVIDEO_IRQ_DEBUG=1) */
#ifndef VIDEO_IRQ_DEBUG
#define VIDEO_IRQ_DEBUG 0
#endif
#if VIDEO_IRQ_DEBUG
volatile uint32_t kbd_dbg_irq_asserts = 0;     /* times the IRQ line was driven low */
volatile uint32_t kbd_dbg_p2_writes = 0;       /* P2 writes sent to the expander */
#endif
static uint64_t next_scan_time_us = 0;

/* Keyboard register $8001 (Apple II style): bit 7 = "new key" strobe, cleared
   when the CPU writes IO_KEY. last_key and its copy cpu_memory[IO_KEY] (which
   core 1 serves to the CPU) are written by both cores -> kbd_lock. */
static volatile uint8_t last_key = 0;
static spin_lock_t *kbd_lock;

static uint8_t raw_rows[KEY_ROWS];          /* keys seen in the scan in progress */
static uint8_t stable_rows[KEY_ROWS];       /* debounced "key is down" state */
static uint8_t up_scans[KEY_ROWS][8];       /* scans since a down key was last seen */
static uint8_t down_scans[KEY_ROWS][8];     /* consecutive scans a not yet reported key was seen */
static uint32_t row_start_us = 0;           /* when the current row was switched */

static uint8_t key_queue[KEYBOARD_QUEUE_SIZE];
static uint8_t queue_head = 0, queue_tail = 0;

void keyboard_init(void)
{
    kbd_lock = spin_lock_init(spin_lock_claim_unused(true));

    // all output pins in PP mode
    pcal6524_write_register(0x5c, 0x00);

    // port 1 output
    pcal6524_write_register(0x71, 0x00);
    pcal6524_write_register(0x0d, 0x80);
    // port 2 output
    pcal6524_write_register(0x72, 0x00);
    pcal6524_write_register(0x0e, 0x00);
    // port 0 pull up/down enable
    pcal6524_write_register(0x4c, 0xff);
    // port 0 pull down
    pcal6524_write_register(0x50, 0x00);
    // disable all rows
    pcal6524_write_register(0x05, 0x00);
    // all high on p2
    pcal6524_write_register(0x06, 0xff);
}

static int queue_empty(void)
{
    return queue_head == queue_tail;
}

static void queue_push(uint8_t ch)
{
    uint8_t next = (queue_tail + 1) % KEYBOARD_QUEUE_SIZE;
    if (next != queue_head)     /* full -> drop the key */
    {
        key_queue[queue_tail] = ch;
        queue_tail = next;
    }
}

/* Hand the next queued key to the CPU once it has taken the previous one. */
static void keyboard_deliver(void)
{
    /* cheap lock-free checks first: this is polled in a tight loop, and taking
       the spinlock every time would keep core 1 (the CPU bus) waiting for it */
    if (queue_empty() || (last_key & 0x80))
    {
        return;
    }
    uint32_t irq = spin_lock_blocking(kbd_lock);
    if (!(last_key & 0x80))
    {
        last_key = key_queue[queue_head] | 0x80;
        cpu_memory[IO_KEY] = last_key;
        queue_head = (queue_head + 1) % KEYBOARD_QUEUE_SIZE;
    }
    spin_unlock(kbd_lock, irq);
}

/* All keys released: $8001 reads 0 again - but only once the CPU has taken
   the pending key, so no key is lost. */
static void keyboard_all_released(void)
{
    if (!queue_empty())
    {
        return;
    }
    uint32_t irq = spin_lock_blocking(kbd_lock);
    if (last_key && !(last_key & 0x80))
    {
        last_key = 0;
        cpu_memory[IO_KEY] = 0;
    }
    spin_unlock(kbd_lock, irq);
}

/* A key went from released to pressed. */
static void keyboard_key_pressed(int row, int bit, int shift, int ctrl)
{
    uint8_t ch = shift ? lrows[row][bit] : rows[row][bit];

    if (ctrl)
    {
        if (ch == '=' || ch == '+')
        {
            printf("INITING BASIC\r\n");
            mapper_init_basic();
            return;
        }
        ch &= 0x1f;
    }
    queue_push(ch);
}

/* Full matrix scan done: debounce every key, report new presses in scan order. */
static void keyboard_scan_finished(void)
{
    int shift = (raw_rows[SHIFT_ROW] & SHIFT_MASK) != 0;
    int ctrl = (raw_rows[CTRL_ROW] & CTRL_MASK) != 0;
    int any_down = 0;

    raw_rows[SHIFT_ROW] &= ~SHIFT_MASK;
    raw_rows[CTRL_ROW] &= ~CTRL_MASK;

    for (int row = 0; row < KEY_ROWS; row++)
    {
        uint8_t raw = raw_rows[row];
        for (int bit = 0; bit < 8; bit++)
        {
            uint8_t mask = 1 << bit;
            if (raw & mask)
            {
                up_scans[row][bit] = 0;
                if (!(stable_rows[row] & mask))
                {
                    if (++down_scans[row][bit] >= KEYBOARD_PRESS_SCANS)
                    {
                        down_scans[row][bit] = 0;
                        stable_rows[row] |= mask;
                        keyboard_key_pressed(row, bit, shift, ctrl);
                    }
                }
            }
            else
            {
                down_scans[row][bit] = 0;
                if ((stable_rows[row] & mask) &&
                    ++up_scans[row][bit] >= KEYBOARD_RELEASE_SCANS)
                {
                    stable_rows[row] &= ~mask;
                }
            }
        }
        any_down |= stable_rows[row];
        raw_rows[row] = 0;
    }

    keyboard_deliver();
    if (!any_down)
    {
        keyboard_all_released();
    }
}

/* Send P2 (LEDs / CPU reset / IRQ line) to the expander. Caller has checked
   that the I2C controller is idle. */
static void keyboard_write_p2(void)
{
    /* clear the flag BEFORE sampling p2: if core 1 changes the LEDs right now,
       p2_written goes back to 0 and the new value is sent next */
    p2_written = 1;
    __dmb();
    if (!pcal6524_write_register_noblock(0x06, p2_ctrl | p2_leds))
    {
        p2_written = 0; /* FIFO busy - retry */
    }
#if VIDEO_IRQ_DEBUG
    else kbd_dbg_p2_writes++;
#endif
}

void keyboard_scan(void)
{
    uint8_t key = 0;

    keyboard_deliver();

    /* At every row boundary (I2C idle, nothing in flight) a pending P2 write
       (LEDs / CPU reset / IRQ line) goes first - so releasing the IRQ line
       takes tens of microseconds, not a whole matrix scan. Between scans
       (row 0) wait for the next scan slot. */
    if (state == WRITE_ROW)
    {
        if (!p2_written)
        {
            state = WRITE_P2;
        }
        else if (current_row == 0 && time_us_64() < next_scan_time_us)
        {
            return;
        }
    }

    switch (state)
    {
    case WRITE_ROW:
    {
        if (pcal6524_write_register_noblock_done())
        {
            if (current_row == 0)
            {
                /* scan really starts now - schedule the next one */
                next_scan_time_us = time_us_64() + KEYBOARD_SCAN_INTERVAL_US;
                /* resend P2 once per scan: LEDs / IRQ line / reset lines then
                   recover by themselves even if a write was ever lost */
                p2_written = 0;
            }
            row_start_us = time_us_32();
            pcal6524_write_register_noblock(0x0d, ~(1 << current_row));
            state = WRITE_ROW2;
        }
    }
    break;
    case WRITE_ROW2:
    {
        if (pcal6524_write_register_noblock_done())
        {
            pcal6524_write_register_noblock(0x05, 1 << current_row);
            state = SCHEDULE_READ_COLUMN;
        }
    }
    break;
    case SCHEDULE_READ_COLUMN:
    {
        if (pcal6524_write_register_noblock_done())
        {
            if (!p2_written)
            {
                /* The bus is idle while the column lines settle: use the time
                   for a pending P2 write, so releasing the CPU's IRQ line does
                   not have to wait for the end of the row. */
                keyboard_write_p2();
            }
            else if ((uint32_t)(time_us_32() - row_start_us) >= KEYBOARD_ROW_SETTLE_US)
            {
                if (pcal6524_schedule_read_register_noblock(0x00))
                {
                    state = READ_COLUMN;
                }
            }
        }
    }
    break;
    case READ_COLUMN:
    {
        if (pcal6524_read_register_noblock(&key))
        {
            if (current_row < KEY_ROWS)
            {
                raw_rows[current_row] = key;
                current_row++;
            }
            else
            {
                /* extra pass after the last row (row 7 selected, nothing connected there) */
                keyboard_scan_finished();
                current_row = 0;
            }
            state = WRITE_ROW;
        }
    }
    break;
    case WRITE_P2:
    {
        if (pcal6524_write_register_noblock_done())
        {
            keyboard_write_p2();
            state = WRITE_ROW;
        }
    }
    break;
    }
}

void keyboard_set_p2(uint8_t val)
{
    p2_ctrl = val & 0x1f;
    p2_leds = val & 0xe0;
    p2_written = 0;
}

/* Drive the CPU's /IRQ line (P2_1, active low). Core 0 only. */
void keyboard_set_irq(int active)
{
    uint8_t c = p2_ctrl;
    uint8_t n = active ? (c & ~(1 << P2_CPU_IRQ)) : (c | (1 << P2_CPU_IRQ));
    if (n != c)
    {
#if VIDEO_IRQ_DEBUG
        if (active) kbd_dbg_irq_asserts++;
#endif
        p2_ctrl = n;
        p2_written = 0;
    }
}

int keyboard_p2_written(void)
{
    return p2_written;
}

/* called on core 1 when the CPU writes $8001 (acknowledge the key) */
void __not_in_flash_func(keyboard_clear_key)(void)
{
    uint32_t irq = spin_lock_blocking(kbd_lock);
    last_key &= 0x7f;
    cpu_memory[IO_KEY] = last_key;
    spin_unlock(kbd_lock, irq);
}

void __not_in_flash_func(keyboard_set_leds)(uint8_t leds)
{
    p2_leds = (uint8_t)(~leds << 5) & 0xe0;
    p2_written = 0;
    cpu_memory[IO_LEDS] = (uint8_t)~(p2_leds >> 5);
}

uint8_t __not_in_flash_func(keyboard_get_leds)(void)
{
    return (uint8_t)~(p2_leds >> 5);
}
