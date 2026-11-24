#!/usr/bin/env python3
"""nezha-agent-py — a Nezha monitoring agent written in Python.

  NEZHA_MODE=pure   (default) the agent is implemented in pure Python:
                    hand-rolled gRPC + HPACK + protobuf over a raw-socket
                    HTTP/2 client. No binary is downloaded, no child agent
                    process exists.
  NEZHA_MODE=binary downloads the official nezha-agent, stores it under
                    a disguise name and supervises it (see binary.py).

Configuration is via environment variables or a .env file next to this
file — see README.md for the full table.
"""
import sys

from config import load_config


def main():
    cfg = load_config()
    if not cfg['server'] or not cfg['host']:
        print('[nezha-agent-py] NEZHA_SERVER is required, e.g. '
              'NEZHA_SERVER=nezha.example.com:8008', file=sys.stderr)
        sys.exit(1)
    if not cfg['key']:
        print('[nezha-agent-py] NEZHA_KEY (the dashboard client secret) '
              'is required', file=sys.stderr)
        sys.exit(1)
    if cfg['mode'] == 'binary':
        from binary import run_binary
        run_binary(cfg)
    else:
        from nezha.agent import run_pure
        run_pure(cfg)


if __name__ == '__main__':
    main()
