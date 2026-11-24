"""Minimal Protocol Buffers (proto3) wire-format codec, zero dependencies.

Supports the wire types the Nezha protocol uses:
  0 = varint (uint64 / bool / enum)
  1 = 64-bit (double)
  2 = length-delimited (string / bytes / embedded message)
  5 = 32-bit (float)
Python ints are arbitrary precision, so 64-bit values stay exact.
"""
import struct


def encode_varint(value):
    v = int(value)
    if v < 0:
        v += 1 << 64  # two's complement
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def read_varint(buf, offset):
    result = 0
    shift = 0
    pos = offset
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos - offset
        shift += 7
        if shift > 70:
            raise ValueError('protobuf: varint too long')
    raise ValueError('protobuf: truncated varint')


class Writer:
    def __init__(self):
        self.parts = []

    def _raw(self, buf):
        self.parts.append(buf)
        return self

    def _tag(self, field, wire):
        return self._raw(encode_varint((field << 3) | wire))

    def varint(self, field, value):
        self._tag(field, 0)
        return self._raw(encode_varint(value))

    def boolean(self, field, value):
        return self.varint(field, 1 if value else 0)

    def bytes(self, field, buf):
        buf = bytes(buf)
        self._tag(field, 2)
        self._raw(encode_varint(len(buf)))
        return self._raw(buf)

    def string(self, field, s):
        return self.bytes(field, str(s).encode('utf-8'))

    def message(self, field, buf):
        return self.bytes(field, buf)

    def double(self, field, value):
        self._tag(field, 1)
        return self._raw(struct.pack('<d', value))

    def float(self, field, value):
        self._tag(field, 5)
        return self._raw(struct.pack('<f', value))

    def finish(self):
        return b''.join(self.parts)


class Field:
    __slots__ = ('field', 'wire', 'value', 'buffer')

    def __init__(self, field, wire, value=None, buffer=None):
        self.field = field
        self.wire = wire
        self.value = value
        self.buffer = buffer


def parse(buf):
    """Parse a message into a flat list of Field objects.

    For wire 0, ``value`` is the int; for wire 1/5, ``value`` is the decoded
    double/float and ``buffer`` the raw bytes; for wire 2, ``buffer`` is the
    payload.
    """
    out = []
    off = 0
    n = len(buf)
    while off < n:
        tag, size = read_varint(buf, off)
        off += size
        field = tag >> 3
        wire = tag & 7
        if wire == 0:
            value, size = read_varint(buf, off)
            off += size
            out.append(Field(field, wire, value=value))
        elif wire == 1:
            b = buf[off:off + 8]
            if len(b) < 8:
                raise ValueError('protobuf: truncated fixed64')
            off += 8
            out.append(Field(field, wire, value=struct.unpack('<d', b)[0], buffer=b))
        elif wire == 2:
            length, size = read_varint(buf, off)
            off += size
            b = buf[off:off + length]
            if len(b) < length:
                raise ValueError('protobuf: truncated bytes')
            off += length
            out.append(Field(field, wire, buffer=b))
        elif wire == 5:
            b = buf[off:off + 4]
            if len(b) < 4:
                raise ValueError('protobuf: truncated fixed32')
            off += 4
            out.append(Field(field, wire, value=struct.unpack('<f', b)[0], buffer=b))
        else:
            raise ValueError('protobuf: unsupported wire type %d' % wire)
    return out


def first(parsed, field):
    for f in parsed:
        if f.field == field:
            return f
    return None


def all_fields(parsed, field):
    return [f for f in parsed if f.field == field]


def u64(parsed, field, default=0):
    f = first(parsed, field)
    return f.value if f is not None and f.value is not None else default


def string(parsed, field, default=''):
    f = first(parsed, field)
    return f.buffer.decode('utf-8', 'replace') if f is not None and f.buffer is not None else default


def boolean(parsed, field, default=False):
    f = first(parsed, field)
    return (f.value != 0) if f is not None and f.value is not None else default
