/* Firmware glue for the flash storage: flash driver, mailbox registers seen by
   the CPU (core 1) and executed on core 0, USB protocol. */
#ifndef P65_STORAGE_FW_H
#define P65_STORAGE_FW_H

#include <stdint.h>
#include <stdbool.h>

/* Size of the storage region at the END of the flash. The firmware image must
   end below PICO_FLASH_SIZE_BYTES - STORAGE_SIZE_BYTES (checked at start-up). */
#ifndef STORAGE_SIZE_BYTES
#define STORAGE_SIZE_BYTES (1024u * 1024u)
#endif

void storage_init(void);                /* mount, set up the registers; call before the CPU starts */
void storage_poll(void);                /* core 0: run pending mailbox commands, serve the USB protocol */
void storage_reg_write(uint8_t off, uint8_t val);   /* core 1: the CPU wrote mailbox register `off` */
bool storage_usb_busy(void);            /* a USB transfer is going on: keep debug text off the port */

#endif
