#!/usr/bin/env python3
"""Copy renewed Let's Encrypt certificates into ./certs and reload Nginx."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CERTS_DIR = REPO_ROOT / "certs"
SECRETS_FILE = REPO_ROOT / "secrets" / "client.env"
HYSTERIA_SECRETS = REPO_ROOT / "secrets" / "hysteria.env"
NGINX_NATIVE_CONF = REPO_ROOT / "nginx" / "nginx.native.conf"
LETSENCRYPT_LIVE = Path("/etc/letsencrypt/live")


def load_domain() -> str:
    domain = os.environ.get("PROXY_DOMAIN", "").strip()
    if domain:
        return domain

    if SECRETS_FILE.is_file():
        for line in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DOMAIN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")

    if HYSTERIA_SECRETS.is_file():
        for line in HYSTERIA_SECRETS.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DOMAIN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")

    raise SystemExit(
        "Domain not found. Set PROXY_DOMAIN or run scripts/setup.py first."
    )


def is_native_mode(explicit: bool | None = None) -> bool:
    if explicit is not None:
        return explicit
    env_flag = os.environ.get("PROXY_NATIVE", "").strip().lower()
    if env_flag in ("1", "true", "yes"):
        return True
    if SECRETS_FILE.is_file():
        for line in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line == "NATIVE=1":
                return True
    return NGINX_NATIVE_CONF.is_file()


def copy_certs(domain: str) -> None:
    source_dir = LETSENCRYPT_LIVE / domain
    if not source_dir.is_dir():
        raise SystemExit(f"Let's Encrypt directory not found: {source_dir}")

    CERTS_DIR.mkdir(parents=True, exist_ok=True)

    for name in ("fullchain.pem", "privkey.pem"):
        src = source_dir / name
        dst = CERTS_DIR / name
        if not src.is_file():
            raise SystemExit(f"Missing certificate file: {src}")
        shutil.copy2(src, dst)
        os.chmod(dst, 0o600 if name == "privkey.pem" else 0o644)
        print(f"Copied {src} -> {dst}")


def reload_nginx_docker() -> bool:
    compose_file = REPO_ROOT / "docker-compose.yml"
    if not compose_file.is_file():
        return False

    reload_cmd = [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "exec",
        "-T",
        "nginx",
        "nginx",
        "-s",
        "reload",
    ]
    result = subprocess.run(reload_cmd, cwd=REPO_ROOT, capture_output=True, text=True)

    if result.returncode == 0:
        print("Docker Nginx reloaded successfully.")
        return True

    print("Docker Nginx reload failed, restarting container...", file=sys.stderr)
    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr)

    restart_cmd = [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "restart",
        "nginx",
    ]
    restart = subprocess.run(restart_cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    if restart.returncode == 0:
        print("Docker Nginx restarted successfully.")
        return True

    if restart.stderr or restart.stdout:
        print(restart.stderr or restart.stdout, file=sys.stderr)
    return False


def nginx_native_running() -> bool:
    pid_file = Path("/run/nginx-denko.pid")
    if pid_file.is_file():
        return True
    active = subprocess.run(
        ["systemctl", "is-active", "--quiet", "denko-nginx"],
        capture_output=True,
    )
    return active.returncode == 0


def reload_nginx_native(*, allow_not_running: bool = False) -> bool:
    if NGINX_NATIVE_CONF.is_file():
        test = subprocess.run(
            ["nginx", "-t", "-c", str(NGINX_NATIVE_CONF)],
            capture_output=True,
            text=True,
        )
        if test.returncode != 0:
            print(test.stderr or test.stdout, file=sys.stderr)
            return False

    if not nginx_native_running():
        if allow_not_running:
            print(
                "Native Nginx is not running yet — certs copied to ./certs/. "
                "Start it with: sudo systemctl start denko-nginx"
            )
            return True
        print("Native Nginx is not running.", file=sys.stderr)
        return False

    for cmd in (
        ["systemctl", "reload", "denko-nginx"],
        ["systemctl", "reload", "nginx"],
    ):
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"Nginx reloaded via {' '.join(cmd)}.")
            return True

    if NGINX_NATIVE_CONF.is_file():
        reload = subprocess.run(
            [
                "nginx",
                "-s",
                "reload",
                "-c",
                str(NGINX_NATIVE_CONF),
                "-g",
                "pid /run/nginx-denko.pid;",
            ],
            capture_output=True,
            text=True,
        )
        if reload.returncode == 0:
            print("Native Nginx reloaded successfully.")
            return True
        print(reload.stderr or reload.stdout, file=sys.stderr)

    return False


def reload_nginx(*, native: bool | None = None) -> None:
    use_native = is_native_mode(native)

    if use_native:
        if reload_nginx_native(allow_not_running=True):
            return
        raise SystemExit("Failed to reload native Nginx.")

    if reload_nginx_docker():
        return

    print("Docker Nginx unavailable; trying native reload...", file=sys.stderr)
    if reload_nginx_native():
        return

    raise SystemExit("Failed to reload Nginx (docker and native both failed).")


def reload_hysteria(*, native: bool | None = None) -> None:
    if not HYSTERIA_SECRETS.is_file():
        return

    use_native = is_native_mode(native)
    if use_native:
        result = subprocess.run(
            ["systemctl", "try-reload-or-restart", "denko-hysteria"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print("Hysteria reloaded via systemctl.")
            return
        active = subprocess.run(
            ["systemctl", "is-active", "--quiet", "denko-hysteria"],
            capture_output=True,
        )
        if active.returncode != 0:
            print("denko-hysteria is not running — certs copied to ./certs/.")
            return
        print(result.stderr or result.stdout, file=sys.stderr)
        return

    hysteria_compose = REPO_ROOT / "docker-compose.hysteria.yml"
    hybrid_compose = REPO_ROOT / "docker-compose.hybrid.yml"
    compose_files = []
    if hybrid_compose.is_file() and (REPO_ROOT / "nginx" / "nginx-hybrid.conf").is_file():
        compose_files = ["-f", "docker-compose.yml", "-f", "docker-compose.hybrid.yml"]
    elif hysteria_compose.is_file():
        compose_files = ["-f", str(hysteria_compose.name)]

    if not compose_files:
        return

    restart = subprocess.run(
        ["docker", "compose", *compose_files, "restart", "hysteria"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if restart.returncode == 0:
        print("Docker Hysteria restarted successfully.")
    elif restart.stderr or restart.stdout:
        print(restart.stderr or restart.stdout, file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deploy LE certs and reload Nginx")
    parser.add_argument(
        "--native",
        action="store_true",
        help="Reload host nginx (systemd / nginx.native.conf) instead of Docker",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    domain = load_domain()
    print(f"Deploying certificates for {domain}")
    copy_certs(domain)
    reload_nginx(native=True if args.native else None)
    reload_hysteria(native=True if args.native else None)


if __name__ == "__main__":
    main()
