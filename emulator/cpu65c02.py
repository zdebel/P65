"""WDC 65C02 CPU core (incl. Rockwell RMB/SMB/BBR/BBS, WAI, STP).

Speed matters in pure Python, so the whole fetch/decode/execute loop is
generated once as a single function: registers live in local variables and the
opcode is dispatched through a balanced if-tree (8 compares). Memory is a flat
bytearray; only accesses that can hit the I/O page go through callbacks:

  reads  of $8002/$8003            -> rd_io(addr)
  writes to $8000-$80FF            -> wr_io(addr, value)
  writes to $8100-$FFFF (ROM)      -> ignored

which is exactly how the Pico firmware (core 1) serves the real 65C02 bus.
Cycle counts are the base WDC counts (no page-crossing penalties).
The I/O addresses come from memmap.py.
"""

from memmap import RAM_END, IO_END, IO_VDATA

# ---------------------------------------------------------------- opcode table
# mode: imp acc imm zp zpx zpy abs abx aby ind iax izx izy izp rel zpr
OPS = {}


def _op(code, mn, mode, cyc):
    OPS[code] = (mn, mode, cyc)


# ALU group (cc = 01)
_ALU = ['ORA', 'AND', 'EOR', 'ADC', 'STA', 'LDA', 'CMP', 'SBC']
_ALU_MODES = [('izx', 6), ('zp', 3), ('imm', 2), ('abs', 4),
              ('izy', 5), ('zpx', 4), ('aby', 4), ('abx', 4)]
for _a, _mn in enumerate(_ALU):
    for _b, (_mode, _cyc) in enumerate(_ALU_MODES):
        _code = (_a << 5) | (_b << 2) | 1
        if _mn == 'STA' and _mode == 'imm':
            continue                    # $89 = BIT #imm on the 65C02
        if _mn == 'STA' and _mode in ('izy', 'aby', 'abx'):
            _cyc += 1
        _op(_code, _mn, _mode, _cyc)
    _op((_a << 5) | 0x12, _mn, 'izp', 5)

