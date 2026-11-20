#ifndef _PCAL6524_H
#define _PCAL6524_H

#include <stdint.h>
#include "hardware/i2c.h"

extern volatile uint32_t pcal6524_aborts;   /* aborted I2C transfers seen so far */

void pcal6524_init(i2c_inst_t *i2c, uint8_t addr, uint8_t sda, uint8_t scl);
int pcal6524_write_register(uint8_t reg, uint8_t value);                /* blocking */

/* Non-blocking: return 0 when the FIFO has no room (try again), else 2 */
int pcal6524_write_register_noblock(uint8_t reg, uint8_t value);
int pcal6524_schedule_read_register_noblock(uint8_t reg);
/* 1 when the last queued transfer is completely finished (FIFO empty, bus idle) */
int pcal6524_write_register_noblock_done(void);
/* 1 and *dst set when the byte requested by ..._schedule_read_... has arrived */
int pcal6524_read_register_noblock(uint8_t *dst);

#endif
