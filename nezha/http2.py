"""Minimal HTTP/2 client (RFC 7540) on raw sockets, zero dependencies.

Only what a gRPC client needs: one connection, client-initiated streams,
HEADERS/DATA, SETTINGS, WINDOW_UPDATE, PING, GOAWAY, RST_STREAM and
CONTINUATION assembly. HPACK itself lives in hpack.py — this layer moves
header *blocks* (opaque bytes) between frames and the caller.

Flow control: we advertise a large initial window and top up the
connection window once, so the small messages of the Nezha protocol never
stall; inbound WINDOW_UPDATE frames for our sends are ignored because
request messages are far below the default 64 KiB window.
"""
import socket
import ssl
import struct
import threading

CLIENT_PREFACE = b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'

FRAME_DATA = 0x0
FRAME_HEADERS = 0x1
FRAME_PRIORITY = 0x2
FRAME_RST_STREAM = 0x3
FRAME_SETTINGS = 0x4
FRAME_PUSH_PROMISE = 0x5
FRAME_PING = 0x6
FRAME_GOAWAY = 0x7
FRAME_WINDOW_UPDATE = 0x8
FRAME_CONTINUATION = 0x9

FLAG_END_STREAM = 0x1
FLAG_ACK = 0x1
FLAG_END_HEADERS = 0x4
FLAG_PADDED = 0x8
FLAG_PRIORITY = 0x20

ERR_NO_ERROR = 0x0
ERR_PROTOCOL = 0x1
ERR_CANCEL = 0x8

SETTINGS_INITIAL_WINDOW_SIZE = 0x3
INITIAL_WINDOW = 1 << 24
MAX_FRAME = 16384


class Http2Error(Exception):
    pass


class StreamError(Http2Error):
    def __init__(self, error_code):
        super().__init__('stream reset, error code %d' % error_code)
        self.error_code = error_code


class Http2Stream:
    def __init__(self, conn, stream_id):
        self.conn = conn
        self.id = stream_id
        self.on_headers = None  # fn(header_block_bytes, end_stream)
        self.on_data = None     # fn(data_bytes, end_stream)
        self.on_error = None    # fn(exc)
        self._ended = False
        self._frag = None       # header block fragment accumulator
        self._frag_end_stream = False

    # -- sending -----------------------------------------------------------
    def send_headers(self, block, end_stream=False):
        flags = FLAG_END_HEADERS | (FLAG_END_STREAM if end_stream else 0)
        self.conn._send_frame(FRAME_HEADERS, flags, self.id, block)

    def send_data(self, data, end_stream=False):
        if not data:
            if end_stream:
                self.conn._send_frame(FRAME_DATA, FLAG_END_STREAM, self.id, b'')
            return
        off = 0
        while off < len(data):
            chunk = data[off:off + MAX_FRAME]
            off += len(chunk)
            last = off >= len(data)
            flags = FLAG_END_STREAM if (end_stream and last) else 0
            self.conn._send_frame(FRAME_DATA, flags, self.id, chunk)

    def close(self):
        try:
            self.conn._send_frame(FRAME_RST_STREAM, 0, self.id,
                                  struct.pack('>I', ERR_CANCEL))
        except Exception:
            pass

    # -- receiving (reader thread only) ------------------------------------
    def _deliver_headers(self, block, end_stream):
        if self.on_headers:
            self.on_headers(block, end_stream)
        if end_stream:
            self._ended = True

    def _deliver_data(self, data, end_stream):
        if self.on_data:
            self.on_data(data, end_stream)
        if end_stream:
            self._ended = True

    def _fail(self, exc):
        if self._ended:
            return
        self._ended = True
        if self.on_error:
            self.on_error(exc)