for _code, _mn, _mode, _cyc in [
    (0x00, 'BRK', 'imp', 7), (0x08, 'PHP', 'imp', 3), (0x18, 'CLC', 'imp', 2),
    (0x20, 'JSR', 'abs', 6), (0x28, 'PLP', 'imp', 4), (0x38, 'SEC', 'imp', 2),
    (0x40, 'RTI', 'imp', 6), (0x48, 'PHA', 'imp', 3), (0x58, 'CLI', 'imp', 2),
    (0x60, 'RTS', 'imp', 6), (0x68, 'PLA', 'imp', 4), (0x78, 'SEI', 'imp', 2),
    (0x88, 'DEY', 'imp', 2), (0x8A, 'TXA', 'imp', 2), (0x98, 'TYA', 'imp', 2),
    (0x9A, 'TXS', 'imp', 2), (0xA8, 'TAY', 'imp', 2), (0xAA, 'TAX', 'imp', 2),
    (0xB8, 'CLV', 'imp', 2), (0xBA, 'TSX', 'imp', 2), (0xC8, 'INY', 'imp', 2),
    (0xCA, 'DEX', 'imp', 2), (0xD8, 'CLD', 'imp', 2), (0xE8, 'INX', 'imp', 2),
    (0xEA, 'NOP', 'imp', 2), (0xF8, 'SED', 'imp', 2),
    (0x5A, 'PHY', 'imp', 3), (0x7A, 'PLY', 'imp', 4), (0xDA, 'PHX', 'imp', 3),
    (0xFA, 'PLX', 'imp', 4), (0x1A, 'INC', 'acc', 2), (0x3A, 'DEC', 'acc', 2),
    (0xCB, 'WAI', 'imp', 3), (0xDB, 'STP', 'imp', 3),
    # branches
    (0x10, 'BPL', 'rel', 2), (0x30, 'BMI', 'rel', 2), (0x50, 'BVC', 'rel', 2),
    (0x70, 'BVS', 'rel', 2), (0x90, 'BCC', 'rel', 2), (0xB0, 'BCS', 'rel', 2),
    (0xD0, 'BNE', 'rel', 2), (0xF0, 'BEQ', 'rel', 2), (0x80, 'BRA', 'rel', 3),
    # jumps
    (0x4C, 'JMP', 'abs', 3), (0x6C, 'JMP', 'ind', 6), (0x7C, 'JMP', 'iax', 6),
    # shifts / inc / dec
    (0x06, 'ASL', 'zp', 5), (0x0A, 'ASL', 'acc', 2), (0x0E, 'ASL', 'abs', 6),
    (0x16, 'ASL', 'zpx', 6), (0x1E, 'ASL', 'abx', 6),
    (0x26, 'ROL', 'zp', 5), (0x2A, 'ROL', 'acc', 2), (0x2E, 'ROL', 'abs', 6),
    (0x36, 'ROL', 'zpx', 6), (0x3E, 'ROL', 'abx', 6),
    (0x46, 'LSR', 'zp', 5), (0x4A, 'LSR', 'acc', 2), (0x4E, 'LSR', 'abs', 6),
    (0x56, 'LSR', 'zpx', 6), (0x5E, 'LSR', 'abx', 6),
    (0x66, 'ROR', 'zp', 5), (0x6A, 'ROR', 'acc', 2), (0x6E, 'ROR', 'abs', 6),
    (0x76, 'ROR', 'zpx', 6), (0x7E, 'ROR', 'abx', 6),
    (0xC6, 'DEC', 'zp', 5), (0xCE, 'DEC', 'abs', 6), (0xD6, 'DEC', 'zpx', 6),
    (0xDE, 'DEC', 'abx', 7),
    (0xE6, 'INC', 'zp', 5), (0xEE, 'INC', 'abs', 6), (0xF6, 'INC', 'zpx', 6),
    (0xFE, 'INC', 'abx', 7),
    # loads / stores / compares (X, Y)
    (0xA0, 'LDY', 'imm', 2), (0xA4, 'LDY', 'zp', 3), (0xAC, 'LDY', 'abs', 4),
    (0xB4, 'LDY', 'zpx', 4), (0xBC, 'LDY', 'abx', 4),
    (0xA2, 'LDX', 'imm', 2), (0xA6, 'LDX', 'zp', 3), (0xAE, 'LDX', 'abs', 4),
    (0xB6, 'LDX', 'zpy', 4), (0xBE, 'LDX', 'aby', 4),
    (0x84, 'STY', 'zp', 3), (0x8C, 'STY', 'abs', 4), (0x94, 'STY', 'zpx', 4),
    (0x86, 'STX', 'zp', 3), (0x8E, 'STX', 'abs', 4), (0x96, 'STX', 'zpy', 4),
    (0x64, 'STZ', 'zp', 3), (0x74, 'STZ', 'zpx', 4), (0x9C, 'STZ', 'abs', 4),
    (0x9E, 'STZ', 'abx', 5),
    (0xC0, 'CPY', 'imm', 2), (0xC4, 'CPY', 'zp', 3), (0xCC, 'CPY', 'abs', 4),
    (0xE0, 'CPX', 'imm', 2), (0xE4, 'CPX', 'zp', 3), (0xEC, 'CPX', 'abs', 4),
    # BIT / TSB / TRB
    (0x24, 'BIT', 'zp', 3), (0x2C, 'BIT', 'abs', 4), (0x34, 'BIT', 'zpx', 4),
    (0x3C, 'BIT', 'abx', 4), (0x89, 'BIT', 'imm', 2),
    (0x04, 'TSB', 'zp', 5), (0x0C, 'TSB', 'abs', 6),
    (0x14, 'TRB', 'zp', 5), (0x1C, 'TRB', 'abs', 6),
    # undocumented-but-defined 65C02 NOPs with operands
    (0x02, 'NOP', 'imm', 2), (0x22, 'NOP', 'imm', 2), (0x42, 'NOP', 'imm', 2),
    (0x62, 'NOP', 'imm', 2), (0x82, 'NOP', 'imm', 2), (0xC2, 'NOP', 'imm', 2),
    (0xE2, 'NOP', 'imm', 2), (0x44, 'NOP', 'zp', 3), (0x54, 'NOP', 'zpx', 4),
    (0xD4, 'NOP', 'zpx', 4), (0xF4, 'NOP', 'zpx', 4), (0x5C, 'NOP', 'abs', 8),
    (0xDC, 'NOP', 'abs', 4), (0xFC, 'NOP', 'abs', 4),
]:
    _op(_code, _mn, _mode, _cyc)

