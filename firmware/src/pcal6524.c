/* NXP PCAL6524 24-bit I2C GPIO expander: keyboard matrix, LEDs and the CPU
   reset / IRQ / NMI lines. The *_noblock functions only queue bytes in the I2C
   FIFO so that the keyboard state machine never waits on the bus. */

#include "pcal6524.h"
#include "hardware/gpio.h"

static uint8_t pcal6524_addr = 0x22;
static i2c_inst_t *pcal6524_i2c = i2c0;

/* number of aborted transfers (NACK, arbitration...) seen - for diagnostics */
volatile uint32_t pcal6524_aborts = 0;

/* After an aborted transfer the controller discards everything written to its
   TX FIFO until the abort flag is cleared (the SDK's blocking calls do that,
   the non-blocking ones below did not). */
static inline void pcal6524_clear_abort(void)
{
    if (pcal6524_i2c->hw->raw_intr_stat & I2C_IC_RAW_INTR_STAT_TX_ABRT_BITS)
    {
        pcal6524_aborts++;
        (void)pcal6524_i2c->hw->clr_tx_abrt;
    }
}

void pcal6524_init(i2c_inst_t *i2c, uint8_t addr, uint8_t sda, uint8_t scl)
{
    pcal6524_addr = addr;
    pcal6524_i2c = i2c;
    i2c_init(i2c, 1000 * 1000);
    gpio_set_function(sda, GPIO_FUNC_I2C);
    gpio_set_function(scl, GPIO_FUNC_I2C);
    gpio_pull_up(sda);
    gpio_pull_up(scl);
}

int pcal6524_write_register(uint8_t reg, uint8_t value)
{
    uint8_t txdata[] = {reg, value};
    return i2c_write_blocking(pcal6524_i2c, pcal6524_addr, txdata, sizeof(txdata), false);
}

int pcal6524_write_register_noblock(uint8_t reg, uint8_t value)
{
    pcal6524_clear_abort();
    pcal6524_i2c->hw->enable = 0;
    pcal6524_i2c->hw->tar = pcal6524_addr;
    pcal6524_i2c->hw->enable = 1;
    if (i2c_get_write_available(pcal6524_i2c) > 1)
    {
        pcal6524_i2c->hw->data_cmd = reg;
        pcal6524_i2c->hw->data_cmd = (1 << I2C_IC_DATA_CMD_STOP_LSB) | value;
        return 2;
    }
    return 0;
}

int pcal6524_write_register_noblock_done()
{
    /* An empty TX FIFO only means the last byte has moved into the shifter: it
       (and the STOP condition) can still be on the wire for ~10-20 us. The next
       operation starts by toggling IC_ENABLE, which would corrupt it - so also
       wait until the controller is idle. (This did not matter while the scan
       was polled once per LCD line; it does now that it is polled in a tight
       loop.) */
    return (i2c_get_write_available(pcal6524_i2c) == 16) &&
           !(pcal6524_i2c->hw->status & I2C_IC_STATUS_ACTIVITY_BITS);
}

int pcal6524_schedule_read_register_noblock(uint8_t reg)
{
    pcal6524_clear_abort();
    pcal6524_i2c->hw->enable = 0;
    pcal6524_i2c->hw->tar = pcal6524_addr;
    pcal6524_i2c->hw->enable = 1;
    if (i2c_get_write_available(pcal6524_i2c) > 1)
    {
        pcal6524_i2c->hw->data_cmd = reg;
        pcal6524_i2c->hw->data_cmd = (1 << I2C_IC_DATA_CMD_STOP_LSB) | (I2C_IC_DATA_CMD_CMD_BITS);
        return 2;
    }
    return 0;
}

int pcal6524_read_register_noblock(uint8_t *dst)
{
    if (i2c_get_read_available(pcal6524_i2c)) {
        *dst = pcal6524_i2c->hw->data_cmd & 0xff;
        return 1;
    }
    return 0;
}
