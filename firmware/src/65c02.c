/* The 65C02 bus: a PIO state machine (65c02_clk.pio) generates the CPU clock,
   multiplexes the address and hands every bus cycle to core 1, which answers
   reads from cpu_memory[] and applies writes (RAM, I/O registers). */

#include "65c02.h"
#include "hardware/gpio.h"
#include "hardware/pio.h"
#include "pico/flash.h"
#include "pico/stdlib.h"
#include "65c02_clk.pio.h"
#include "mapper.h"
#include "keyboard.h"
#include "video.h"

#define CPU_PIO     pio0
#define CPU_SM      0

void cpu_init(void)
{
    gpio_init_mask(CPU_DATA_MASK);
    gpio_init_mask(CPU_ADDR_MASK);
    gpio_init_mask((1 << CPU_B0_EN) | (1 << CPU_B1_EN) | (1 << CPU_RW) | (1 << CPU_CLK));

    gpio_set_dir_in_masked(CPU_DATA_MASK | CPU_ADDR_MASK | (1 << CPU_RW));
    gpio_set_dir_out_masked((1 << CPU_B0_EN) | (1 << CPU_B1_EN) | (1 << CPU_CLK));

    gpio_put_masked((1 << CPU_B0_EN) | (1 << CPU_B1_EN) | (1 << CPU_CLK), 0xffffffff);
}

void cpu_clk_enable(void)
{
    int offset = pio_add_program(CPU_PIO, &clk_program);

    /* hand the pins over to the PIO */
    pio_gpio_init(CPU_PIO, CPU_CLK);
    pio_gpio_init(CPU_PIO, CPU_B0_EN);
    pio_gpio_init(CPU_PIO, CPU_B1_EN);
    pio_gpio_init(CPU_PIO, CPU_RW);
    for (uint pin = CPU_AB0; pin < CPU_D0 + 8; pin++)       /* address and data pins are consecutive */
    {
        pio_gpio_init(CPU_PIO, pin);
    }

    pio_sm_set_consecutive_pindirs(CPU_PIO, CPU_SM, CPU_CLK, 1, true);
    pio_sm_set_consecutive_pindirs(CPU_PIO, CPU_SM, CPU_B0_EN, 2, true);
    pio_sm_set_consecutive_pindirs(CPU_PIO, CPU_SM, CPU_AB0, 8, false);
    pio_sm_set_consecutive_pindirs(CPU_PIO, CPU_SM, CPU_D0, 8, false);

    pio_sm_config c = clk_program_get_default_config(offset);
    sm_config_set_sideset_pins(&c, CPU_CLK);
    sm_config_set_set_pins(&c, CPU_B0_EN, 2);
    sm_config_set_in_pins(&c, CPU_AB0);
    sm_config_set_out_pins(&c, CPU_D0, 8);
    sm_config_set_in_shift(&c, true, true, 32);
    sm_config_set_out_shift(&c, true, true, 8);
    sm_config_set_jmp_pin(&c, CPU_RW);

    /* 28 PIO cycles per CPU cycle at 200 MHz -> 2.000 MHz: 200 / (28 * 2.000) */
    sm_config_set_clkdiv(&c, 3.57142857f);
    pio_sm_init(CPU_PIO, CPU_SM, offset, &c);
    pio_sm_set_enabled(CPU_PIO, CPU_SM, true);
}

/* Core 1. The PIO pushes {A0-A15, D0-D7, R/W} for every bus cycle; a read must
   be answered with one byte within ~125 ns, so everything here is inlined and
   runs from RAM. This is the hot path of mapper_read_fast()/mapper_write_fast()
   (mapper.h) written out by hand - keep the two in step. */
void __not_in_flash_func(cpu_core1_loop)(void)
{
    io_ro_32 * const rxf = &CPU_PIO->rxf[CPU_SM];
    io_rw_32 * const txf = &CPU_PIO->txf[CPU_SM];
    io_ro_32 * const fstat = &CPU_PIO->fstat;
    const uint32_t rx_empty_mask = 1u << (PIO_FSTAT_RXEMPTY_LSB + CPU_SM);
    uint8_t * const mem = cpu_memory;

    /* lets core 0 erase / program flash (storage): this core then waits in RAM */
    flash_safe_execute_core_init();

    while (1)
    {
        while (*fstat & rx_empty_mask)
        {
            tight_loop_contents();
        }

        uint32_t pio_in = *rxf;
        uint16_t addr = (uint16_t)pio_in;

        if (pio_in & 0x01000000)                        /* read */
        {
            if (__builtin_expect(addr == IO_VCTRL, 0))
            {
                /* VDP status: live value (INT/5S/COL are set on core 0),
                   read-and-clear under the VDP status spinlock */
                *txf = vrEmuTms9918ReadStatus(vdp);
            }
            else
            {
                *txf = mem[addr];
                if (__builtin_expect(addr == IO_VDATA, 0))
                {
                    /* read-ahead buffer: the next read finds the next byte in place */
                    vdp->currentAddress++;
                    mem[IO_VDATA] = vdp->vram[vdp->currentAddress & 0x3fff];
                }
            }
        }
        else                                            /* write */
        {
            uint8_t val = (uint8_t)(pio_in >> 16);
            if (__builtin_expect(addr <= RAM_END, 1))
            {
                mem[addr] = val;
            }
            else if (__builtin_expect(addr == IO_VDATA, 1))
            {
                vdp->vram[(vdp->currentAddress++) & 0x3fff] = val;
                mem[IO_VDATA] = vdp->vram[vdp->currentAddress & 0x3fff];
            }
            else if (addr == IO_KEY)
            {
                /* acknowledge key - under the keyboard spinlock, so a key
                   being delivered by core 0 at the same moment is not lost */
                keyboard_clear_key();
            }
            else if (addr <= IO_END)
            {
                mapper_io_write((uint8_t)addr, val);
                mem[IO_VDATA] = vdp->vram[vdp->currentAddress & 0x3fff];
            }
            else if (rom_ram)
            {
                mem[addr] = val;                        /* ROM switched off: RAM above the I/O page */
            }
        }
    }
}