for _k in range(8):
    _op(0x07 | (_k << 4), 'RMB%d' % _k, 'zp', 5)
    _op(0x87 | (_k << 4), 'SMB%d' % _k, 'zp', 5)
    _op(0x0F | (_k << 4), 'BBR%d' % _k, 'zpr', 5)
    _op(0x8F | (_k << 4), 'BBS%d' % _k, 'zpr', 5)

for _code in range(256):                      # remaining: 1-byte, 1-cycle NOPs
    if _code not in OPS:
        _op(_code, 'NOP', 'imp', 1)

MODE_LEN = {'imp': 1, 'acc': 1, 'imm': 2, 'zp': 2, 'zpx': 2, 'zpy': 2,
            'izx': 2, 'izy': 2, 'izp': 2, 'rel': 2, 'abs': 3, 'abx': 3,
            'aby': 3, 'ind': 3, 'iax': 3, 'zpr': 3}

# ---------------------------------------------------------------- disassembler


def disassemble(peek, pc):
    """Return (text, length) of the instruction at pc. peek(addr) -> byte."""
    op = peek(pc)
    mn, mode, _ = OPS[op]
    n = MODE_LEN[mode]
    b1 = peek((pc + 1) & 0xFFFF)
    b2 = peek((pc + 2) & 0xFFFF)
    w = b1 | (b2 << 8)
    if mode == 'imp':
        arg = ''
    elif mode == 'acc':
        arg = 'A'
    elif mode == 'imm':
        arg = '#$%02X' % b1
    elif mode == 'zp':
        arg = '$%02X' % b1
    elif mode == 'zpx':
        arg = '$%02X,X' % b1
    elif mode == 'zpy':
        arg = '$%02X,Y' % b1
    elif mode == 'izx':
        arg = '($%02X,X)' % b1
    elif mode == 'izy':
        arg = '($%02X),Y' % b1
    elif mode == 'izp':
        arg = '($%02X)' % b1
    elif mode == 'abs':
        arg = '$%04X' % w
    elif mode == 'abx':
        arg = '$%04X,X' % w
    elif mode == 'aby':
        arg = '$%04X,Y' % w
    elif mode == 'ind':
        arg = '($%04X)' % w
    elif mode == 'iax':
        arg = '($%04X,X)' % w
    elif mode == 'rel':
        arg = '$%04X' % ((pc + 2 + (b1 - 256 if b1 & 0x80 else b1)) & 0xFFFF)
    else:  # zpr
        arg = '$%02X,$%04X' % (b1, (pc + 3 + (b2 - 256 if b2 & 0x80 else b2)) & 0xFFFF)
    raw = ' '.join('%02X' % peek((pc + k) & 0xFFFF) for k in range(n))
    return '%-8s  %s %s' % (raw, mn, arg), n


# ---------------------------------------------------------------- code generator
PC1 = 'pc = (pc + 1) & 0xFFFF'
PC2 = 'pc = (pc + 2) & 0xFFFF'
ZP_MODES = ('zp', 'zpx', 'zpy')

PACK_P = ('((fn & 0x80) | (0x40 if fv else 0) | 0x30 | (d << 3) | (i << 2)'
          ' | (0 if fz else 2) | c)')
