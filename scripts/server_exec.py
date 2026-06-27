from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for deps in reversed((
    PROJECT_ROOT / ".local" / "ssh-deps-clean",
    PROJECT_ROOT / ".local" / "ssh-deps",
    PROJECT_ROOT / "tmp" / "remote-log-deps",
)):
    if deps.exists():
        sys.path.insert(0, str(deps))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a command on the room robot server.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--key")
    parser.add_argument("--bind-address", default=os.environ.get("ROOM_ROBOT_SSH_BIND_ADDRESS", ""))
    args = parser.parse_args()

    password = os.environ.get("ROOM_ROBOT_SSH_PASSWORD")
    try:
        import paramiko
    except Exception as exc:  # pragma: no cover - only used on operator machines.
        print(f"paramiko unavailable: {exc}", file=sys.stderr)
        return 2

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs: dict[str, object] = {
        "hostname": args.host,
        "username": args.user,
        "timeout": 15,
        "banner_timeout": 15,
        "auth_timeout": 15,
    }
    if args.key:
        connect_kwargs["key_filename"] = args.key
    elif password:
        connect_kwargs["password"] = password
    else:
        print("missing ROOM_ROBOT_SSH_PASSWORD or key", file=sys.stderr)
        return 2

    sock = None
    try:
        if args.bind_address:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(15)
            sock.bind((args.bind_address, 0))
            sock.connect((args.host, 22))
            connect_kwargs["sock"] = sock
        client.connect(**connect_kwargs)
        stdin, stdout, stderr = client.exec_command(args.command, get_pty=False)
        del stdin
        out = stdout.read()
        err = stderr.read()
        if out:
            sys.stdout.buffer.write(out)
        if err:
            sys.stderr.buffer.write(err)
        return stdout.channel.recv_exit_status()
    except Exception:
        if sock is not None:
            sock.close()
        raise
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
