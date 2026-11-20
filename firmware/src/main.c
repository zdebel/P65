/* P65: a Raspberry Pi Pico runs a real W65C02S, emulates its memory and a
   TMS9918 video chip on an ILI9341 LCD.

     core 1: serves the 65C02 bus (65c02.c)
     core 0: renders the picture, scans the keyboard, 60 Hz VSYNC, flash storage
             and the USB protocol (video.c, keyboard.c, storage_fw.c) */

#include <stdio.h>
#include "pico/stdlib.h"
#include "pico/multicore.h"
#include "hardware/structs/bus_ctrl.h"
#include "hardware/vreg.h"
#include "hardware/clocks.h"
#include "pcal6524.h"
#include "keyboard.h"
#include "65c02.h"
#include "mapper.h"
#include "video.h"
#include "storage_fw.h"

#define EXPANDER_ADDR   0x22
#define EXPANDER_SDA    26
#define EXPANDER_SCL    27

/* Send P2 to the expander (core 0 polls the keyboard state machine, which does
   the I2C write) and wait until it has gone out. */
static void set_p2_and_wait(uint8_t value)
{
    keyboard_set_p2(value);
    while (keyboard_p2_written() == 0)
    {
        keyboard_scan();
    }
}

/* Pulse the CPU clock by hand while /RESET is low: the 65C02 needs a few
   clocks to take the reset. */
static void pulse_cpu_clock(void)
{
    gpio_put(CPU_CLK, 1);
    sleep_ms(1);
    gpio_put(CPU_CLK, 0);
    sleep_ms(1);
}

static void reset_cpu(void)
{
    /* P2_IDLE keeps the LCD out of reset and the CPU IRQ/NMI lines released.
       Bits 5-7 = 0: LEDs on until the ROM turns them off. */
    set_p2_and_wait(P2_IDLE);
    pulse_cpu_clock();

    set_p2_and_wait(P2_IDLE & ~(1 << P2_CPU_RST));      /* /RESET low */
    pulse_cpu_clock();

    set_p2_and_wait(P2_IDLE);                           /* /RESET released */
}

int main(void)
{
    /* 200 MHz needs 1.20 V */
    vreg_set_voltage(VREG_VOLTAGE_1_20);
    sleep_ms(10);
    set_sys_clock_khz(200000, true);

    /* clk_peri from the system PLL (200 MHz) so that the LCD's SPI can reach 50 MHz */
    clock_configure(clk_peri,
                    0,
                    CLOCKS_CLK_PERI_CTRL_AUXSRC_VALUE_CLKSRC_PLL_SYS,
                    200000 * KHZ,
                    200000 * KHZ);

    stdio_init_all();                                   /* USB serial */

    pcal6524_init(i2c1, EXPANDER_ADDR, EXPANDER_SDA, EXPANDER_SCL);
    keyboard_init();
    mapper_init();                                      /* 64 KB memory: RAM + ROM image */
    storage_init();                                     /* mount the flash file system, mailbox registers */
    cpu_init();
    video_init();                                       /* VDP emulation + LCD */

    reset_cpu();
    printf("cpu reset done\r\n");

    cpu_clk_enable();
    bus_ctrl_hw->priority = BUSCTRL_BUS_PRIORITY_PROC1_BITS;    /* core 1 (the CPU bus) wins bus arbitration */
    multicore_reset_core1();
    multicore_launch_core1(cpu_core1_loop);

    while (1)
    {
        video_poll();       /* keyboard state machine, storage, 60 Hz VSYNC, CPU IRQ line */
        video_cycle();      /* render one TMS9918 scanline and queue it for the LCD */
    }
}
