"""Pure-Python Nezha agent: speaks the real Nezha gRPC protocol using only
the standard library (raw-socket HTTP/2 + hand-rolled HPACK + protobuf).
It downloads nothing, writes no executable files and spawns no agent
processes — the agent *is* this Python process.
"""
import json
import threading
import time
import urllib.request

from . import messages
from .grpc import GrpcConnection, GrpcError
from .hostinfo import StateCollector, collect_host
from .tasks import handle_task

AGENT = 'nezha-agent-py'
USER_AGENT = 'nezha-agent-py/1.0.0'


def log(*args):
    print('[%s]' % AGENT, *args, flush=True)


def _fetch_geoip():
    req = urllib.request.Request('https://ipapi.co/json/',
                                 headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=8) as res:
        return json.loads(res.read().decode('utf-8'))


def _report_geoip(conn):
    info = _fetch_geoip()
    ip = info.get('ip') or ''
    is6 = ':' in ip
    geo = {
        'use6': is6,
        'ip': {'ipv6': ip} if is6 else {'ipv4': ip},
        'country_code': (info.get('country_code') or '').lower(),
        'dashboard_boot_time': 0,
    }
    conn.unary('ReportGeoIP', messages.encode_geoip(geo))


def _report_host_info(conn, cfg):
    payload = messages.encode_host(collect_host(cfg))
    try:
        res = conn.unary('ReportSystemInfo2', payload)
        receipt = messages.decode_uint64_receipt(res)
        log('registered (ReportSystemInfo2), dashboard_boot_time=%d'
            % receipt['data'])
        return receipt['data']
    except GrpcError as e:
        # Older dashboards don't implement ReportSystemInfo2
        # (grpc UNIMPLEMENTED = 12).
        if e.status == '12':
            conn.unary('ReportSystemInfo', payload)
            log('registered (ReportSystemInfo fallback)')
            return 0
        raise


def _run_session(cfg):
    conn = GrpcConnection(
        cfg['host'], cfg['port'], tls=cfg['tls'],
        insecure_tls=cfg['insecure_tls'],
        metadata={
            # The official agent sends both dash and underscore spellings.
            'client-secret': cfg['key'],
            'client-uuid': cfg['uuid'],
            'client_secret': cfg['key'],
            'client_uuid': cfg['uuid'],
        },
    )
    closed = threading.Event()
    stop = threading.Event()
    close_err = {}

    def _on_close(err):
        close_err['err'] = err
        closed.set()

    conn.on_close(_on_close)
    conn.connect()
    log('connected to %s:%d (tls=%s)' % (cfg['host'], cfg['port'], cfg['tls']))

    try:
        _report_host_info(conn, cfg)
        threading.Thread(target=lambda: _swallow(_report_geoip, conn),
                         daemon=True).start()

        # --- state stream: push a State every report interval ---
        collector = StateCollector()
        state_stream = conn.bidi('ReportSystemState')
        state_stream.on_message(lambda buf: None)  # Receipt — nothing to do
        state_stream.on_error(lambda e: None)

        def push_loop():
            while not stop.is_set():
                try:
                    state_stream.write(messages.encode_state(collector.collect()))
                except Exception:
                    return
                stop.wait(cfg['report_interval_ms'] / 1000.0)

        pusher = threading.Thread(target=push_loop, daemon=True)
        pusher.start()

        # --- task stream: dashboard pushes Tasks, we answer TaskResults ---
        task_stream = conn.bidi('RequestTask')
        task_stream.on_error(lambda e: None)

        def on_task(buf):
            try:
                task = messages.decode_task(buf)
            except Exception:
                return

            def answer():
                result = handle_task(cfg, task)
                try:
                    task_stream.write(messages.encode_task_result(result))
                except Exception:
                    pass

            # Handle each task on its own thread: a long Command must not
            # stall the reader thread that feeds both streams.
            threading.Thread(target=answer, daemon=True).start()

        task_stream.on_message(on_task)

        # --- periodic host re-report (official agent re-reports on IP
        # change; a fixed 10-minute cadence is the minimal equivalent) ---
        def rereport_loop():
            while not stop.wait(600):
                _swallow(_report_host_info, conn, cfg)
                _swallow(_report_geoip, conn)

        rereporter = threading.Thread(target=rereport_loop, daemon=True)
        rereporter.start()

        closed.wait()  # block until the session drops
        err = close_err.get('err')
        if err is not None:
            raise err
        raise ConnectionError('connection closed')
    finally:
        stop.set()
        conn.close()


def _swallow(fn, *args):
    try:
        fn(*args)
    except Exception:
        pass


def run_pure(cfg):
    log('pure mode: server=%s uuid=%s' % (cfg['server'], cfg['uuid']))
    while True:
        try:
            _run_session(cfg)
        except Exception as e:
            log('session ended:', e)
        log('reconnecting in 5s…')
        time.sleep(5)
