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

#include "tms9918.h"
#include <stdlib.h>
#include <string.h>
#include "pico/platform.h"
#include "hardware/sync.h"

/* Status register is shared between core 0 (renderer sets flags) and
 * core 1 (CPU bus reads and clears it) - guarded by this spinlock. */
static spin_lock_t *tmsStatusLock = NULL;

/* IRQ diagnostics (build with -DVIDEO_IRQ_DEBUG=1): status reads by the CPU */
#ifndef VIDEO_IRQ_DEBUG
#define VIDEO_IRQ_DEBUG 0
#endif
#if VIDEO_IRQ_DEBUG
volatile uint32_t tms_dbg_status_reads = 0;
volatile uint32_t tms_dbg_int_reads = 0;      /* reads that returned the frame flag */
#endif

/* status as seen by the scanline currently being rendered (core 0 only) */
static uint8_t scanStatus = 0;

#define VRAM_SIZE (1 << 14) /* 16KB */

#define GRAPHICS_NUM_COLS 32
#define GRAPHICS_NUM_ROWS 24
#define GRAPHICS_CHAR_WIDTH 8

#define TEXT_NUM_COLS 40
#define TEXT_NUM_ROWS 24
#define TEXT_CHAR_WIDTH 6

#define MAX_SPRITES 32
#define SPRITE_ATTR_BYTES 4
#define LAST_SPRITE_VPOS 0xD0
#define MAX_SCANLINE_SPRITES 4

#define STATUS_INT 0x80
#define STATUS_5S  0x40
#define STATUS_COL 0x20


/* Function:  tmsMode
  * --------------------
  * return the current mode
  */
static vrEmuTms9918Mode __not_in_flash_func(tmsMode)(VrEmuTms9918* tms9918)
{
  if (tms9918->registers[TMS_REG_0] & 0x02)
  {
    return TMS_MODE_GRAPHICS_II;
  }

  switch ((tms9918->registers[TMS_REG_1] & 0x18) >> 3)
  {
    case 0:
      return TMS_MODE_GRAPHICS_I;

    case 1:
      return TMS_MODE_MULTICOLOR;

    case 2:
      return TMS_MODE_TEXT;
  }
  return TMS_MODE_GRAPHICS_I;
}


/* Function:  tmsSpriteSize
  * --------------------
  * sprite size (0 = 8x8, 1 = 16x16)
  */
static inline bool tmsSpriteSize(VrEmuTms9918* tms9918)
{
  return tms9918->registers[TMS_REG_1] & 0x02;
}

/* Function:  tmsSpriteMagnification
  * --------------------
  * sprite size (0 = 1x, 1 = 2x)
  */
static inline bool tmsSpriteMag(VrEmuTms9918* tms9918)
{
  return tms9918->registers[TMS_REG_1] & 0x01;
}

/* Function:  tmsNameTableAddr
  * --------------------
  * name table base address
  */
static inline uint16_t tmsNameTableAddr(VrEmuTms9918* tms9918)
{
  return (tms9918->registers[TMS_REG_2] & 0x0f) << 10;
}

/* Function:  tmsColorTableAddr
  * --------------------
  * color table base address
  */
static inline uint16_t tmsColorTableAddr(VrEmuTms9918* tms9918)
{
  if (tms9918->mode == TMS_MODE_GRAPHICS_II)
    return (tms9918->registers[TMS_REG_3] & 0x80) << 6;
  return tms9918->registers[TMS_REG_3] << 6;
}

/* Function:  tmsPatternTableAddr
  * --------------------
  * pattern table base address
  */
static inline uint16_t tmsPatternTableAddr(VrEmuTms9918* tms9918)
{
  if (tms9918->mode == TMS_MODE_GRAPHICS_II)
    return (tms9918->registers[TMS_REG_4] & 0x04) << 11;
  return (tms9918->registers[TMS_REG_4] & 0x07) << 11;
}

/* Function:  tmsSpriteAttrTableAddr
  * --------------------
  * sprite attribute table base address
  */