UNPACK_P = ['fn = p & 0x80', 'fv = p & 0x40', 'd = (p >> 3) & 1',
            'i = (p >> 2) & 1', 'fz = 0 if p & 2 else 1', 'c = p & 1']


def _push(expr):
    return ['m[0x100 + sp] = ' + expr, 'sp = (sp - 1) & 0xFF']


def _pull(var):
    return ['sp = (sp + 1) & 0xFF', var + ' = m[0x100 + sp]']


def _ea(mode):
    if mode == 'zp':
        return ['ea = m[pc]', PC1]
    if mode == 'zpx':
        return ['ea = (m[pc] + x) & 0xFF', PC1]
    if mode == 'zpy':
        return ['ea = (m[pc] + y) & 0xFF', PC1]
    if mode == 'abs':
        return ['ea = m[pc] | (m[pc + 1] << 8)', PC2]
    if mode == 'abx':
        return ['ea = ((m[pc] | (m[pc + 1] << 8)) + x) & 0xFFFF', PC2]
    if mode == 'aby':
        return ['ea = ((m[pc] | (m[pc + 1] << 8)) + y) & 0xFFFF', PC2]
    if mode == 'izx':
        return ['t = (m[pc] + x) & 0xFF',
                'ea = m[t] | (m[(t + 1) & 0xFF] << 8)', PC1]
    if mode == 'izy':
        return ['t = m[pc]',
                'ea = ((m[t] | (m[(t + 1) & 0xFF] << 8)) + y) & 0xFFFF', PC1]
    if mode == 'izp':
        return ['t = m[pc]', 'ea = m[t] | (m[(t + 1) & 0xFF] << 8)', PC1]
    raise ValueError(mode)


def _read(mode):
    if mode == 'imm':
        return ['v = m[pc]', PC1]
    if mode in ZP_MODES:
        return _ea(mode) + ['v = m[ea]']
    return _ea(mode) + ['v = m[ea] if (ea & 0xFFFE) != 0x%04X else rd_io(ea)' % IO_VDATA]


def _write(mode, expr):
    if mode in ZP_MODES:
        return ['m[ea] = ' + expr]
    return ['if ea <= 0x%04X:' % RAM_END, '    m[ea] = ' + expr,
            'elif ea <= 0x%04X:' % IO_END, '    wr_io(ea, ' + expr + ')']


def _branch(cond):
    body = ['t = m[pc]',
            'pc = (pc + 1 + t - ((t & 0x80) << 1)) & 0xFFFF',
            'cyc += 1']
    if cond is None:
        return body
    return ['if ' + cond + ':'] + ['    ' + l for l in body] + ['else:', '    ' + PC1]


BRANCH_COND = {'BPL': 'fn < 0x80', 'BMI': 'fn >= 0x80', 'BVC': 'not fv',
               'BVS': 'fv', 'BCC': 'not c', 'BCS': 'c', 'BNE': 'fz',
               'BEQ': 'not fz', 'BRA': None}

ALU_BODY = {
    'LDA': ['a = fn = fz = v'],
    'LDX': ['x = fn = fz = v'],
    'LDY': ['y = fn = fz = v'],
    'ORA': ['a |= v', 'fn = fz = a'],
    'AND': ['a &= v', 'fn = fz = a'],
    'EOR': ['a ^= v', 'fn = fz = a'],
    'ADC': ['if d:',
            '    a, c, fv = adc_dec(a, v, c)',
            '    fn = fz = a',
            'else:',
            '    t = a + v + c',
            '    fv = (a ^ t) & (v ^ t) & 0x80',
            '    c = t >> 8',
            '    a = fn = fz = t & 0xFF'],
    'SBC': ['if d:',
            '    a, c, fv = sbc_dec(a, v, c)',
            '    fn = fz = a',
            'else:',
            '    v ^= 0xFF',
            '    t = a + v + c',
            '    fv = (a ^ t) & (v ^ t) & 0x80',
            '    c = t >> 8',
            '    a = fn = fz = t & 0xFF'],
    'CMP': ['c = 1 if a >= v else 0', 'fn = fz = (a - v) & 0xFF'],
    'CPX': ['c = 1 if x >= v else 0', 'fn = fz = (x - v) & 0xFF'],
    'CPY': ['c = 1 if y >= v else 0', 'fn = fz = (y - v) & 0xFF'],
    'BIT': ['fn = v', 'fv = v & 0x40', 'fz = a & v'],
    'NOP': [],
}

