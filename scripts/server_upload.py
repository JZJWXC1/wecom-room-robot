from __future__ import annotations

import argparse
import os
import posixpath
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


def _remote_join(base: str, relative: str) -> str:
    clean_relative = relative.replace("\\", "/").lstrip("/")
    return posixpath.join(base.rstrip("/"), clean_relative)


def _mkdir_p(sftp, remote_dir: str) -> None:
    parts = [part for part in remote_dir.split("/") if part]
    current = "/" if remote_dir.startswith("/") else ""
    for part in parts:
        current = posixpath.join(current, part) if current else part
        try:
            sftp.stat(current)
        except OSError:
            sftp.mkdir(current)


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload local project files to the room robot server.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--remote-root", required=True)
    parser.add_argument("--key")
    parser.add_argument("--bind-address", default=os.environ.get("ROOM_ROBOT_SSH_BIND_ADDRESS", ""))
    parser.add_argument("paths", nargs="+", help="Project-relative file paths to upload.")
    args = parser.parse_args()

    password = os.environ.get("ROOM_ROBOT_SSH_PASSWORD")
    try:
        import paramiko
    except Exception as exc:  # pragma: no cover - only used on operator machines.
        print(f"paramiko unavailable: {exc}", file=sys.stderr)
        return 2

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

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    sock = None
    try:
        if args.bind_address:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(15)
            sock.bind((args.bind_address, 0))
            sock.connect((args.host, 22))
            connect_kwargs["sock"] = sock
        client.connect(**connect_kwargs)
        with client.open_sftp() as sftp:
            for raw_path in args.paths:
                if "=>" in raw_path:
                    local_raw, remote_raw = raw_path.split("=>", 1)
                    local_relative = local_raw.strip().replace("\\", "/")
                    remote_relative = remote_raw.strip().replace("\\", "/")
                else:
                    local_relative = raw_path.replace("\\", "/")
                    remote_relative = local_relative
                local_path = PROJECT_ROOT / local_relative
                if not local_path.is_file():
                    print(f"skip missing file: {local_relative}", file=sys.stderr)
                    return 1
                remote_path = _remote_join(args.remote_root, remote_relative)
                _mkdir_p(sftp, posixpath.dirname(remote_path))
                sftp.put(str(local_path), remote_path)
                print(f"uploaded {local_relative} -> {remote_path}")
        return 0
    except Exception:
        if sock is not None:
            sock.close()
        raise
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
