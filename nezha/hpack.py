"""Minimal HPACK codec (RFC 7541), zero dependencies.

Encoder: emits fully-indexed fields for static matches and literal fields
without indexing otherwise; strings are sent un-Huffman-coded (which HPACK
explicitly permits).

Decoder: full support for indexed fields, literals with/without/never
indexing, dynamic table size updates, dynamic table eviction, and
Huffman-coded strings (table in huffman_table.py, RFC 7541 Appendix B).
"""
from .huffman_table import HUFFMAN_TABLE

# RFC 7541 Appendix A static table (index is position + 1).
STATIC_TABLE = [
    (':authority', ''),
    (':method', 'GET'),
    (':method', 'POST'),
    (':path', '/'),
    (':path', '/index.html'),
    (':scheme', 'http'),
    (':scheme', 'https'),
    (':status', '200'),
    (':status', '204'),
    (':status', '206'),
    (':status', '304'),
    (':status', '400'),
    (':status', '404'),
    (':status', '500'),
    ('accept-charset', ''),
    ('accept-encoding', 'gzip, deflate'),
    ('accept-language', ''),
    ('accept-ranges', ''),
    ('accept', ''),
    ('access-control-allow-origin', ''),
    ('age', ''),
    ('allow', ''),
    ('authorization', ''),
    ('cache-control', ''),
    ('content-disposition', ''),
    ('content-encoding', ''),
    ('content-language', ''),
    ('content-length', ''),
    ('content-location', ''),
    ('content-range', ''),
    ('content-type', ''),
    ('cookie', ''),
    ('date', ''),
    ('etag', ''),
    ('expect', ''),
    ('expires', ''),
    ('from', ''),
    ('host', ''),
    ('if-match', ''),
    ('if-modified-since', ''),
    ('if-none-match', ''),
    ('if-range', ''),
    ('if-unmodified-since', ''),
    ('last-modified', ''),
    ('link', ''),
    ('location', ''),
    ('max-forwards', ''),
    ('proxy-authenticate', ''),
    ('proxy-authorization', ''),
    ('range', ''),
    ('referer', ''),
    ('refresh', ''),
    ('retry-after', ''),
    ('server', ''),
    ('set-cookie', ''),
    ('strict-transport-security', ''),
    ('transfer-encoding', ''),
    ('user-agent', ''),
    ('vary', ''),
    ('via', ''),
    ('www-authenticate', ''),
]
STATIC_BY_PAIR = {pair: i + 1 for i, pair in enumerate(STATIC_TABLE)}
STATIC_BY_NAME = {}
for _i, (_n, _v) in enumerate(STATIC_TABLE):
    STATIC_BY_NAME.setdefault(_n, _i + 1)

# ---------------------------------------------------------------------------
# Huffman decoding
# ---------------------------------------------------------------------------
# Build a lookup keyed by (code, length). Decoding walks bit by bit; at most
# 30 steps per symbol.
_HUFF_DECODE = {}
for _sym, (_code, _length) in enumerate(HUFFMAN_TABLE):
    _HUFF_DECODE[(_code, _length)] = _sym
_MAX_HUFF_LEN = max(length for _, length in HUFFMAN_TABLE)


def huffman_decode(data):
    out = bytearray()
    code = 0
    length = 0
    for byte in data:
        for shift in range(7, -1, -1):
            code = (code << 1) | ((byte >> shift) & 1)
            length += 1
            sym = _HUFF_DECODE.get((code, length))
            if sym is not None:
                if sym == 256:
                    raise ValueError('hpack: EOS in string')
                out.append(sym)
                code = 0
                length = 0
            elif length > _MAX_HUFF_LEN:
                raise ValueError('hpack: invalid huffman code')
    if length:
        # Padding must be all-ones (a prefix of the EOS code).
        if code != (1 << length) - 1:
            raise ValueError('hpack: invalid huffman padding')
    return bytes(out)


# ---------------------------------------------------------------------------
# Integer and string primitives (RFC 7541 sections 5.1, 5.2)
# ---------------------------------------------------------------------------

