/*
 * P65 USB storage protocol: how a PC (the emulator's storage manager) lists,
 * reads, writes and deletes the files in the device's flash storage over the
 * USB serial port. Pure C and independent of the transport: bytes in via
 * proto_rx(), reply bytes out through the send callback.
 *
 * Frame (both directions):
 *     A5 5A | len (u16 LE) | payload (len bytes) | crc16 (u16 LE)
 * crc16 = CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) over len and payload.
 * Request payload  = command, data...
 * Reply payload    = command | 0x80, status, data...      (status 0 = OK)
 * Bytes outside frames are ignored (so debug text can share the port).
 * All integers little endian. Names are the 11 byte 8.3 form ("GAME    BIN").
 *
 *   01 HELLO       -                                -> ver u8, max_chunk u16, total u32, free u32, files u16, text
 *   02 LIST        start u16                        -> n u8, total u16, n * entry(24)
 *                  entry = name[11] type u8 load u16 exec u16 size u32 crc u32
 *   03 READ        name[11] offset u32 len u16      -> data
 *   04 PUT_BEGIN   name[11] type u8 load u16 exec u16 size u32
 *   05 PUT_DATA    offset u32, data (offsets must be sequential)
 *   06 PUT_END     crc32 u32 of the whole file      (commits when it matches)
 *   07 DELETE      name[11]
 *   08 RENAME      old[11] new[11]
 *   09 FORMAT      0x464F524D ("FORM")
 */
#ifndef P65_USB_PROTO_H
#define P65_USB_PROTO_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include "fs.h"

#define PROTO_VERSION       1
#define PROTO_MAX_CHUNK     1024
#define PROTO_MAX_PAYLOAD   (PROTO_MAX_CHUNK + 16)
#define PROTO_LIST_PER_FRAME 8
#define PROTO_ENTRY_SIZE    24
#define PROTO_PUT_TIMEOUT_MS 10000

#define PROTO_C_HELLO       0x01
#define PROTO_C_LIST        0x02
#define PROTO_C_READ        0x03
#define PROTO_C_PUT_BEGIN   0x04
#define PROTO_C_PUT_DATA    0x05
#define PROTO_C_PUT_END     0x06
#define PROTO_C_DELETE      0x07
#define PROTO_C_RENAME      0x08
#define PROTO_C_FORMAT      0x09
#define PROTO_FORMAT_MAGIC  0x464F524Du

/* status codes: the STO_E_* values of include/storage.h, plus */
#define PROTO_E_SEQ         20      /* PUT_DATA offset is not the next one */
#define PROTO_E_CRC         21      /* PUT_END crc does not match what was received */
#define PROTO_E_NOSESSION   22      /* PUT_DATA / PUT_END without PUT_BEGIN */
#define PROTO_E_BUSY        23      /* a PUT is in progress */

typedef void (*proto_send_fn)(void *ctx, const uint8_t *data, size_t len);

typedef struct {
    fs_t *fs;
    fs_writer_t *put;               /* writer used by PUT sessions */
    fs_writer_t *scratch;           /* writer used by RENAME (may be shared with the CPU's mailbox) */
    proto_send_fn send;
    void *send_ctx;
    bool ready;                     /* storage mounted */
    /* receiver state */
    uint8_t  state;
    uint16_t len, pos;
    uint16_t crc;
    uint8_t  rxcrc[2];
    uint8_t  buf[PROTO_MAX_PAYLOAD];
    /* PUT session */
    bool     putting;
    uint32_t put_next;              /* next expected offset */
    uint32_t last_ms;
    uint32_t seen_ms;               /* last time any frame arrived */
} proto_t;

void proto_init(proto_t *p, fs_t *fs, fs_writer_t *put, fs_writer_t *scratch, proto_send_fn send, void *send_ctx);
void proto_set_ready(proto_t *p, bool ready);
void proto_rx(proto_t *p, const uint8_t *data, size_t len, uint32_t now_ms);
void proto_tick(proto_t *p, uint32_t now_ms);           /* aborts a stalled PUT */
bool proto_busy(const proto_t *p, uint32_t now_ms);     /* a session or recent traffic: keep quiet on the port */

uint16_t proto_crc16(const uint8_t *data, size_t len, uint16_t crc);

#endif
