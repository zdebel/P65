/* ILI9341 LCD driver (SPI, write only). The pixel stream itself is driven by
   DMA from video.c; this file only does the setup and the commands. */

#include <stdint.h>
#include "pico/stdlib.h"
#include "ili9341.h"

#define LCD_WIDTH   320
#define LCD_HEIGHT  240

#define DC_CMD      0
#define DC_DATA     1

static inline void cs_select(void)   { gpio_put(LCD_PIN_CS, 0); }   /* active low */
static inline void cs_deselect(void) { gpio_put(LCD_PIN_CS, 1); }

void ili9341_command(uint8_t cmd)
{
    cs_select();
    gpio_put(LCD_PIN_DC, DC_CMD);
    spi_write_blocking(LCD_SPI, &cmd, 1);
    cs_deselect();
}

static void write_data_byte(uint8_t data)
{
    cs_select();
    gpio_put(LCD_PIN_DC, DC_DATA);
    spi_write_blocking(LCD_SPI, &data, 1);
    cs_deselect();
}

static void write_u16_pair(uint16_t a, uint16_t b)
{
    const uint8_t bytes[4] = { a >> 8, a & 0xFF, b >> 8, b & 0xFF };
    cs_select();
    gpio_put(LCD_PIN_DC, DC_DATA);
    spi_write_blocking(LCD_SPI, bytes, sizeof bytes);
    cs_deselect();
}

void ili9341_set_window(uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1)
{
    ili9341_command(ILI9341_CASET);
    write_u16_pair(x0, x1);
    ili9341_command(ILI9341_PASET);
    write_u16_pair(y0, y1);
    ili9341_command(ILI9341_RAMWR);
}

/* Init sequence: { command, number of parameters, parameters... } */
static const uint8_t init_sequence[] = {
    0xCF, 3, 0x00, 0xC1, 0x30,
    0xED, 4, 0x64, 0x03, 0x12, 0x81,
    0xE8, 3, 0x85, 0x00, 0x78,
    0xCB, 5, 0x39, 0x2C, 0x00, 0x34, 0x02,
    0xF7, 1, 0x20,
    0xEA, 2, 0x00, 0x00,
    0xC0, 1, 0x13,                  /* power control 1 */
    0xC1, 1, 0x13,                  /* power control 2 */
    0xC5, 2, 0x1C, 0x35,            /* VCOM control 1 */
    0xC7, 1, 0xC8,                  /* VCOM control 2 */
    0x21, 0,                        /* display inversion on */
    0x36, 1, 0xA8,                  /* memory access control: MY | MV | BGR */
    0xB6, 2, 0x0A, 0xA2,            /* display function control */
    0x3A, 1, 0x55,                  /* pixel format: 16 bit */
    0xF6, 2, 0x01, 0x30,            /* interface control */
    0xB1, 2, 0x00, 0x1B,            /* frame rate control */
    0xF2, 1, 0x00,                  /* 3-gamma function off */
    0x26, 1, 0x01,                  /* gamma curve 1 */
    0xE0, 15, 0x0F, 0x35, 0x31, 0x0B, 0x0E, 0x06, 0x49, 0xA7,
              0x33, 0x07, 0x0F, 0x03, 0x0C, 0x0A, 0x00,         /* positive gamma */
    0xE1, 15, 0x00, 0x0A, 0x0F, 0x04, 0x11, 0x08, 0x36, 0x58,
              0x4D, 0x07, 0x10, 0x0C, 0x32, 0x34, 0x0F,         /* negative gamma */
};

static void run_init_sequence(void)
{
    const uint8_t *p = init_sequence;
    const uint8_t *end = init_sequence + sizeof init_sequence;
    while (p < end)
    {
        ili9341_command(*p++);
        for (uint8_t n = *p++; n; n--)
        {
            write_data_byte(*p++);
        }
    }
    ili9341_command(0x11);              /* sleep out */
    sleep_ms(120);
    ili9341_command(0x29);              /* display on */
}

static void fill_screen_black(void)
{
    static uint16_t line[LCD_WIDTH];    /* zero = black */
    ili9341_set_window(0, 0, LCD_WIDTH - 1, LCD_HEIGHT - 1);
    cs_select();
    gpio_put(LCD_PIN_DC, DC_DATA);
    for (int y = 0; y < LCD_HEIGHT; y++)
    {
        spi_write_blocking(LCD_SPI, (const uint8_t *)line, sizeof line);
    }
    cs_deselect();
}

void ili9341_init(void)
{
    spi_init(LCD_SPI, LCD_SPI_HZ);
    gpio_set_function(LCD_PIN_SCK, GPIO_FUNC_SPI);
    gpio_set_function(LCD_PIN_MOSI, GPIO_FUNC_SPI);

    gpio_init(LCD_PIN_CS);
    gpio_set_dir(LCD_PIN_CS, GPIO_OUT);
    gpio_put(LCD_PIN_CS, 0);
    gpio_init(LCD_PIN_DC);
    gpio_set_dir(LCD_PIN_DC, GPIO_OUT);
    gpio_put(LCD_PIN_DC, DC_CMD);

    run_init_sequence();
    fill_screen_black();
}
