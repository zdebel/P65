/*
 * Copyright (c) 2021 Troy Schrapel.
 *
 * Permission is hereby granted, free of charge, to any person obtaining a
 * copy of this software and associated documentation files (the "Software"),
 * to deal in the Software without restriction, including without limitation
 * the rights to use, copy, modify, merge, publish, distribute, sublicense,
 * and/or sell copies of the Software, and to permit persons to whom the
 * Software is furnished to do so, subject to the following condition:  The
 * above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 * 
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
 * THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
 * FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
 * DEALINGS IN THE SOFTWARE.
 */

/*
 * P65 modifications: this file is derived from vrEmuTms9918 by Troy Schrapel
 * (license above) and has been changed for the P65, i.e. for the RP2040 and for
 * speed:
 *  - scanlines are rendered straight into 16-bit LCD pixels (vrEmuTms9918ScanLine16,
 *    palette lookup in one pass); the original 8-bit scanline path was removed;
 *  - the time-critical functions run from RAM (__not_in_flash_func), because the
 *    code is executed on both RP2040 cores and must not wait for the flash cache;
 *  - the status register is shared between core 0 (renderer, VSYNC timer) and
 *    core 1 (CPU bus) and is guarded by a spinlock;
 *  - the frame flag / interrupt comes from a 60 Hz timer instead of the renderer,
 *    and the INT output (flag AND R1 bit 5) is available for the CPU /IRQ line;
 *  - unused API and the other platform-specific parts were removed.
 */

#ifndef _VR_EMU_TMS9918_H_
#define _VR_EMU_TMS9918_H_

#include <stdint.h>
#include <stdbool.h>

/* PRIVATE DATA STRUCTURE
 * ---------------------------------------- */
struct vrEmuTMS9918_s;
typedef struct vrEmuTMS9918_s VrEmuTms9918;

typedef enum
{
  TMS_MODE_GRAPHICS_I,
  TMS_MODE_GRAPHICS_II,
  TMS_MODE_TEXT,
  TMS_MODE_MULTICOLOR,
} vrEmuTms9918Mode;

typedef enum
{
  TMS_TRANSPARENT = 0,
  TMS_BLACK,
  TMS_MED_GREEN,
  TMS_LT_GREEN,
  TMS_DK_BLUE,
  TMS_LT_BLUE,
  TMS_DK_RED,
  TMS_CYAN,
  TMS_MED_RED,
  TMS_LT_RED,
  TMS_DK_YELLOW,
  TMS_LT_YELLOW,
  TMS_DK_GREEN,
  TMS_MAGENTA,
  TMS_GREY,
  TMS_WHITE,
} vrEmuTms9918Color;

typedef enum
{
  TMS_REG_0 = 0,
  TMS_REG_1,
  TMS_REG_2,
  TMS_REG_3,
  TMS_REG_4,
  TMS_REG_5,
  TMS_REG_6,
  TMS_REG_7,
  TMS_NUM_REGISTERS,
  TMS_REG_NAME_TABLE        = TMS_REG_2,
  TMS_REG_COLOR_TABLE       = TMS_REG_3,
  TMS_REG_PATTERN_TABLE     = TMS_REG_4,
  TMS_REG_SPRITE_ATTR_TABLE = TMS_REG_5,
  TMS_REG_SPRITE_PATT_TABLE = TMS_REG_6,
  TMS_REG_FG_BG_COLOR       = TMS_REG_7,
} vrEmuTms9918Register;

#define TMS9918_PIXELS_X 256
#define TMS9918_PIXELS_Y 192
#define TMS9918_VRAM_SIZE (1 << 14)

struct vrEmuTMS9918_s
{
  uint8_t vram[TMS9918_VRAM_SIZE];
  uint8_t registers[TMS_NUM_REGISTERS];
  uint8_t status;
  uint8_t lastMode;
  uint16_t currentAddress;
  vrEmuTms9918Mode mode;
  uint8_t rowSpriteBits[TMS9918_PIXELS_X];
};


/* PUBLIC INTERFACE
 * ---------------------------------------- */

 /* Function:  vrEmuTms9918New
  * --------------------
  * create a new TMS9918
  */

VrEmuTms9918* vrEmuTms9918New();

/* Function:  vrEmuTms9918Reset
  * --------------------
  * reset the new TMS9918
  */

void vrEmuTms9918Reset(VrEmuTms9918* tms9918);

/* Function:  vrEmuTms9918WriteAddr
 * --------------------
 * write an address (mode = 1) to the tms9918
 *
 * uint8_t: the data (DB0 -> DB7) to send
 */

void vrEmuTms9918WriteAddr(VrEmuTms9918* tms9918, uint8_t data);

/* Function:  vrEmuTms9918WriteData
 * --------------------
 * write data (mode = 0) to the tms9918
 *
 * uint8_t: the data (DB0 -> DB7) to send
 */

void vrEmuTms9918WriteData(VrEmuTms9918* tms9918, uint8_t data);

/* Function:  vrEmuTms9918ReadStatus
 * --------------------
 * read from the status register
 */

uint8_t vrEmuTms9918ReadStatus(VrEmuTms9918* tms9918);

/* Set the frame flag (called at 60 Hz) / state of the INT output
 * (frame flag AND R1 bit 5) */
void vrEmuTms9918SetInterrupt(VrEmuTms9918* tms9918);
bool vrEmuTms9918InterruptPending(VrEmuTms9918* tms9918);

/* Function:  vrEmuTms9918ReadData
 * --------------------
 * read data (mode = 0) from the tms9918
 */

uint8_t vrEmuTms9918ReadData(VrEmuTms9918* tms9918);

/* Function:  vrEmuTms9918ScanLine16
 * ----------------------------------------
 * render scanline y straight into 16-bit pixels, pal = 16 colour palette
 * (indexed by vrEmuTms9918Color). Also updates the sprite flags in the status
 * register. Core 0 only.
 */
void vrEmuTms9918ScanLine16(VrEmuTms9918* tms9918, uint8_t y, uint16_t pixels[TMS9918_PIXELS_X], const uint16_t *pal);

/* Function:  vrEmuTms9918DisplayEnabled
  * --------------------
  * check BLANK flag
  */

bool vrEmuTms9918DisplayEnabled(VrEmuTms9918* tms9918);


#endif // _VR_EMU_TMS9918_H_
