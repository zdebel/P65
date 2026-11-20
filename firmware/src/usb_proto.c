#include "usb_proto.h"
#include "storage.h"
#include <string.h>

uint16_t proto_crc16(const uint8_t *d, size_t n, uint16_t crc)
{
    while (n--) {
        crc ^= (uint16_t)(*d++) << 8;
        for (int i = 0; i < 8; i++) crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
    }
    return crc;
}

static uint32_t g32(const uint8_t *p) { return p[0] | (p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24); }
static uint16_t g16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static void p32(uint8_t *p, uint32_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24); }
static void p16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }

void proto_init(proto_t *p, fs_t *fs, fs_writer_t *put, fs_writer_t *scratch, proto_send_fn send, void *send_ctx)
{
    memset(p, 0, sizeof *p);
    p->fs = fs; p->put = put; p->scratch = scratch; p->send = send; p->send_ctx = send_ctx;
}

void proto_set_ready(proto_t *p, bool ready) { p->ready = ready; }

static void reply(proto_t *p, uint8_t cmd, uint8_t status, const uint8_t *data, size_t n)
{
    uint8_t head[6];
    uint16_t len = (uint16_t)(2 + n);
    head[0] = 0xA5; head[1] = 0x5A;
    p16(head + 2, len);
    head[4] = cmd | 0x80;
    head[5] = status;
    uint16_t crc = proto_crc16(head + 2, 4, 0xFFFF);
    if (n) crc = proto_crc16(data, n, crc);
    uint8_t tail[2];
    p16(tail, crc);
    p->send(p->send_ctx, head, 6);
    if (n) p->send(p->send_ctx, data, n);
    p->send(p->send_ctx, tail, 2);
}

static void abort_put(proto_t *p)
{
    if (p->putting) fs_writer_abort(p->put);
    p->putting = false;
}