static inline uint16_t tmsSpriteAttrTableAddr(VrEmuTms9918* tms9918)
{
  return (tms9918->registers[TMS_REG_5] & 0x7f) << 7;
}

/* Function:  tmsSpritePatternTableAddr
  * --------------------
  * sprite pattern table base address
  */
static inline uint16_t tmsSpritePatternTableAddr(VrEmuTms9918* tms9918)
{
  return (tms9918->registers[TMS_REG_6] & 0x07) << 11;
}

/* Function:  tmsBgColor
  * --------------------
  * background color
  */
static inline vrEmuTms9918Color tmsMainBgColor(VrEmuTms9918* tms9918)
{
  return (vrEmuTms9918Color)((vrEmuTms9918DisplayEnabled(tms9918) 
            ? tms9918->registers[TMS_REG_7] 
            : TMS_BLACK) & 0x0f);
}

/* Function:  tmsFgColor
  * --------------------
  * foreground color
  */
static inline vrEmuTms9918Color tmsMainFgColor(VrEmuTms9918* tms9918)
{
  vrEmuTms9918Color c = (vrEmuTms9918Color)(tms9918->registers[TMS_REG_7] >> 4);
  return c == TMS_TRANSPARENT ? tmsMainBgColor(tms9918) : c;
}

/* Function:  tmsFgColor
  * --------------------
  * foreground color
  */
static inline vrEmuTms9918Color tmsFgColor(VrEmuTms9918* tms9918, uint8_t colorByte)
{
  vrEmuTms9918Color c = (vrEmuTms9918Color)(colorByte >> 4);
  return c == TMS_TRANSPARENT ? tmsMainBgColor(tms9918) : c;
}

/* Function:  tmsBgColor
  * --------------------
  * background color
  */
static inline vrEmuTms9918Color tmsBgColor(VrEmuTms9918* tms9918, uint8_t colorByte)
{
  vrEmuTms9918Color c = (vrEmuTms9918Color)(colorByte & 0x0f);
  return c == TMS_TRANSPARENT ? tmsMainBgColor(tms9918) : c;
}


/* Function:  vrEmuTms9918New
  * --------------------
  * create a new TMS9918
  */
 VrEmuTms9918* vrEmuTms9918New()
{
  if (tmsStatusLock == NULL)
  {
    tmsStatusLock = spin_lock_init(spin_lock_claim_unused(true));
  }

  VrEmuTms9918* tms9918 = (VrEmuTms9918*)malloc(sizeof(VrEmuTms9918));
  if (tms9918 != NULL)
  {
    vrEmuTms9918Reset(tms9918);
 }

  return tms9918;
}

/* Function:  vrEmuTms9918Reset
  * --------------------
  * reset the new TMS9918
  */
 void vrEmuTms9918Reset(VrEmuTms9918* tms9918)
{
  if (tms9918)
  {
    /* initialization */
    tms9918->currentAddress = 0;
    tms9918->lastMode = 0;
    tms9918->status = 0;
    memset(tms9918->registers, 0, sizeof(tms9918->registers));
    memset(tms9918->vram, 0xff, sizeof(tms9918->vram));
  }
}


/* Function:  vrEmuTms9918WriteAddr
 * --------------------
 * write an address (mode = 1) to the tms9918
 *
 * data: the data (DB0 -> DB7) to send
 */
 void __not_in_flash_func(vrEmuTms9918WriteAddr)(VrEmuTms9918* tms9918, uint8_t data)
{
  if (tms9918->lastMode)
  {
    /* second address byte */

    if (data & 0x80) /* register */
    {
      tms9918->registers[data & 0x07] = tms9918->currentAddress & 0xff;

      tms9918->mode = tmsMode(tms9918);
    }
    else /* address */
    {
      tms9918->currentAddress |= ((data & 0x3f) << 8);
    }
    tms9918->lastMode = 0;
  }
  else
  {
    tms9918->currentAddress = data;
    tms9918->lastMode = 1;
  }
}

