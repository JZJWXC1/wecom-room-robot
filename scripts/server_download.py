from __future__ import annotations

import argparse
import os
import posixpath
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
    return posixpath.join(base.rstrip("/"), relative.replace("\\", "/").lstrip("/"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Download room robot server files into tmp/server_snapshot.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--remote-root", required=True)
    parser.add_argument("--local-root", default="tmp/server_snapshot")
    parser.add_argument("--key")
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()

    password = os.environ.get("ROOM_ROBOT_SSH_PASSWORD")
    try:
        import paramiko
    except Exception as exc:  # pragma: no cover
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

    local_root = PROJECT_ROOT / args.local_root
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(**connect_kwargs)
        with client.open_sftp() as sftp:
            for raw_path in args.paths:
                relative = raw_path.replace("\\", "/")
                remote_path = _remote_join(args.remote_root, relative)
                local_path = local_root / relative
                local_path.parent.mkdir(parents=True, exist_ok=True)
                sftp.get(remote_path, str(local_path))
                print(f"downloaded {remote_path} -> {local_path}")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
