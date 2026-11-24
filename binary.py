"""Binary mode: run the *official* nezha-agent, but camouflaged.
  - downloaded from the official GitHub Releases (nezhahq/agent)
  - stored under a media-player disguise name
  - all credentials are passed through a config file (-c config.yml),
    never on the command line, so `ps` shows nothing sensitive
  - supervised: restarted with backoff if it exits

NOTE: this module is the downloader/launcher only. Whether you run it at
all is your choice at startup (NEZHA_MODE=binary); pure mode never
touches this code path.
"""
import os
import platform
import random
import signal
import subprocess
import sys
import time
import urllib.request
import zipfile

RUNTIME_DIR_NAME = '.runtime'
DISGUISE_LINUX = ['.music-player', '.spotify-client', '.netease-music']
USER_AGENT = 'nezha-agent-py/1.0.0'


def release_asset():
    """Official release asset naming: nezha-agent_<os>_<arch>.zip"""
    os_map = {'linux': 'linux', 'darwin': 'darwin', 'win32': 'windows'}
    arch_map = {'x86_64': 'amd64', 'AMD64': 'amd64', 'aarch64': 'arm64',
                'arm64': 'arm64', 'armv7l': 'arm', 'i386': '386', 'i686': '386'}
    os_name = os_map.get(sys.platform)
    arch_name = arch_map.get(platform.machine())
    if not os_name or not arch_name:
        raise RuntimeError('unsupported platform/arch: %s/%s'
                           % (sys.platform, platform.machine()))
    return 'nezha-agent_%s_%s.zip' % (os_name, arch_name)


def release_url(asset):
    return ('https://github.com/nezhahq/agent/releases/latest/download/'
            + asset)


def _download(url, dest):
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as res, \
            open(dest, 'wb') as out:
        while True:
            chunk = res.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)


def _extract_entry(zip_path, out_path):
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            base = os.path.basename(name)
            if base in ('nezha-agent', 'nezha-agent.exe'):
                with zf.open(name) as src, open(out_path, 'wb') as dst:
                    while True:
                        chunk = src.read(1 << 16)
                        if not chunk:
                            break
                        dst.write(chunk)
                return name
    raise RuntimeError('zip: agent binary not found in archive')


def _pick_disguise_name(runtime_dir):
    marker = os.path.join(runtime_dir, '.bin-name')
    try:
        with open(marker, 'r', encoding='utf-8') as f:
            saved = f.read().strip()
        if saved:
            return saved
    except OSError:
        pass
    if sys.platform == 'win32':
        name = 'MusicPlayer_%06x.exe' % random.getrandbits(24)
    else:
        name = random.choice(DISGUISE_LINUX)
    try:
        with open(marker, 'w', encoding='utf-8') as f:
            f.write(name + '\n')
    except OSError:
        pass
    return name


def _write_agent_config(runtime_dir, cfg):
    """The official agent reads a YAML config file via `-c <file>`; secrets
    live only in this file (mode 0600), never in argv."""
    lines = [
        'server: %s:%d' % (cfg['host'], cfg['port']),
        'client_secret: %s' % cfg['key'],
        'uuid: %s' % cfg['uuid'],
        'tls: %s' % ('true' if cfg['tls'] else 'false'),
        'insecure_tls: %s' % ('true' if cfg['insecure_tls'] else 'false'),
        'disable_command_execute: %s'
        % ('true' if cfg['disable_command'] else 'false'),
        'disable_auto_update: true',
        'disable_force_update: true',
        'report_delay: 2',
        '',
    ]
    path = os.path.join(runtime_dir, 'config.yml')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return path


def run_binary(cfg):
    runtime_dir = os.path.join(cfg['base_dir'], RUNTIME_DIR_NAME)
    os.makedirs(runtime_dir, exist_ok=True)

    bin_name = _pick_disguise_name(runtime_dir)
    bin_path = os.path.join(runtime_dir, bin_name)

    if not os.path.exists(bin_path):
        asset = release_asset()
        zip_path = os.path.join(runtime_dir, asset)
        print('[nezha-agent-py] binary mode: downloading %s'
              % release_url(asset), flush=True)
        _download(release_url(asset), zip_path)
        found = _extract_entry(zip_path, bin_path)
        os.unlink(zip_path)
        if sys.platform != 'win32':
            os.chmod(bin_path, 0o755)
        print('[nezha-agent-py] extracted %s -> %s' % (found, bin_name),
              flush=True)

    config_file = _write_agent_config(runtime_dir, cfg)
    print('[nezha-agent-py] binary mode: starting %s -c config.yml'
          % bin_name, flush=True)

    state = {'stopping': False, 'child': None}

    def shutdown(signum, frame):
        state['stopping'] = True
        child = state['child']
        if child is not None:
            try:
                child.terminate()
            except Exception:
                pass
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    backoff = 2.0
    while not state['stopping']:
        child = subprocess.Popen([bin_path, '-c', config_file])
        state['child'] = child
        code = child.wait()
        if state['stopping']:
            break
        print('[nezha-agent-py] agent exited (code=%s), restarting in %gs'
              % (code, backoff), flush=True)
        time.sleep(backoff)
        backoff = min(backoff * 2, 60.0)