/* Function:  vrEmuTms9918WriteData
 * --------------------
 * write data (mode = 0) to the tms9918
 *
 * data: the data (DB0 -> DB7) to send
 */
 void __not_in_flash_func(vrEmuTms9918WriteData)(VrEmuTms9918* tms9918, uint8_t data)
{
  tms9918->vram[(tms9918->currentAddress++) & 0x3fff] = data;
}

/* Function:  vrEmuTms9918ReadStatus
 * --------------------
 * read from the status register
 */
 uint8_t __not_in_flash_func(vrEmuTms9918ReadStatus)(VrEmuTms9918* tms9918)
{
  uint32_t irq = spin_lock_blocking(tmsStatusLock);
  uint8_t tmpStatus = tms9918->status;
  tms9918->status = 0;
  spin_unlock(tmsStatusLock, irq);
#if VIDEO_IRQ_DEBUG
  tms_dbg_status_reads++;
  if (tmpStatus & STATUS_INT) tms_dbg_int_reads++;
#endif
  return tmpStatus;
}

/* Function:  vrEmuTms9918SetInterrupt
 * --------------------
 * set the frame (VSYNC) flag - called from a 60 Hz timer on core 0
 */
 void __not_in_flash_func(vrEmuTms9918SetInterrupt)(VrEmuTms9918* tms9918)
{
  uint32_t irq = spin_lock_blocking(tmsStatusLock);
  tms9918->status |= STATUS_INT;
  spin_unlock(tmsStatusLock, irq);
}

/* Function:  vrEmuTms9918InterruptPending
 * --------------------
 * state of the INT output: frame flag set AND interrupts enabled (R1 bit 5).
 * Stays true until the CPU reads the status register.
 */
 bool __not_in_flash_func(vrEmuTms9918InterruptPending)(VrEmuTms9918* tms9918)
{
  return (tms9918->status & STATUS_INT) && (tms9918->registers[TMS_REG_1] & 0x20);
}

/* Function:  vrEmuTms9918ReadData
 * --------------------
 * read data (mode = 0) from the tms9918
 */
 uint8_t __not_in_flash_func(vrEmuTms9918ReadData)(VrEmuTms9918* tms9918)
{
  return tms9918->vram[(tms9918->currentAddress++) & 0x3fff];
}

/* =========================================================================
 * 16-bit direct scanline rendering (single-pass RGB565 / BGR16)
 * ========================================================================= */