RMW_BODY = {
    'ASL': ['c = v >> 7', 'v = fn = fz = (v << 1) & 0xFF'],
    'LSR': ['c = v & 1', 'v = fn = fz = v >> 1'],
    'ROL': ['t = (v << 1) | c', 'c = t >> 8', 'v = fn = fz = t & 0xFF'],
    'ROR': ['t = (v >> 1) | (c << 7)', 'c = v & 1', 'v = fn = fz = t'],
    'INC': ['v = fn = fz = (v + 1) & 0xFF'],
    'DEC': ['v = fn = fz = (v - 1) & 0xFF'],
    'TSB': ['fz = a & v', 'v |= a'],
    'TRB': ['fz = a & v', 'v &= a ^ 0xFF'],
}

IMPLIED = {
    'CLC': ['c = 0'], 'SEC': ['c = 1'], 'CLI': ['i = 0'], 'SEI': ['i = 1'],
    'CLD': ['d = 0'], 'SED': ['d = 1'], 'CLV': ['fv = 0'],
    'TAX': ['x = fn = fz = a'], 'TXA': ['a = fn = fz = x'],
    'TAY': ['y = fn = fz = a'], 'TYA': ['a = fn = fz = y'],
    'TSX': ['x = fn = fz = sp'], 'TXS': ['sp = x'],
    'INX': ['x = fn = fz = (x + 1) & 0xFF'], 'DEX': ['x = fn = fz = (x - 1) & 0xFF'],
    'INY': ['y = fn = fz = (y + 1) & 0xFF'], 'DEY': ['y = fn = fz = (y - 1) & 0xFF'],
    'NOP': [],
    'PHA': _push('a'), 'PHX': _push('x'), 'PHY': _push('y'),
    'PHP': _push(PACK_P),
    'PLA': _pull('a') + ['fn = fz = a'],
    'PLX': _pull('x') + ['fn = fz = x'],
    'PLY': _pull('y') + ['fn = fz = y'],
    'PLP': _pull('p') + UNPACK_P,
    'RTS': ['sp = (sp + 1) & 0xFF', 't = m[0x100 + sp]', 'sp = (sp + 1) & 0xFF',
            'pc = (((m[0x100 + sp] << 8) | t) + 1) & 0xFFFF'],
    'RTI': _pull('p') + UNPACK_P +
           ['sp = (sp + 1) & 0xFF', 't = m[0x100 + sp]', 'sp = (sp + 1) & 0xFF',
            'pc = (m[0x100 + sp] << 8) | t'],
    'BRK': ['t = (pc + 1) & 0xFFFF'] + _push('t >> 8') + _push('t & 0xFF') +
           _push(PACK_P) + ['i = 1', 'd = 0', 'pc = m[0xFFFE] | (m[0xFFFF] << 8)'],
    'WAI': ['pc = (pc - 1) & 0xFFFF', 'halted = 1', 'break'],
    'STP': ['pc = (pc - 1) & 0xFFFF', 'halted = 2', 'break'],
}


