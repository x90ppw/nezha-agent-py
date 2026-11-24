"""Host / State sampling using only the Python standard library.

Counters that need /proc (network totals, swap, connection & process
counts, CPU times) are Linux-only; elsewhere they report 0, like the
official agent does on platforms where a counter is unavailable.
"""
import os
import platform
import re
import shutil
import sys
import time

IS_LINUX = sys.platform.startswith('linux')


def arch_name():
    m = platform.machine()
    return {'x86_64': 'amd64', 'AMD64': 'amd64', 'aarch64': 'arm64',
            'arm64': 'arm64', 'armv7l': 'arm', 'armv6l': 'arm',
            'i386': '386', 'i686': '386'}.get(m, m)


def _read_proc(path):
    try:
        with open(path, 'r', errors='replace') as f:
            return f.read()
    except OSError:
        return None


def _meminfo():
    text = _read_proc('/proc/meminfo')
    out = {}
    if not text:
        return out
    for line in text.splitlines():
        m = re.match(r'^(\w+):\s+(\d+)\s*kB', line)
        if m:
            out[m.group(1)] = int(m.group(2)) * 1024
    return out


def _mem_total():
    mi = _meminfo()
    if mi.get('MemTotal'):
        return mi['MemTotal']
    try:
        return os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
    except (ValueError, OSError, AttributeError):
        return 0


def _mem_free():
    mi = _meminfo()
    if mi.get('MemAvailable'):
        return mi['MemAvailable']
    try:
        return os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_AVPHYS_PAGES')
    except (ValueError, OSError, AttributeError):
        return 0


def _disk_stat():
    try:
        usage = shutil.disk_usage('C:\\' if sys.platform == 'win32' else '/')
        return {'total': usage.total, 'used': usage.used}
    except OSError:
        return {'total': 0, 'used': 0}


def _net_counters():
    text = _read_proc('/proc/net/dev')
    if not text:
        return {'in': 0, 'out': 0}
    in_total = 0
    out_total = 0
    for line in text.splitlines()[2:]:
        parts = line.replace(':', ' ').split()
        if len(parts) < 10:
            continue
        if parts[0] == 'lo':
            continue
        try:
            in_total += int(parts[1])
            out_total += int(parts[9])
        except ValueError:
            continue
    return {'in': in_total, 'out': out_total}


def _count_lines(path):
    text = _read_proc(path)
    if not text:
        return 0
    return max(0, len(text.strip().splitlines()) - 1)  # minus header line


def _conn_counts():
    if not IS_LINUX:
        return {'tcp': 0, 'udp': 0}
    tcp = _count_lines('/proc/net/tcp') + _count_lines('/proc/net/tcp6')
    udp = _count_lines('/proc/net/udp') + _count_lines('/proc/net/udp6')
    return {'tcp': tcp, 'udp': udp}


def _process_count():
    if not IS_LINUX:
        return 0
    try:
        return sum(1 for n in os.listdir('/proc') if n.isdigit())
    except OSError:
        return 0


def _cpu_times():
    """(idle, total) jiffies from /proc/stat; (0, 0) when unavailable."""
    text = _read_proc('/proc/stat')
    if not text:
        return (0, 0)
    for line in text.splitlines():
        if line.startswith('cpu '):
            parts = [int(x) for x in line.split()[1:]]
            idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
            return (idle, sum(parts))
    return (0, 0)


def _cpu_model():
    text = _read_proc('/proc/cpuinfo')
    if text:
        count = 0
        model = ''
        for line in text.splitlines():
            if line.startswith('processor'):
                count += 1
            elif line.startswith('model name') and not model:
                model = line.split(':', 1)[1].strip()
        if model:
            return ['%s (%d cores)' % (model, count or 1)]
    try:
        n = os.cpu_count() or 1
    except Exception:
        n = 1
    name = platform.processor() or platform.machine()
    return ['%s (%d cores)' % (name, n)] if name else []


def _boot_time():
    text = _read_proc('/proc/stat')
    if text:
        for line in text.splitlines():
            if line.startswith('btime'):
                return int(line.split()[1])
    up = _uptime()
    return int(time.time() - up) if up else 0


def _uptime():
    text = _read_proc('/proc/uptime')
    if text:
        try:
            return float(text.split()[0])
        except ValueError:
            pass
    return 0.0


def collect_host(cfg):
    mi = _meminfo()
    disk = _disk_stat()
    return {
        # sys.platform strings match Go's: linux / darwin / win32
        'platform': sys.platform,
        'platform_version': platform.release(),
        'cpu': _cpu_model(),
        'mem_total': _mem_total(),
        'disk_total': disk['total'],
        'swap_total': mi.get('SwapTotal', 0),
        'arch': arch_name(),
        'virtualization': '',
        'boot_time': _boot_time(),
        'version': cfg['version'],
        'gpu': [],
    }


class StateCollector:
    """Diffs CPU/network counters between calls to derive utilization
    percentages and per-second speeds (mirrors the JS collector)."""

    def __init__(self):
        self._prev_cpu = _cpu_times()
        self._prev_net = _net_counters()
        self._prev_time = time.time()

    def collect(self):
        now = time.time()
        dt = max(1.0, now - self._prev_time)

        cur_cpu = _cpu_times()
        total_diff = cur_cpu[1] - self._prev_cpu[1]
        idle_diff = cur_cpu[0] - self._prev_cpu[0]
        if total_diff > 0:
            cpu_pct = min(100.0, max(0.0, 100.0 * (1 - idle_diff / total_diff)))
        else:
            cpu_pct = 0.0

        cur_net = _net_counters()
        in_speed = max(0, round((cur_net['in'] - self._prev_net['in']) / dt))
        out_speed = max(0, round((cur_net['out'] - self._prev_net['out']) / dt))

        self._prev_cpu = cur_cpu
        self._prev_net = cur_net
        self._prev_time = now

        mi = _meminfo()
        disk = _disk_stat()
        conns = _conn_counts()
        try:
            load = os.getloadavg()
        except (OSError, AttributeError):
            load = (0.0, 0.0, 0.0)
        mem_total = _mem_total()
        mem_free = _mem_free()

        return {
            'cpu': cpu_pct,
            'mem_used': max(0, mem_total - mem_free),
            'swap_used': max(0, mi.get('SwapTotal', 0) - mi.get('SwapFree', 0))
                         if mi.get('SwapTotal') else 0,
            'disk_used': disk['used'],
            'net_in_transfer': cur_net['in'],
            'net_out_transfer': cur_net['out'],
            'net_in_speed': in_speed,
            'net_out_speed': out_speed,
            'uptime': int(_uptime()),
            'load1': load[0],
            'load5': load[1],
            'load15': load[2],
            'tcp_conn_count': conns['tcp'],
            'udp_conn_count': conns['udp'],
            'process_count': _process_count(),
        }