static void __not_in_flash_func(vrEmuTms9918OutputSprites16)(VrEmuTms9918* tms9918, uint8_t y, uint16_t pixels[TMS9918_PIXELS_X], const uint16_t *pal)
{
  int spriteSizePx = (tmsSpriteSize(tms9918) ? 16 : 8) * (tmsSpriteMag(tms9918) ? 2 : 1);
  uint16_t spriteAttrTableAddr = tmsSpriteAttrTableAddr(tms9918);
  uint16_t spritePatternAddr = tmsSpritePatternTableAddr(tms9918);

  int spritesShown = 0;

  /* NOTE: status is no longer cleared at y == 0 - on the real chip the flags
   * stay set until the CPU reads the status register. */

  for (int i = 0; i < MAX_SPRITES; ++i)
  {
    int spriteAttrAddr = spriteAttrTableAddr + i * SPRITE_ATTR_BYTES;
    int vPos = tms9918->vram[spriteAttrAddr];

    if (vPos == LAST_SPRITE_VPOS)
    {
      if ((scanStatus & STATUS_5S) == 0)
      {
        scanStatus = (scanStatus & 0xE0) | i;
      }
      break;
    }

    if (vPos > (uint8_t)-32)
    {
      vPos -= 256;
    }

    vPos += 1;

    int patternRow = y - vPos;
    if (tmsSpriteMag(tms9918))
    {
      patternRow /= 2;
    }

    if (patternRow < 0 || patternRow >= (tmsSpriteSize(tms9918) ? 16 : 8))
      continue;

    vrEmuTms9918Color spriteColor = tms9918->vram[spriteAttrAddr + 3] & 0x0f;

    if (spritesShown == 0)
    {
      memset(tms9918->rowSpriteBits, 0, TMS9918_PIXELS_X);
    }

    if (++spritesShown > MAX_SCANLINE_SPRITES)
    {
      if ((scanStatus & STATUS_5S) == 0)
      {
        scanStatus = (scanStatus & 0xE0) | STATUS_5S | i;
      }
      break;
    }

    uint8_t patternName = tms9918->vram[spriteAttrAddr + 2];
    uint16_t patternOffset = spritePatternAddr + patternName * 8 + (uint16_t)patternRow;

    int hPos = tms9918->vram[spriteAttrAddr + 1];
    if (tms9918->vram[spriteAttrAddr + 3] & 0x80)
    {
      hPos -= 32;
    }

    uint8_t patternByte = tms9918->vram[patternOffset];
    uint16_t color16 = pal[spriteColor];

    int screenBit  = 0;
    int patternBit = 0;

    for (int screenX = hPos; screenX < (hPos + spriteSizePx); ++screenX, ++screenBit)
    {
      if (screenX >= TMS9918_PIXELS_X)
      {
        break;
      }

      if (screenX >= 0)
      {
        if (patternByte & (0x80 >> patternBit))
        {
          if (spriteColor != TMS_TRANSPARENT)
          {
            pixels[screenX] = color16;
          }

          if (tms9918->rowSpriteBits[screenX])
          {
            scanStatus |= STATUS_COL;
          }
          tms9918->rowSpriteBits[screenX] = 1;
        }
      }

      if (!tmsSpriteMag(tms9918) || (screenBit & 0x01))
      {
        if (++patternBit == 8)
        {
          patternBit = 0;
          patternByte = tms9918->vram[patternOffset + 16];
        }
      }
    }    
  }
}

static void __not_in_flash_func(vrEmuTms9918GraphicsIScanLine16)(VrEmuTms9918* tms9918, uint8_t y, uint16_t pixels[TMS9918_PIXELS_X], const uint16_t *pal)
{
  uint8_t textRow = y >> 3;
  int patternRow = y % 8;

  uint16_t namesAddr = tmsNameTableAddr(tms9918) + textRow * GRAPHICS_NUM_COLS;
  uint16_t patternBaseAddr = tmsPatternTableAddr(tms9918) + patternRow;
  uint16_t colorBaseAddr = tmsColorTableAddr(tms9918);

  vrEmuTms9918Color mainBg = (tms9918->registers[TMS_REG_7]) & 0x0f;
  uint16_t mainBg16 = pal[mainBg];

  uint16_t *p = pixels;

  for (int tileX = 0; tileX < GRAPHICS_NUM_COLS; ++tileX)
  {
    int pattern = tms9918->vram[namesAddr + tileX];
    uint8_t patternByte = tms9918->vram[patternBaseAddr + pattern * 8];
    uint8_t colorByte = tms9918->vram[colorBaseAddr + pattern / 8];

    uint8_t fgIdx = colorByte >> 4;
    uint8_t bgIdx = colorByte & 0x0f;
    uint16_t fg = fgIdx ? pal[fgIdx] : mainBg16;
    uint16_t bg = bgIdx ? pal[bgIdx] : mainBg16;

    p[0] = (patternByte & 0x80) ? fg : bg;
    p[1] = (patternByte & 0x40) ? fg : bg;
    p[2] = (patternByte & 0x20) ? fg : bg;
    p[3] = (patternByte & 0x10) ? fg : bg;
    p[4] = (patternByte & 0x08) ? fg : bg;
    p[5] = (patternByte & 0x04) ? fg : bg;
    p[6] = (patternByte & 0x02) ? fg : bg;
    p[7] = (patternByte & 0x01) ? fg : bg;
    p += 8;
  }

  vrEmuTms9918OutputSprites16(tms9918, y, pixels, pal);
}