def _handler(op):
    mn, mode, cyc = OPS[op]
    lines = ['cyc += %d' % cyc]
    if mode == 'rel':
        return lines + _branch(BRANCH_COND[mn])
    if mn == 'JMP':
        if mode == 'abs':
            return lines + ['pc = m[pc] | (m[pc + 1] << 8)']
        if mode == 'ind':
            return lines + ['t = m[pc] | (m[pc + 1] << 8)',
                            'pc = m[t] | (m[(t + 1) & 0xFFFF] << 8)']
        return lines + ['t = ((m[pc] | (m[pc + 1] << 8)) + x) & 0xFFFF',
                        'pc = m[t] | (m[(t + 1) & 0xFFFF] << 8)']
    if mn == 'JSR':
        return lines + ['t = (pc + 1) & 0xFFFF'] + _push('t >> 8') + \
            _push('t & 0xFF') + ['pc = m[pc] | (m[pc + 1] << 8)']
    if mn[:3] in ('RMB', 'SMB'):
        bit = 1 << int(mn[3])
        expr = 'm[ea] & 0x%02X' % (0xFF ^ bit) if mn[0] == 'R' else 'm[ea] | 0x%02X' % bit
        return lines + ['ea = m[pc]', PC1, 'm[ea] = ' + expr]
    if mn[:3] in ('BBR', 'BBS'):
        bit = 1 << int(mn[3])
        cond = ('not (t & 0x%02X)' if mn[2] == 'R' else 't & 0x%02X') % bit
        return lines + ['t = m[m[pc]]', 'o = m[pc + 1]', PC2,
                        'if ' + cond + ':',
                        '    pc = (pc + o - ((o & 0x80) << 1)) & 0xFFFF',
                        '    cyc += 1']
    if mode == 'imp':
        return lines + IMPLIED[mn]
    if mode == 'acc':
        return lines + ['v = a'] + RMW_BODY[mn] + ['a = v']
    if mn in ('STA', 'STX', 'STY', 'STZ'):
        src = {'STA': 'a', 'STX': 'x', 'STY': 'y', 'STZ': '0'}[mn]
        return lines + _ea(mode) + _write(mode, src)
    if mn in RMW_BODY:
        return lines + _read(mode) + RMW_BODY[mn] + _write(mode, 'v')
    if mn == 'BIT' and mode == 'imm':
        return lines + _read(mode) + ['fz = a & v']
    if mn == 'NOP':      # NOPs with operands: just skip them (no bus reads)
        return lines + ['pc = (pc + %d) & 0xFFFF' % (MODE_LEN[mode] - 1)]
    return lines + _read(mode) + ALU_BODY[mn]


def _tree(lo, hi, depth, out):
    ind = '    ' * depth
    if hi - lo == 1:
        for line in _handler(lo):
            out.append(ind + line)
        return
    mid = (lo + hi) // 2
    out.append(ind + 'if op < %d:' % mid)
    _tree(lo, mid, depth + 1, out)
    out.append(ind + 'else:')
    _tree(mid, hi, depth + 1, out)


def _generate(with_breakpoints):
    out = ['def run(self, budget):',
           '    m = self.mem',
           '    rd_io = self.rd_io',
           '    wr_io = self.wr_io',
           '    adc_dec = _adc_dec',
           '    sbc_dec = _sbc_dec',
           '    bps = self.breakpoints',
           '    a = self.a; x = self.x; y = self.y; sp = self.sp; pc = self.pc',
           '    fn = self.fn; fz = self.fz; fv = self.fv; c = self.c',
           '    d = self.d; i = self.i',
           '    halted = self.halted',
           '    irq_in = self.irq',
           '    cyc = 0',
           '    while cyc < budget and not halted:']
    # level-sensitive /IRQ, sampled between instructions (not when I is set)
    out.append('        if irq_in and not i:')
    for line in (['t = pc'] + _push('t >> 8') + _push('t & 0xFF') +
                 _push(PACK_P.replace('0x30', '0x20')) +
                 ['i = 1', 'd = 0', 'pc = m[0xFFFE] | (m[0xFFFF] << 8)', 'cyc += 7', 'continue']):
        out.append('            ' + line)
    if with_breakpoints:
        out.append('        if cyc and pc in bps:')
        out.append('            self.hit_breakpoint = True')
        out.append('            break')
    out.append('        op = m[pc]')
    out.append('        pc = (pc + 1) & 0xFFFF')
    _tree(0, 256, 2, out)
    out += ['    self.a = a; self.x = x; self.y = y; self.sp = sp; self.pc = pc',
            '    self.fn = fn; self.fz = fz; self.fv = fv; self.c = c',
            '    self.d = d; self.i = i',
            '    self.halted = halted',
            '    self.cycles += cyc',
            '    return cyc']
    return '\n'.join(out) + '\n'


