"""Drop only databases explicitly created by the isolated browser-test harness."""
import json
import re
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from sqlalchemy import create_engine, text
from app.core.config import PROJECT_ROOT, settings


def stop_test_servers():
    """Windows may retain npm grandchildren after Playwright finishes.

    Only stop listeners whose executable and exact test command are verified;
    validate all identities first, before stopping either process.
    """
    if os.name != 'nt':
        return
    node = shutil.which('node')
    if not node:
        raise RuntimeError('Cannot verify the test frontend without node')
    import psutil
    output = subprocess.check_output(['netstat', '-ano', '-p', 'tcp'], text=True)
    verified = {}
    for line in output.splitlines():
        match = re.match(r'^\s*TCP\s+127\.0\.0\.1:(8011|5174)\s+\S+\s+LISTENING\s+(\d+)\s*$', line)
        if not match:
            continue
        port, identity = map(int, match.groups())
        process = psutil.Process(identity)
        if not verified_listener(process, port, node):
            raise RuntimeError(f'Test port {port} identity mismatch; no process stopped')
        verified[identity] = process
    for process in verified.values():
        # psutil checks process creation time to protect against PID reuse.
        if process.is_running():
            process.terminate()
    _, alive = psutil.wait_procs(list(verified.values()), timeout=5)
    if alive:
        raise RuntimeError('Verified test servers did not terminate')


def verified_listener(process, port, node):
    executable = Path(process.exe()).resolve()
    args = process.cmdline()
    if port == 8011:
        return executable in {Path(sys.executable).resolve(), Path(sys._base_executable).resolve()} and args[1:] == ['-m', 'tests.e2e_server']
    if port == 5174:
        return (executable == Path(node).resolve() and len(args) == 4
                and Path(args[1]).resolve() == (PROJECT_ROOT / 'frontend/node_modules/vite/bin/vite.js').resolve()
                and args[2:] == ['--port', '5174'])
    return False



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stop-servers', action='store_true')
    args = parser.parse_args()
    marker = PROJECT_ROOT / 'data/e2e/runtime.json'
    if not marker.exists():
        return
    name = json.loads(marker.read_text(encoding='utf-8'))['database']
    if not re.fullmatch(r'rag_e2e_[0-9a-f]{12}', name) or name == settings.postgres.postgres_db:
        raise ValueError('Refusing to drop a database outside the E2E namespace')
    if args.stop_servers:
        stop_test_servers()
    engine = create_engine(settings.postgres.dsn, isolation_level='AUTOCOMMIT')
    with engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    engine.dispose()
    print(f'E2E database cleaned: {name}')


if __name__ == '__main__':
    main()