static void __not_in_flash_func(vrEmuTms9918GraphicsIIScanLine16)(VrEmuTms9918* tms9918, uint8_t y, uint16_t pixels[TMS9918_PIXELS_X], const uint16_t *pal)
{
  uint8_t textRow = y >> 3;
  int patternRow = y % 8;

  uint16_t namesAddr = tmsNameTableAddr(tms9918) + textRow * GRAPHICS_NUM_COLS;

  int pageThird = (textRow & 0x18) >> 3;
  uint16_t pageOffset = pageThird << 11;

  bool invalidGfxII = (tms9918->registers[TMS_REG_4] & 0x03) != 0x03 ||
                      (tms9918->registers[TMS_REG_3] & 0x7f) != 0x7f;

  if (invalidGfxII)
  {
    pageOffset = 0;
  }

  uint16_t patternBaseAddr = tmsPatternTableAddr(tms9918) + pageOffset + patternRow;
  uint16_t colorBaseAddr = tmsColorTableAddr(tms9918) + pageOffset + patternRow;

  vrEmuTms9918Color mainBg = (tms9918->registers[TMS_REG_7]) & 0x0f;
  uint16_t mainBg16 = pal[mainBg];

  uint16_t *p = pixels;

  for (int tileX = 0; tileX < GRAPHICS_NUM_COLS; ++tileX)
  {
    int pattern = tms9918->vram[namesAddr + tileX];

    if (invalidGfxII)
    {
      pattern &= 0x07;
    }

    uint8_t patternByte = tms9918->vram[patternBaseAddr + pattern * 8];
    uint8_t colorByte = tms9918->vram[colorBaseAddr + pattern * 8];

    uint8_t fgIdx = colorByte >> 4;
    uint8_t bgIdx = colorByte & 0x0f;
    uint16_t fg = fgIdx ? pal[fgIdx] : mainBg16;
    uint16_t bg = bgIdx ? pal[bgIdx] : mainBg16;

    p[0] = (patternByte & 0x80) ? fg : bg;
    p[1] = (patternByte & 0x40) ? fg : bg;
    p[2] = (patternByte & 0x20) ? fg : bg;
    p[3] = (patternByte & 0x10) ? fg : bg;
    p[4] = (patternByte & 0x08) ? fg : bg;
    p[5] = (patternByte & 0x04) ? fg : bg;
    p[6] = (patternByte & 0x02) ? fg : bg;
    p[7] = (patternByte & 0x01) ? fg : bg;
    p += 8;
  }

  vrEmuTms9918OutputSprites16(tms9918, y, pixels, pal);
}

static void __not_in_flash_func(vrEmuTms9918TextScanLine16)(VrEmuTms9918* tms9918, uint8_t y, uint16_t pixels[TMS9918_PIXELS_X], const uint16_t *pal)
{
  uint8_t textRow = y >> 3;
  int patternRow = y % 8;

  uint16_t namesAddr = tmsNameTableAddr(tms9918) + textRow * TEXT_NUM_COLS;
  uint16_t patternBase = tmsPatternTableAddr(tms9918) + patternRow;

  vrEmuTms9918Color mainBg = (tms9918->registers[TMS_REG_7]) & 0x0f;
  vrEmuTms9918Color mainFg = (tms9918->registers[TMS_REG_7] >> 4);
  if (mainFg == 0) mainFg = mainBg;

  uint16_t bg = pal[mainBg];
  uint16_t fg = pal[mainFg];
  
  uint16_t *p = pixels;
  for (int i = 0; i < 8; i++) *p++ = bg;

  for (int tileX = 0; tileX < TEXT_NUM_COLS; ++tileX)
  {
    int pattern = tms9918->vram[namesAddr + tileX];
    uint8_t patternByte = tms9918->vram[patternBase + pattern * 8];

    p[0] = (patternByte & 0x80) ? fg : bg;
    p[1] = (patternByte & 0x40) ? fg : bg;
    p[2] = (patternByte & 0x20) ? fg : bg;
    p[3] = (patternByte & 0x10) ? fg : bg;
    p[4] = (patternByte & 0x08) ? fg : bg;
    p[5] = (patternByte & 0x04) ? fg : bg;
    p += 6;
  }

  while (p < pixels + TMS9918_PIXELS_X)
  {
    *p++ = bg;
  }
}