def _adc_dec(a, v, c):
    lo = (a & 0x0F) + (v & 0x0F) + c
    if lo > 9:
        lo += 6
    hi = (a >> 4) + (v >> 4) + (1 if lo > 0x0F else 0)
    t = (hi << 4) & 0xFF
    fv = (a ^ t) & (v ^ t) & 0x80          # V as from the binary-ish sum
    if hi > 9:
        hi += 6
    c = 1 if hi > 0x0F else 0
    return ((hi << 4) | (lo & 0x0F)) & 0xFF, c, fv


def _sbc_dec(a, v, c):
    tb = a + (v ^ 0xFF) + c                 # binary result -> C and V
    fv = (a ^ tb) & ((v ^ 0xFF) ^ tb) & 0x80
    lo = (a & 0x0F) - (v & 0x0F) + c - 1
    r = a - v + c - 1
    if r < 0:
        r -= 0x60
    if lo < 0:
        r -= 0x06
    return r & 0xFF, tb >> 8, fv


_ns = {'_adc_dec': _adc_dec, '_sbc_dec': _sbc_dec}
exec(compile(_generate(False), '<65c02-fast>', 'exec'), _ns)
_run_fast = _ns['run']
_ns = {'_adc_dec': _adc_dec, '_sbc_dec': _sbc_dec}
exec(compile(_generate(True), '<65c02-bp>', 'exec'), _ns)
_run_bp = _ns['run']


class CPU65C02:
    def __init__(self, mem, rd_io, wr_io):
        self.mem = mem
        self.rd_io = rd_io
        self.wr_io = wr_io
        self.breakpoints = set()
        self.hit_breakpoint = False
        self.cycles = 0
        self.a = self.x = self.y = 0
        self.sp = 0xFD
        self.pc = 0
        self.fn = 0
        self.fz = 1
        self.fv = 0
        self.c = 0
        self.d = 0
        self.i = 1
        self.halted = 0             # 0 = running, 1 = WAI, 2 = STP
        self.irq = 0                # state of the /IRQ pin (1 = asserted/low)

    def reset(self):
        m = self.mem
        self.pc = m[0xFFFC] | (m[0xFFFD] << 8)
        self.sp = (self.sp - 3) & 0xFF
        self.d = 0
        self.i = 1
        self.halted = 0

    def run(self, budget):
        """Execute instructions for about `budget` cycles; returns cycles used."""
        self.hit_breakpoint = False
        if self.breakpoints:
            return _run_bp(self, budget)
        return _run_fast(self, budget)

    def step(self):
        return _run_fast(self, 1)

    # --- flags as a P register value
    @property
    def p(self):
        return ((self.fn & 0x80) | (0x40 if self.fv else 0) | 0x30 |
                (self.d << 3) | (self.i << 2) | (0 if self.fz else 2) | self.c)

    @p.setter
    def p(self, p):
        self.fn = p & 0x80
        self.fv = p & 0x40
        self.d = (p >> 3) & 1
        self.i = (p >> 2) & 1
        self.fz = 0 if p & 2 else 1
        self.c = p & 1

    def state(self):
        return {'a': self.a, 'x': self.x, 'y': self.y, 'sp': self.sp,
                'pc': self.pc, 'p': self.p, 'halted': self.halted,
                'cycles': self.cycles}

    def set_state(self, s):
        self.a, self.x, self.y = s['a'], s['x'], s['y']
        self.sp, self.pc = s['sp'], s['pc']
        self.p = s['p']
        self.halted = s.get('halted', 0)
        self.cycles = s.get('cycles', 0)
