#ifndef _65C02_H
#define _65C02_H

#include <stdint.h>

/* Pico GPIO <-> W65C02S. The address is multiplexed over GPIO0-7 (A0-A7 and
   A8-A15 are selected by the B0/B1 buffer enables), the data bus is GPIO8-15. */
#define CPU_AB0     0           /* first of 8 multiplexed address pins */
#define CPU_D0      8           /* first of 8 data pins */
#define CPU_RW      16
#define CPU_B0_EN   20          /* 74LVC244 enable: A0-A7 */
#define CPU_B1_EN   21          /* 74LVC244 enable: A8-A15 */
#define CPU_CLK     22

#define CPU_ADDR_MASK   (0xFFu << CPU_AB0)
#define CPU_DATA_MASK   (0xFFu << CPU_D0)

void cpu_init(void);            /* GPIO setup, clock held high */
void cpu_clk_enable(void);      /* start the PIO state machine: clock + bus cycles */
void cpu_core1_loop(void);      /* core 1: serve the CPU's bus cycles, never returns */

#endif
