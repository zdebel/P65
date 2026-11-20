#ifndef _ILI9341_H
#define _ILI9341_H

#include <stdint.h>
#include "hardware/spi.h"

/* LCD wiring (SPI0, write only - the panel's MISO is not connected) */
#define LCD_SPI         spi0
#define LCD_PIN_CS      17
#define LCD_PIN_SCK     18
#define LCD_PIN_MOSI    19
#define LCD_PIN_DC      28
#define LCD_SPI_HZ      50000000    /* needs clk_peri = 200 MHz, see main.c */

#define ILI9341_CASET   0x2A        /* column address set */
#define ILI9341_PASET   0x2B        /* page (row) address set */
#define ILI9341_RAMWR   0x2C        /* memory write: pixel data follows */

/* Set up SPI, run the panel's init sequence and clear the whole 320x240 screen. */
void ili9341_init(void);

/* Send one command byte (DC low). Leaves DC high and CS high. */
void ili9341_command(uint8_t cmd);

/* Open the write window x0..x1, y0..y1 and send RAMWR. Pixel data may follow. */
void ili9341_set_window(uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1);

#endif
