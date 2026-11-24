"""Dashboard task handling for pure mode.

Task type numbers come from the official agent (model/task.go):
  1 HTTPGet, 2 ICMPPing, 3 TCPPing, 4 Command, 5 Terminal, 6 Upgrade,
  7 Keepalive, 8 TerminalGRPC, 9 NAT, 10 ReportHostInfo (deprecated),
  11 FM, 12 ReportConfig, 13 ApplyConfig, 14 ServerTransferApply,
  15 Exec, 16-20 Fs*

Implemented here: HTTPGet, TCPPing, Command (unless disabled), Keepalive.
ICMPPing needs raw sockets, which the Python standard library cannot open
without privileges — it reports a failure result instead of faking a
measurement. Everything else is answered with successful=False so the
dashboard never hangs waiting.
"""
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

TASK_HTTP_GET = 1
TASK_ICMP_PING = 2
TASK_TCP_PING = 3
TASK_COMMAND = 4
TASK_TERMINAL = 5
TASK_UPGRADE = 6
TASK_KEEPALIVE = 7
TASK_REPORT_HOST_INFO = 10

USER_AGENT = 'nezha-agent-py/1.0.0'


def http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            res.read(0)
            status = res.status
    except urllib.error.HTTPError as e:
        status = e.code  # an HTTP response still proves reachability
    return {'status': status, 'delay': (time.time() - start) * 1000}


def tcp_ping(target):
    target = target.strip()
    if target.startswith('['):  # [ipv6]:port
        host, _, rest = target[1:].partition(']')
        port = int(rest.lstrip(':'))
    else:
        host, _, port_s = target.rpartition(':')
        if not _:
            raise ValueError('invalid host:port')
        port = int(port_s)
    start = time.time()
    with socket.create_connection((host, port), timeout=5):
        pass
    return {'delay': (time.time() - start) * 1000}


def run_command(command, timeout=120):
    try:
        proc = subprocess.run(
            command, shell=True, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        out = proc.stdout.decode('utf-8', 'replace')[-4 * 1024 * 1024:]
        return {'ok': proc.returncode == 0, 'data': out}
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b'').decode('utf-8', 'replace')
        return {'ok': False, 'data': (out + '\ncommand timed out').strip()}
    except Exception as e:
        return {'ok': False, 'data': str(e)}


def handle_task(cfg, task):
    """task: {id, type, data} -> TaskResult dict."""
    base = {'id': task['id'], 'type': task['type'], 'delay': 0.0,
            'data': '', 'successful': False}
    start = time.time()
    ttype = task['type']
    try:
        if ttype == TASK_KEEPALIVE:
            return {**base, 'successful': True}

        if ttype == TASK_HTTP_GET:
            r = http_get(task['data'])
            return {**base, 'delay': r['delay'], 'data': str(r['status']),
                    'successful': 200 <= r['status'] < 400}

        if ttype == TASK_TCP_PING:
            r = tcp_ping(task['data'])
            return {**base, 'delay': r['delay'], 'successful': True}

        if ttype == TASK_ICMP_PING:
            return {**base,
                    'data': 'ICMP ping is not supported in pure Python mode '
                            '(no raw socket access)',
                    'successful': False}

        if ttype == TASK_COMMAND:
            if cfg['disable_command']:
                return {**base,
                        'data': 'command execution disabled '
                                '(NEZHA_DISABLE_COMMAND=1)',
                        'successful': False}
            r = run_command(task['data'])
            return {**base, 'delay': (time.time() - start) * 1000,
                    'data': r['data'], 'successful': r['ok']}

        if ttype == TASK_REPORT_HOST_INFO:
            # Host info is re-reported on the periodic timer in agent.py.
            return {**base, 'successful': True}

        return {**base,
                'data': 'task type %d is not supported in pure Python mode'
                        % ttype,
                'successful': False}
    except Exception as e:
        return {**base, 'delay': (time.time() - start) * 1000,
                'data': str(e), 'successful': False}