static void __not_in_flash_func(vrEmuTms9918MulticolorScanLine16)(VrEmuTms9918* tms9918, uint8_t y, uint16_t pixels[TMS9918_PIXELS_X], const uint16_t *pal)
{
  uint8_t textRow = y >> 3;
  int patternRow = (y / 4) % 2 + (textRow % 4) * 2;

  uint16_t namesAddr = tmsNameTableAddr(tms9918) + textRow * GRAPHICS_NUM_COLS;
  uint16_t patternBase = tmsPatternTableAddr(tms9918) + patternRow;

  uint16_t *p = pixels;

  for (int tileX = 0; tileX < GRAPHICS_NUM_COLS; ++tileX)
  {
    int pattern = tms9918->vram[namesAddr + tileX];
    uint8_t colorByte = tms9918->vram[patternBase + pattern * 8];

    uint16_t fg = pal[tmsFgColor(tms9918, colorByte)];
    uint16_t bg = pal[tmsBgColor(tms9918, colorByte)];

    for (int i = 0; i < 4; ++i) *p++ = fg;
    for (int i = 0; i < 4; ++i) *p++ = bg;
  }

  vrEmuTms9918OutputSprites16(tms9918, y, pixels, pal);
}

void __not_in_flash_func(vrEmuTms9918ScanLine16)(VrEmuTms9918* tms9918, uint8_t y, uint16_t pixels[TMS9918_PIXELS_X], const uint16_t *pal)
{
  if (tms9918 == NULL)
    return;

  /* Rendering runs on core 0 while core 1 may read (and clear) the status
   * register at any moment. Status changes made while rendering this line are
   * collected in scanStatus and merged into tms9918->status under a spinlock,
   * so a flag set here can never be lost by a concurrent status read. */
  uint8_t statusBefore = tms9918->status;
  scanStatus = statusBefore;

  if (!vrEmuTms9918DisplayEnabled(tms9918) || y >= TMS9918_PIXELS_Y)
  {
    uint16_t bg = pal[tmsMainBgColor(tms9918)];
    for (int i = 0; i < TMS9918_PIXELS_X; i++) pixels[i] = bg;
  }
  else
  {
    switch (tms9918->mode)
    {
      case TMS_MODE_GRAPHICS_I:
        vrEmuTms9918GraphicsIScanLine16(tms9918, y, pixels, pal);
        break;

      case TMS_MODE_GRAPHICS_II:
        vrEmuTms9918GraphicsIIScanLine16(tms9918, y, pixels, pal);
        break;

      case TMS_MODE_TEXT:
        vrEmuTms9918TextScanLine16(tms9918, y, pixels, pal);
        break;

      case TMS_MODE_MULTICOLOR:
        vrEmuTms9918MulticolorScanLine16(tms9918, y, pixels, pal);
        break;
    }
  }

  /* NOTE: the frame flag (STATUS_INT) is no longer set here. It comes from a
   * 60 Hz timer (video_poll), independent of how fast the LCD can be fed. */

  uint32_t irq = spin_lock_blocking(tmsStatusLock);
  uint8_t s = tms9918->status;
  uint8_t added = scanStatus & (uint8_t)~statusBefore & (STATUS_INT | STATUS_5S | STATUS_COL);
  s |= added;
  /* the sprite number field follows the renderer unless a 5th sprite is latched */
  if (!(s & STATUS_5S) || (added & STATUS_5S))
  {
    s = (s & (STATUS_INT | STATUS_5S | STATUS_COL)) | (scanStatus & 0x1f);
  }
  tms9918->status = s;
  spin_unlock(tmsStatusLock, irq);
}

/* Function:  vrEmuTms9918DisplayEnabled
  * --------------------
  * check BLANK flag
  */

bool __not_in_flash_func(vrEmuTms9918DisplayEnabled)(VrEmuTms9918* tms9918)
{
  if (tms9918 == NULL)
    return false;

  return tms9918->registers[TMS_REG_1] & 0x40;
}
