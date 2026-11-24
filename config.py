"""Configuration: environment variables win; a `.env` file next to main.py
fills in the rest. No secrets are ever hard-coded here.
"""
import os
import re
import uuid as uuid_mod

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AGENT_VERSION = '1.0.0'


def _load_dotenv(dir_path):
    path = os.path.join(dir_path, '.env')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return
    for line in text.splitlines():
        m = re.match(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$', line)
        if not m:
            continue
        value = m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        if m.group(1) not in os.environ:
            os.environ[m.group(1)] = value


def _truthy(v):
    return bool(re.match(r'^(1|true|yes|on)$', v or '', re.IGNORECASE))


def _parse_server(server):
    """'host:port', optionally prefixed with http:// or https://."""
    s = (server or '').strip()
    tls_hint = False
    if s.startswith('https://'):
        tls_hint = True
        s = s[len('https://'):]
    elif s.startswith('http://'):
        s = s[len('http://'):]
    s = s.split('/')[0]
    host = s
    port = 8008  # Nezha dashboard default gRPC port
    idx = s.rfind(':')
    if idx > 0:
        host = s[:idx]
        try:
            port = int(s[idx + 1:]) or port
        except ValueError:
            pass
    return host, port, tls_hint


def _resolve_uuid(base_dir):
    """The dashboard identifies agents by UUID. If the user doesn't provide
    one, generate it once and persist it so restarts keep the same identity."""
    env = os.environ.get('NEZHA_UUID')
    if env:
        return env.strip()
    path = os.path.join(base_dir, '.nezha-uuid')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            saved = f.read().strip()
        if saved:
            return saved
    except OSError:
        pass
    new_uuid = str(uuid_mod.uuid4())
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(new_uuid + '\n')
    except OSError:
        pass
    return new_uuid


def load_config():
    _load_dotenv(BASE_DIR)
    server = os.environ.get('NEZHA_SERVER', '')
    host, port, tls_hint = _parse_server(server)
    tls_env = os.environ.get('NEZHA_TLS')
    if tls_env:
        tls = _truthy(tls_env)
    else:
        tls = tls_hint or port == 443
    try:
        report_sec = int(os.environ.get('NEZHA_REPORT_INTERVAL', '2'))
    except ValueError:
        report_sec = 2
    if report_sec <= 0:
        report_sec = 2
    return {
        'server': server,
        'host': host,
        'port': port,
        'tls': tls,
        'insecure_tls': _truthy(os.environ.get('NEZHA_INSECURE_TLS')),
        'key': os.environ.get('NEZHA_KEY', ''),
        'mode': 'binary' if (os.environ.get('NEZHA_MODE', 'pure').lower()
                             == 'binary') else 'pure',
        'uuid': _resolve_uuid(BASE_DIR),
        'disable_command': _truthy(os.environ.get('NEZHA_DISABLE_COMMAND')),
        'report_interval_ms': report_sec * 1000,
        'version': AGENT_VERSION,
        'base_dir': BASE_DIR,
    }