static void handle(proto_t *p, const uint8_t *pl, uint16_t n, uint32_t now)
{
    uint8_t cmd = pl[0];
    const uint8_t *d = pl + 1;
    uint16_t dn = (uint16_t)(n - 1);
    uint8_t out[PROTO_MAX_CHUNK + 8];
    p->seen_ms = now;

    if (!p->ready && cmd != PROTO_C_HELLO) { reply(p, cmd, STO_E_NOSTORAGE, NULL, 0); return; }

    switch (cmd) {
    case PROTO_C_HELLO: {
        out[0] = PROTO_VERSION;
        p16(out + 1, PROTO_MAX_CHUNK);
        p32(out + 3, p->ready ? fs_total_bytes(p->fs) : 0);
        p32(out + 7, p->ready ? fs_free_bytes(p->fs) : 0);
        p16(out + 11, p->ready ? (uint16_t)fs_count(p->fs) : 0);
        const char *txt = "P65 storage";
        memcpy(out + 13, txt, strlen(txt) + 1);
        reply(p, cmd, p->ready ? STO_OK : STO_E_NOSTORAGE, out, 13 + strlen(txt) + 1);
        break;
    }
    case PROTO_C_LIST: {
        if (dn < 2) { reply(p, cmd, STO_E_BADCMD, NULL, 0); break; }
        uint16_t start = g16(d);
        int total = fs_count(p->fs);
        int cnt = 0;
        for (int i = start; i < total && cnt < PROTO_LIST_PER_FRAME; i++, cnt++) {
            const fs_entry_t *e = fs_entry(p->fs, i);
            uint8_t *o = out + 3 + cnt * PROTO_ENTRY_SIZE;
            memcpy(o, e->name, FS_NAME_LEN);
            o[11] = e->type;
            p16(o + 12, e->load); p16(o + 14, e->exec);
            p32(o + 16, e->size); p32(o + 20, e->crc);
        }
        out[0] = (uint8_t)cnt;
        p16(out + 1, (uint16_t)total);
        reply(p, cmd, STO_OK, out, 3 + (size_t)cnt * PROTO_ENTRY_SIZE);
        break;
    }
    case PROTO_C_READ: {
        if (dn < 17) { reply(p, cmd, STO_E_BADCMD, NULL, 0); break; }
        const fs_entry_t *e = fs_find(p->fs, (const char *)d);
        if (!e) { reply(p, cmd, STO_E_NOTFOUND, NULL, 0); break; }
        uint32_t off = g32(d + 11);
        uint16_t len = g16(d + 15);
        if (len > PROTO_MAX_CHUNK) len = PROTO_MAX_CHUNK;
        if (off > e->size) { reply(p, cmd, STO_E_ADDRESS, NULL, 0); break; }
        int got = fs_read(p->fs, e, off, out, len);
        if (got < 0) { reply(p, cmd, (uint8_t)sto_result_from_fs(got), NULL, 0); break; }
        reply(p, cmd, STO_OK, out, (size_t)got);
        break;
    }
    case PROTO_C_PUT_BEGIN: {
        if (dn < 20) { reply(p, cmd, STO_E_BADCMD, NULL, 0); break; }
        abort_put(p);
        int r = fs_writer_begin(p->put, p->fs, (const char *)d, d[11], g16(d + 12), g16(d + 14), g32(d + 16));
        if (r == FS_OK) { p->putting = true; p->put_next = 0; p->last_ms = now; }
        reply(p, cmd, (uint8_t)sto_result_from_fs(r), NULL, 0);
        break;
    }
    case PROTO_C_PUT_DATA: {
        if (!p->putting) { reply(p, cmd, PROTO_E_NOSESSION, NULL, 0); break; }
        if (dn < 4) { reply(p, cmd, STO_E_BADCMD, NULL, 0); break; }
        if (g32(d) != p->put_next) { reply(p, cmd, PROTO_E_SEQ, NULL, 0); break; }
        int r = fs_writer_write(p->put, d + 4, (uint32_t)(dn - 4));
        if (r != FS_OK) { p->putting = false; reply(p, cmd, (uint8_t)sto_result_from_fs(r), NULL, 0); break; }
        p->put_next += (uint32_t)(dn - 4);
        p->last_ms = now;
        reply(p, cmd, STO_OK, NULL, 0);
        break;
    }
    case PROTO_C_PUT_END: {
        if (!p->putting) { reply(p, cmd, PROTO_E_NOSESSION, NULL, 0); break; }
        if (dn < 4) { reply(p, cmd, STO_E_BADCMD, NULL, 0); break; }
        if (p->put->written != p->put->total || g32(d) != p->put->crc) {
            abort_put(p);
            reply(p, cmd, PROTO_E_CRC, NULL, 0);
            break;
        }
        int r = fs_writer_finish(p->put);
        p->putting = false;
        reply(p, cmd, (uint8_t)sto_result_from_fs(r), NULL, 0);
        break;
    }
    case PROTO_C_DELETE:
        if (dn < FS_NAME_LEN) { reply(p, cmd, STO_E_BADCMD, NULL, 0); break; }
        reply(p, cmd, (uint8_t)sto_result_from_fs(fs_delete(p->fs, (const char *)d)), NULL, 0);
        break;
    case PROTO_C_RENAME:
        if (dn < 2 * FS_NAME_LEN) { reply(p, cmd, STO_E_BADCMD, NULL, 0); break; }
        if (p->putting && p->scratch == p->put) { reply(p, cmd, PROTO_E_BUSY, NULL, 0); break; }
        reply(p, cmd, (uint8_t)sto_result_from_fs(fs_rename(p->fs, (const char *)d, (const char *)d + FS_NAME_LEN, p->scratch)), NULL, 0);
        break;
    case PROTO_C_FORMAT:
        if (dn < 4 || g32(d) != PROTO_FORMAT_MAGIC) { reply(p, cmd, STO_E_BADCMD, NULL, 0); break; }
        abort_put(p);
        reply(p, cmd, (uint8_t)sto_result_from_fs(fs_format(p->fs)), NULL, 0);
        break;
    default:
        reply(p, cmd, STO_E_BADCMD, NULL, 0);
    }
}

void proto_rx(proto_t *p, const uint8_t *data, size_t len, uint32_t now_ms)
{
    for (size_t i = 0; i < len; i++) {
        uint8_t b = data[i];
        switch (p->state) {
        case 0: if (b == 0xA5) p->state = 1; break;
        case 1: p->state = (b == 0x5A) ? 2 : (b == 0xA5 ? 1 : 0); break;
        case 2: p->len = b; p->state = 3; break;
        case 3:
            p->len |= (uint16_t)(b << 8);
            if (p->len < 1 || p->len > PROTO_MAX_PAYLOAD) { p->state = 0; break; }
            p->crc = proto_crc16((const uint8_t[]){(uint8_t)p->len, (uint8_t)(p->len >> 8)}, 2, 0xFFFF);
            p->pos = 0;
            p->state = 4;
            break;
        case 4:
            p->buf[p->pos++] = b;
            if (p->pos == p->len) {
                p->crc = proto_crc16(p->buf, p->len, p->crc);
                p->state = 5;
            }
            break;
        case 5: p->rxcrc[0] = b; p->state = 6; break;
        case 6:
            p->rxcrc[1] = b;
            p->state = 0;
            if (g16(p->rxcrc) == p->crc) handle(p, p->buf, p->len, now_ms);
            break;
        }
    }
}

void proto_tick(proto_t *p, uint32_t now_ms)
{
    if (p->putting && now_ms - p->last_ms > PROTO_PUT_TIMEOUT_MS) abort_put(p);
}

bool proto_busy(const proto_t *p, uint32_t now_ms)
{
    return p->putting || (p->seen_ms != 0 && now_ms - p->seen_ms < 2000);
}