class Http2Connection:
    def __init__(self, host, port, tls=False, insecure_tls=False):
        self.host = host
        self.port = port
        self.tls = tls
        self.insecure_tls = insecure_tls
        self.sock = None
        self._send_lock = threading.Lock()
        self._streams = {}
        self._next_stream_id = 1
        self._closed = False
        self._close_handlers = []
        self._reader = None

    def on_close(self, fn):
        self._close_handlers.append(fn)

    def _emit_close(self, err):
        if self._closed:
            return
        self._closed = True
        for stream in list(self._streams.values()):
            stream._fail(err or Http2Error('connection closed'))
        for fn in self._close_handlers:
            try:
                fn(err)
            except Exception:
                pass

    # -- lifecycle ----------------------------------------------------------
    def connect(self, timeout=15):
        raw = socket.create_connection((self.host, self.port), timeout=timeout)
        if self.tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.set_alpn_protocols(['h2'])
            if self.insecure_tls:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            else:
                ctx.load_default_certs()
            raw = ctx.wrap_socket(raw, server_hostname=self.host)
            if raw.selected_alpn_protocol() not in (None, 'h2'):
                raw.close()
                raise Http2Error('server did not negotiate h2')
        raw.settimeout(None)
        self.sock = raw
        # Client preface + our SETTINGS, then a connection-level window bump.
        settings = struct.pack('>HI', SETTINGS_INITIAL_WINDOW_SIZE, INITIAL_WINDOW)
        with self._send_lock:
            self.sock.sendall(CLIENT_PREFACE)
            self._send_frame_locked(FRAME_SETTINGS, 0, 0, settings)
            self._send_frame_locked(FRAME_WINDOW_UPDATE, 0, 0,
                                    struct.pack('>I', INITIAL_WINDOW))
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self._emit_close(None)

    def new_stream(self):
        with self._send_lock:
            sid = self._next_stream_id
            self._next_stream_id += 2
        stream = Http2Stream(self, sid)
        self._streams[sid] = stream
        return stream

    # -- framing ------------------------------------------------------------
    def _send_frame_locked(self, ftype, flags, stream_id, payload):
        header = struct.pack('>I', len(payload))[1:] + struct.pack(
            '>BBI', ftype, flags, stream_id & 0x7FFFFFFF)
        self.sock.sendall(header + payload)

    def _send_frame(self, ftype, flags, stream_id, payload):
        if self._closed:
            raise Http2Error('connection closed')
        with self._send_lock:
            self._send_frame_locked(ftype, flags, stream_id, payload)

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise EOFError('socket closed')
            buf += chunk
        return bytes(buf)

    def _read_loop(self):
        err = None
        try:
            while True:
                header = self._recv_exact(9)
                length = int.from_bytes(header[0:3], 'big')
                ftype = header[3]
                flags = header[4]
                stream_id = struct.unpack('>I', header[5:9])[0] & 0x7FFFFFFF
                payload = self._recv_exact(length) if length else b''
                self._dispatch(ftype, flags, stream_id, payload)
        except Exception as e:  # EOF, socket errors, protocol errors
            err = e
        self._emit_close(err)

    def _dispatch(self, ftype, flags, stream_id, payload):
        if ftype == FRAME_SETTINGS:
            if not (flags & FLAG_ACK):
                self._send_frame(FRAME_SETTINGS, FLAG_ACK, 0, b'')
            return
        if ftype == FRAME_PING:
            if not (flags & FLAG_ACK):
                self._send_frame(FRAME_PING, FLAG_ACK, 0, payload)
            return
        if ftype == FRAME_GOAWAY:
            raise Http2Error('server sent GOAWAY')
        if ftype == FRAME_WINDOW_UPDATE:
            return
        stream = self._streams.get(stream_id)
        if stream is None:
            return
        if ftype == FRAME_RST_STREAM:
            code = struct.unpack('>I', payload[:4])[0] if len(payload) >= 4 else 0
            stream._fail(StreamError(code))
            return
        if ftype == FRAME_HEADERS:
            end_stream = bool(flags & FLAG_END_STREAM)
            frag = self._strip_headers_meta(flags, payload)
            if flags & FLAG_END_HEADERS:
                stream._deliver_headers(frag, end_stream)
            else:
                stream._frag = bytearray(frag)
                stream._frag_end_stream = end_stream
            return
        if ftype == FRAME_CONTINUATION:
            if stream._frag is None:
                raise Http2Error('unexpected CONTINUATION frame')
            stream._frag += payload
            if flags & FLAG_END_HEADERS:
                block = bytes(stream._frag)
                stream._frag = None
                stream._deliver_headers(block, stream._frag_end_stream)
            return
        if ftype == FRAME_DATA:
            data = payload
            if flags & FLAG_PADDED and data:
                pad = data[0]
                data = data[1:len(data) - pad if pad else len(data)]
            stream._deliver_data(data, bool(flags & FLAG_END_STREAM))
            return
        # PRIORITY / PUSH_PROMISE / unknown: ignore.

    @staticmethod
    def _strip_headers_meta(flags, payload):
        pos = 0
        pad = 0
        if flags & FLAG_PADDED:
            pad = payload[0]
            pos = 1
        if flags & FLAG_PRIORITY:
            pos += 5
        end = len(payload) - pad if pad else len(payload)
        return payload[pos:end]
