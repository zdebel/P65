#ifndef _VIDEO_H
#define _VIDEO_H

#include <stdint.h>
#include "tms9918.h"

extern VrEmuTms9918 *vdp;       /* the emulated TMS9918, shared by both cores */

void video_init(void);
void video_write_addr(uint8_t addr);    /* core 1: CPU wrote the VDP control port */
void video_cycle(void);         /* core 0: render one scanline and queue it for the LCD */
void video_poll(void);          /* core 0: keyboard scan, storage, 60 Hz VSYNC and IRQ line; call often */

#endif
