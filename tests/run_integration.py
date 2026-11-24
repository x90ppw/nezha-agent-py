"""Local integration test: run the pure-Python agent against a fake Nezha
dashboard (tests/fake_dashboard.js, Node's http2 server) and verify the
whole session: registration with auth metadata, state stream, and the
task stream (Keepalive / TCPPing / HTTPGet all answered successfully).

Requires node (>= 18) on PATH. Exits non-zero on any failure.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRPC_PORT = 18081
HTTP_PORT = 18082


def main():
    tmp = tempfile.mkdtemp(prefix='nezha-integ-')
    result_file = os.path.join(tmp, 'result.json')
    server = subprocess.Popen(
        ['node', os.path.join(ROOT, 'tests', 'fake_dashboard.js'),
         str(GRPC_PORT), str(HTTP_PORT), result_file],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    agent = None
    try:
        time.sleep(1.0)  # let the fake dashboard bind
        env = dict(os.environ)
        env.update({
            'NEZHA_SERVER': '127.0.0.1:%d' % GRPC_PORT,
            'NEZHA_KEY': 'testkey',
            'NEZHA_UUID': 'test-uuid-1234',
            'NEZHA_REPORT_INTERVAL': '1',
        })
        agent = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, 'main.py')],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=tmp)  # cwd elsewhere: keep repo dir clean
        code = server.wait(timeout=70)
        out, _ = server.communicate()
        if code != 0:
            print(out)
            print('INTEGRATION FAILED: dashboard exited %d' % code)
            return 1
    finally:
        if agent is not None:
            agent.terminate()
            try:
                agent_out, _ = agent.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                agent.kill()
                agent_out, _ = agent.communicate()
        if server.poll() is None:
            server.kill()

    with open(result_file) as f:
        summary = json.load(f)
    print(json.dumps(summary, indent=2))
    assert summary['registered'], 'agent never registered'
    assert summary['authOk'], 'auth metadata headers wrong'
    assert summary['stateCount'] >= 2, 'too few state reports'
    for task_id in ('1', '2', '3'):
        r = summary['taskResults'].get(task_id)
        assert r is not None, 'missing result for task %s' % task_id
        assert r.get('successful'), 'task %s failed: %r' % (task_id, r)
    print('--- agent log (tail) ---')
    print('\n'.join(agent_out.splitlines()[-5:]))
    print('INTEGRATION OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
