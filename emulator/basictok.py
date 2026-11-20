"""P65 BASIC tokenizer / detokenizer, a faithful copy of what the ROM does when
you type a line (PARSE_INPUT_LINE in asm_examples/fpp65.a65):

  * spaces are ignored everywhere except inside quotes, DATA and REM
  * '?' is PRINT
  * the first token of the name table that matches wins (case insensitive,
    spaces inside the keyword are ignored), except that "AT" followed by N or O
    is looked up "the hard way" (ATN, A TO)
  * quoted text, DATA and REM are copied verbatim

A program is stored as linked lines:  next-line address (2), line number (2),
tokens ... 0  and ends with two more zero bytes. Addresses are absolute, so a
program is tied to its start address (0x0401); LOAD relinks anyway.
"""

from basictok_data import TOKENS

TXTTAB = 0x0401
TOKEN_REM = 0x80 + TOKENS.index('REM')
TOKEN_DATA = 0x80 + TOKENS.index('DATA')
TOKEN_AT = 0x80 + TOKENS.index('AT')
TOKEN_PRINT = 0x80 + TOKENS.index('PRINT')


def tokenize_body(text):
    """Tokenize everything after the line number. Returns bytes (without the final 0)."""
    s = text
    n = len(s)
    out = bytearray()
    i = 0
    in_data = False
    while True:
        # next character
        while i < n and s[i] == ' ' and not in_data:
            i += 1
        if i >= n:
            break
        c = s[i]
        if c == '"':                                    # quotation: copy up to and including the closing quote
            j = s.find('"', i + 1)
            end = n if j < 0 else j + 1
            out += s[i:end].encode('latin-1')
            i = end
            continue
        if in_data:
            out.append(ord(c))
            i += 1
            if c == ':':
                in_data = False
            continue
        if c == '?':
            out.append(TOKEN_PRINT)
            i += 1
            continue
        if '0' <= c <= ';':                             # digits, ':' and ';' are never tokens
            out.append(ord(c))
            i += 1
            if c == ':':
                in_data = False
            continue
        # look for a keyword at i
        found = None
        for idx, name in enumerate(TOKENS):
            j = i
            ok = True
            for ch in name:
                while j < n and s[j] == ' ':
                    j += 1
                if j >= n or s[j].upper() != ch:
                    ok = False
                    break
                j += 1
            if not ok:
                continue
            tok = 0x80 + idx
            if tok == TOKEN_AT:
                k = j
                while k < n and s[k] == ' ':            # the ROM looks at the very next character
                    k += 1
                nxt = s[j] if j < n else ''
                if nxt in ('N', 'O'):
                    continue
            found = (tok, j)
            break
        if found:
            tok, j = found
            out.append(tok)
            i = j
            if tok == TOKEN_DATA:
                in_data = True
            elif tok == TOKEN_REM:                      # the rest of the line is a remark
                out += s[i:].encode('latin-1')
                i = n
        else:
            out.append(ord(c))
            i += 1
    return bytes(out)


def tokenize_program(text, base=TXTTAB):
    """BASIC source text -> the bytes of a program as it sits in memory at `base`."""
    lines = {}
    for raw in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        raw = raw.rstrip()
        if not raw.strip():
            continue
        p = 0
        while p < len(raw) and raw[p] == ' ':
            p += 1
        q = p
        while q < len(raw) and raw[q].isdigit():
            q += 1
        if q == p or q - p > 5 or int(raw[p:q]) > 63999:
            raise ValueError('line without a valid line number: %r' % raw)
        lines[int(raw[p:q])] = tokenize_body(raw[q:])   # a later line with the same number replaces the earlier
    prog = bytearray()
    addr = base
    for num in sorted(lines):
        body = lines[num]
        nxt = addr + 4 + len(body) + 1
        prog += bytes([nxt & 255, nxt >> 8, num & 255, num >> 8]) + body + b'\0'
        addr = nxt
    prog += b'\0\0'                                   # end of program: a link whose high byte is 0
    return bytes(prog)


def detokenize_program(data, base=TXTTAB):
    """Program bytes -> list text. Round-trips through tokenize_program."""
    lines = []
    pos = 0
    while pos + 4 <= len(data):
        link = data[pos] | data[pos + 1] << 8
        if (link >> 8) == 0:                            # high byte of the link 0: end of program
            break
        num = data[pos + 2] | data[pos + 3] << 8
        end = data.index(0, pos + 4)
        lines.append('%d %s' % (num, detokenize_body(data[pos + 4:end])))
        pos = end + 1
    return '\n'.join(lines) + ('\n' if lines else '')


def detokenize_body(body):
    out = ''
    i = 0
    while i < len(body):
        b = body[i]
        if b == 0x22:                                   # quoted text
            j = body.find(b'"', i + 1)
            j = len(body) if j < 0 else j + 1
            out += body[i:j].decode('latin-1')
            i = j
        elif b >= 0x80:
            name = TOKENS[b - 0x80] if b - 0x80 < len(TOKENS) else '?%02X' % b
            if out and (out[-1].isalnum() or out[-1] == '$') and name[0].isalnum():
                out += ' '
            out += name
            i += 1
            if b == TOKEN_REM or b == TOKEN_DATA:       # verbatim to the end of the line / statement
                if b == TOKEN_DATA:
                    j = i
                    while j < len(body) and body[j] != 0x3A:
                        j += 1
                else:
                    j = len(body)
                out += body[i:j].decode('latin-1')
                i = j
                continue
            if i < len(body) and name[-1].isalnum() and (chr(body[i]).isalnum() or body[i] == 0x22):
                out += ' '
        else:
            out += chr(b)
            i += 1
    return out
