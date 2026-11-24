"""Minimal gRPC client on top of the hand-rolled HTTP/2 layer (http2.py)
and HPACK codec (hpack.py). No grpc library, no generated code.

gRPC message framing: every message on the wire is a 5-byte prefix —
1 byte compression flag (0 = identity) followed by a 4-byte big-endian
message length — then the protobuf payload.
"""
import threading
from urllib.parse import unquote

from . import hpack
from .http2 import Http2Connection

SERVICE_PATH = '/proto.NezhaService/'  # package proto, service NezhaService
USER_AGENT = 'nezha-agent-py/1.0.0'


def frame_message(payload):
    return b'\x00' + len(payload).to_bytes(4, 'big') + payload


class FrameParser:
    def __init__(self, on_message):
        self.buf = bytearray()
        self.on_message = on_message

    def push(self, chunk):
        self.buf += chunk
        while True:
            if len(self.buf) < 5:
                return
            compressed = self.buf[0]
            length = int.from_bytes(self.buf[1:5], 'big')
            if len(self.buf) < 5 + length:
                return
            msg = bytes(self.buf[5:5 + length])
            del self.buf[:5 + length]
            if compressed:
                raise ValueError('grpc: compressed frames not supported')
            self.on_message(msg)


class GrpcError(Exception):
    def __init__(self, status, message=''):
        msg = unquote(message) if message else 'grpc status %s' % status
        super().__init__(msg)
        self.status = str(status)


class _GrpcStreamBase:
    """Shared header/status bookkeeping for unary and bidi streams."""

    def __init__(self, conn, method):
        self.conn = conn
        self.stream = conn.h2.new_stream()
        self.status = None
        self.message = ''
        self._finished = False
        headers = [
            (':method', 'POST'),
            (':path', SERVICE_PATH + method),
            (':scheme', 'https' if conn.tls else 'http'),
            (':authority', '%s:%d' % (conn.host, conn.port)),
            ('content-type', 'application/grpc'),
            ('user-agent', USER_AGENT),
            ('te', 'trailers'),
        ]
        for k, v in conn.metadata.items():
            headers.append((k, v))
        self._headers = headers

    def _start(self):
        block = self.conn.encoder.encode(self._headers)
        self.stream.send_headers(block, end_stream=False)

    def _grab_status(self, header_block):
        for name, value in self.conn.decoder.decode(header_block):
            if name == 'grpc-status':
                self.status = value
            elif name == 'grpc-message':
                self.message = value


class GrpcStream(_GrpcStreamBase):
    """Bidirectional stream: write() TaskResults, on_message gets Tasks."""

    def __init__(self, conn, method):
        super().__init__(conn, method)
        self._msg_handlers = []
        self._err_handlers = []
        self._end_handlers = []
        self._parser = FrameParser(self._emit_message)
        self.stream.on_headers = self._on_headers
        self.stream.on_data = self._on_data
        self.stream.on_error = self._on_error
        self._start()

    def on_message(self, fn):
        self._msg_handlers.append(fn)

    def on_error(self, fn):
        self._err_handlers.append(fn)

    def on_end(self, fn):
        self._end_handlers.append(fn)

    def _emit_message(self, msg):
        for fn in self._msg_handlers:
            fn(msg)

    def _finish(self, err=None):
        if self._finished:
            return
        self._finished = True
        if err is None and self.status is not None and self.status != '0':
            err = GrpcError(self.status, self.message)
        if err is not None:
            for fn in self._err_handlers:
                fn(err)
        else:
            for fn in self._end_handlers:
                fn()

    def _on_headers(self, block, end_stream):
        self._grab_status(block)
        if end_stream:
            self._finish()

    def _on_data(self, data, end_stream):
        try:
            self._parser.push(data)
        except Exception as e:
            self._finish(e)
            return
        if end_stream:
            self._finish()

    def _on_error(self, exc):
        self._finish(exc)

    def write(self, payload):
        self.stream.send_data(frame_message(payload))

    def end(self):
        try:
            self.stream.send_data(b'', end_stream=True)
        except Exception:
            pass

    def close(self):
        self.stream.close()


class GrpcConnection:
    def __init__(self, host, port, tls=False, insecure_tls=False, metadata=None):
        self.host = host
        self.port = port
        self.tls = tls
        self.metadata = metadata or {}
        self.h2 = Http2Connection(host, port, tls=tls, insecure_tls=insecure_tls)
        # HPACK state is per-connection; the encoder is stateless here, the
        # decoder must only be touched from the h2 reader thread — which is
        # exactly where header callbacks run.
        self.encoder = hpack.Encoder()
        self.decoder = hpack.Decoder()

    def connect(self):
        self.h2.connect()

    def close(self):
        self.h2.close()

    def on_close(self, fn):
        self.h2.on_close(fn)

    def unary(self, method, payload, timeout=30):
        """Blocking unary call; returns the response message payload."""
        done = threading.Event()
        result = {}

        stream = _GrpcStreamBase(self, method)
        messages = []

        def on_headers(block, end_stream):
            stream._grab_status(block)
            if end_stream:
                done.set()

        def on_data(data, end_stream):
            try:
                stream._parser.push(data)
            except Exception as e:
                result['error'] = e
                done.set()
                return
            if end_stream:
                done.set()

        def on_error(exc):
            result['error'] = exc
            done.set()

        stream._parser = FrameParser(messages.append)
        stream.stream.on_headers = on_headers
        stream.stream.on_data = on_data
        stream.stream.on_error = on_error
        stream._start()
        stream.stream.send_data(frame_message(payload), end_stream=True)

        if not done.wait(timeout):
            stream.stream.close()
            raise GrpcError('deadline', 'unary call timed out')
        if 'error' in result:
            raise result['error']
        if stream.status is not None and stream.status != '0':
            raise GrpcError(stream.status, stream.message)
        return messages[0] if messages else b''

    def bidi(self, method):
        return GrpcStream(self, method)