def encode_integer(value, prefix_bits, prefix=0):
    limit = (1 << prefix_bits) - 1
    out = bytearray()
    if value < limit:
        out.append(prefix | value)
        return bytes(out)
    out.append(prefix | limit)
    value -= limit
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def decode_integer(buf, pos, prefix_bits):
    limit = (1 << prefix_bits) - 1
    value = buf[pos] & limit
    pos += 1
    if value < limit:
        return value, pos
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        value += (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            return value, pos


def encode_string(s):
    data = s.encode('utf-8') if isinstance(s, str) else bytes(s)
    return encode_integer(len(data), 7, 0) + data  # H bit = 0 (not Huffman)


def decode_string(buf, pos):
    if pos >= len(buf):
        raise ValueError('hpack: truncated string')
    huffman = bool(buf[pos] & 0x80)
    length, pos = decode_integer(buf, pos, 7)
    data = buf[pos:pos + length]
    if len(data) < length:
        raise ValueError('hpack: truncated string data')
    pos += length
    if huffman:
        data = huffman_decode(data)
    return data.decode('utf-8', 'replace'), pos


# ---------------------------------------------------------------------------
# Encoder
# ---------------------------------------------------------------------------

class Encoder:
    def encode(self, headers):
        """headers: list of (name, value) in wire order."""
        out = bytearray()
        for name, value in headers:
            pair_idx = STATIC_BY_PAIR.get((name, value))
            if pair_idx:
                out += encode_integer(pair_idx, 7, 0x80)  # indexed field
                continue
            name_idx = STATIC_BY_NAME.get(name, 0)
            # Literal without indexing, 4-bit prefix (0x00).
            out += encode_integer(name_idx, 4, 0x00)
            if not name_idx:
                out += encode_string(name)
            out += encode_string(value)
        return bytes(out)


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------

class Decoder:
    def __init__(self, max_table_size=4096):
        self.dynamic = []  # list of (name, value), newest first
        self.max_table_size = max_table_size
        self._table_bytes = 0

    def _entry(self, index):
        if index == 0:
            raise ValueError('hpack: invalid index 0')
        if index <= len(STATIC_TABLE):
            return STATIC_TABLE[index - 1]
        dyn = index - len(STATIC_TABLE) - 1
        if dyn < len(self.dynamic):
            return self.dynamic[dyn]
        raise ValueError('hpack: index out of range')

    def _add_dynamic(self, name, value):
        size = len(name) + len(value) + 32
        if size > self.max_table_size:
            self.dynamic.clear()
            self._table_bytes = 0
            return
        while self._table_bytes + size > self.max_table_size and self.dynamic:
            n, v = self.dynamic.pop()
            self._table_bytes -= len(n) + len(v) + 32
        self.dynamic.insert(0, (name, value))
        self._table_bytes += size

    def decode(self, buf):
        headers = []
        pos = 0
        while pos < len(buf):
            b = buf[pos]
            if b & 0x80:  # indexed field
                index, pos = decode_integer(buf, pos, 7)
                headers.append(self._entry(index))
            elif b & 0x40:  # literal, incremental indexing
                index, pos = decode_integer(buf, pos, 6)
                name, value, pos = self._read_pair(buf, pos, index)
                headers.append((name, value))
                self._add_dynamic(name, value)
            elif b & 0x20:  # dynamic table size update
                self.max_table_size, pos = decode_integer(buf, pos, 5)
                while self._table_bytes > self.max_table_size and self.dynamic:
                    n, v = self.dynamic.pop()
                    self._table_bytes -= len(n) + len(v) + 32
            else:  # literal without indexing (0000xxxx) / never indexed (0001xxxx)
                index, pos = decode_integer(buf, pos, 4)
                name, value, pos = self._read_pair(buf, pos, index)
                headers.append((name, value))
        return headers

    def _read_pair(self, buf, pos, index):
        if index:
            name = self._entry(index)[0]
        else:
            name, pos = decode_string(buf, pos)
        value, pos = decode_string(buf, pos)
        return name, value, pos
