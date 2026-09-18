#!/usr/bin/env python3
"""
TCP port relay for macOS Docker Desktop + VPN development.

Docker Desktop runs containers in a Linux VM that cannot access the host's
VPN interfaces. This relay listens on a host port and forwards traffic to
the VPN-internal model gateway, so containers can reach it via
host-gateway (set by TALOS_HOST_FORWARD in the executor).

Usage:
    python3 port-relay.py --listen 0.0.0.0:9443 --target <host>:<port>
    python3 port-relay.py          # reads from TALOS_HOST_FORWARD in env

Never put internal hostnames or IPs in this source file — read them from
args or environment variables only.

This is a development-only tool. Server deployments do not need it.
"""

import argparse
import os
import socket
import sys
import threading


def parse_host_forward(spec: str) -> list[tuple[str, int, str, int]]:
    """Parse TALOS_HOST_FORWARD entries.

    Format: hostname:host.docker.internal:port
    We need hostname and port; host.docker.internal is a fixed token
    meaning "relay on the Docker host".

    Returns list of (listen_addr, listen_port, target_host, target_port)
    where target_host:target_port is the real endpoint to forward to.
    For each entry, we resolve hostname to its real IP and forward to
    that IP on the same port (the container config uses the same port).
    """
    entries: list[tuple[str, int, str, int]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        tokens = part.split(":")
        if len(tokens) != 3:
            continue
        hostname = tokens[0]
        # tokens[1] is "host.docker.internal" — we listen on all interfaces
        port_str = tokens[2]
        try:
            port = int(port_str)
        except ValueError:
            continue
        # Resolve hostname to real IP for forwarding target
        try:
            resolved = socket.gethostbyname(hostname)
        except socket.gaierror:
            print(f"Cannot resolve {hostname}, skipping", file=sys.stderr)
            continue
        entries.append(("0.0.0.0", port, resolved, port))
    return entries


def forward(src: socket.socket, dst: socket.socket) -> None:
    """Bidirectionally forward data between two sockets."""
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        try:
            src.close()
        except Exception:
            pass
        try:
            dst.close()
        except Exception:
            pass


def run_relay(listen_addr: str, listen_port: int,
              target_host: str, target_port: int) -> None:
    """Run a single TCP relay."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((listen_addr, listen_port))
    server.listen(16)
    print(
        f"port-relay: {listen_addr}:{listen_port} -> "
        f"{target_host}:{target_port}",
        flush=True,
    )
    while True:
        client, addr = server.accept()
        remote = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        remote.settimeout(10)
        try:
            remote.connect((target_host, target_port))
            remote.settimeout(None)
            threading.Thread(
                target=forward, args=(client, remote), daemon=True
            ).start()
            threading.Thread(
                target=forward, args=(remote, client), daemon=True
            ).start()
        except Exception as exc:
            print(
                f"port-relay: connect to {target_host}:{target_port} "
                f"failed: {exc}",
                file=sys.stderr,
                flush=True,
            )
            client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TCP port relay for macOS Docker Desktop + VPN development."
    )
    parser.add_argument(
        "--listen",
        help="Listen address and port, e.g. 0.0.0.0:9443",
    )
    parser.add_argument(
        "--target",
        help="Target host and port, e.g. example.com:9443",
    )
    args = parser.parse_args()

    entries: list[tuple[str, int, str, int]] = []

    if args.listen and args.target:
        la, lp = args.listen.rsplit(":", 1)
        th, tp = args.target.rsplit(":", 1)
        entries.append((la, int(lp), th, int(tp)))
    else:
        spec = os.environ.get("TALOS_HOST_FORWARD", "")
        if not spec:
            print(
                "No --listen/--target given and TALOS_HOST_FORWARD not set.",
                file=sys.stderr,
            )
            sys.exit(1)
        for listen_addr, listen_port, target_host, target_port in (
            parse_host_forward(spec)
        ):
            entries.append((listen_addr, listen_port, target_host, target_port))

    if not entries:
        print("No relay entries to start.", file=sys.stderr)
        sys.exit(1)

    threads: list[threading.Thread] = []
    for listen_addr, listen_port, target_host, target_port in entries:
        t = threading.Thread(
            target=run_relay,
            args=(listen_addr, listen_port, target_host, target_port),
            daemon=True,
        )
        t.start()
        threads.append(t)

    # Keep main thread alive
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
