#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["paramiko"]
# ///
"""
MikroTik SSH command tool.
Usage: uv run mikrotik.py <command>
       uv run mikrotik.py --interactive

Config via env vars or .env file:
  MIKROTIK_HOST      - IP address (default: 192.168.88.1)
  MIKROTIK_USER      - username (default: admin)
  MIKROTIK_PASSWORD  - password
  MIKROTIK_PORT      - SSH port (default: 22)
"""

import os
import sys
import argparse
import paramiko
from pathlib import Path

ENV_FILE = Path(__file__).parent / ".env"


def load_env():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def get_config():
    load_env()
    return {
        "host": os.environ.get("MIKROTIK_HOST", "192.168.88.1"),
        "port": int(os.environ.get("MIKROTIK_PORT", "22")),
        "username": os.environ.get("MIKROTIK_USER", "admin"),
        "password": os.environ.get("MIKROTIK_PASSWORD", ""),
    }


def connect(cfg):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=cfg["host"],
        port=cfg["port"],
        username=cfg["username"],
        password=cfg["password"],
        look_for_keys=False,
        allow_agent=False,
        timeout=10,
    )
    return client


def run_command(client, command):
    stdin, stdout, stderr = client.exec_command(command)
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    if err:
        return f"{out}\n[stderr] {err}".strip()
    return out


def interactive_mode(cfg):
    print(f"Connecting to {cfg['host']}:{cfg['port']} as {cfg['username']}...")
    client = connect(cfg)
    print("Connected. Type 'quit' or Ctrl-C to exit.\n")
    try:
        while True:
            try:
                cmd = input("[mikrotik]> ").strip()
            except EOFError:
                break
            if cmd.lower() in ("quit", "exit"):
                break
            if not cmd:
                continue
            print(run_command(client, cmd))
    except KeyboardInterrupt:
        pass
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description="MikroTik SSH command tool")
    parser.add_argument("command", nargs="*", help="RouterOS command to run")
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive shell mode")
    parser.add_argument("--host", help="Override MIKROTIK_HOST")
    parser.add_argument("--user", help="Override MIKROTIK_USER")
    parser.add_argument("--password", help="Override MIKROTIK_PASSWORD")
    args = parser.parse_args()

    cfg = get_config()
    if args.host:
        cfg["host"] = args.host
    if args.user:
        cfg["username"] = args.user
    if args.password:
        cfg["password"] = args.password

    if args.interactive:
        interactive_mode(cfg)
        return

    if not args.command:
        parser.print_help()
        sys.exit(1)

    command = " ".join(args.command)
    try:
        client = connect(cfg)
        print(run_command(client, command))
        client.close()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
