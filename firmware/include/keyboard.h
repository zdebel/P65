#ifndef _KEYBOARD_H
#define _KEYBOARD_H

#include <stdint.h>

#define P2_LED2     7
#define P2_LED1     6
#define P2_LED0     5
#define P2_LCD_RST  3
#define P2_CPU_NMI  2
#define P2_CPU_IRQ  1
#define P2_CPU_RST  0

/* idle state of P2 bits 0-3: reset released, IRQ/NMI released (all active low),
   LCD out of reset */
#define P2_IDLE     ((1 << P2_LCD_RST) | (1 << P2_CPU_NMI) | (1 << P2_CPU_IRQ) | (1 << P2_CPU_RST))

void keyboard_init(void);
void keyboard_scan(void);
void keyboard_set_p2(uint8_t val);
void keyboard_set_irq(int active);   /* drive the CPU IRQ line (core 0 only) */
int keyboard_p2_written(void);
void keyboard_clear_key(void);
void keyboard_set_leds(uint8_t leds);
uint8_t keyboard_get_leds(void);
#endif