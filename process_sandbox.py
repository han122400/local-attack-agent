"""Docker invocation for generated code; never fall back to execution on the host."""

import os
import re
import shutil
import subprocess
from pathlib import Path

from session_log import validate_run_id


IMAGE = 'local-attack-agent-runtime:v3'


def sandbox_command(name: str, run_id: str, runtime: Path, argv: list[str]) -> list[str]:
    validate_run_id(run_id)
    if not re.fullmatch(r'agent-' + run_id + r'-[a-z0-9-]+', name):
        raise ValueError('Invalid sandbox container name')
    docker = shutil.which('docker')
    if not docker:
        raise RuntimeError('Docker sandbox unavailable; host execution is disabled')
    root = str(runtime.resolve())
    if ',' in root:
        raise ValueError('Runtime path cannot contain a comma')
    try:
        subprocess.run([docker, 'image', 'inspect', IMAGE], check=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError('Docker sandbox unavailable. Build it with: docker build -t ' + IMAGE + ' - < Dockerfile.runtime') from exc
    command = [docker, 'create', '--rm', '-i', '--name', name, '--label', 'local-attack-agent.run=' + run_id,
               '--network', 'none', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
               '--read-only', '--pids-limit', '256', '--memory', '2g', '--cpus', '2',
               '--tmpfs', '/tmp:rw,nosuid,size=256m', '--mount', f'type=bind,src={root},dst=/work',
               '--workdir', '/work', '--env', 'HOME=/tmp', '--env', 'TMPDIR=/tmp']
    if os.name != 'nt':
        command += ['--user', f'{os.getuid()}:{os.getgid()}']
    return [*command, IMAGE, *argv]


def remove_container(name: str, run_id: str) -> None:
    validate_run_id(run_id)
    if not re.fullmatch(r'agent-' + run_id + r'-[a-z0-9-]+', name):
        raise ValueError('Invalid sandbox container name')
    docker = shutil.which('docker')
    if not docker:
        raise RuntimeError('Docker is required to clean up sandbox containers')
    # Never remove an unrelated container with the same name.
    result = subprocess.run([docker, 'ps', '-aq', '--filter', 'name=^/' + name + '$',
                             '--filter', 'label=local-attack-agent.run=' + run_id],
                            capture_output=True, text=True, check=True, timeout=10)
    if result.stdout.strip():
        subprocess.run([docker, 'rm', '-f', name], check=True, capture_output=True, timeout=10)
