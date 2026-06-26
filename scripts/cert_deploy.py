#!/usr/bin/env python3
"""Copy renewed Let's Encrypt certificates into ./certs and reload Nginx."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CERTS_DIR = REPO_ROOT / "certs"
SECRETS_FILE = REPO_ROOT / "secrets" / "client.env"
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

    raise SystemExit(
        "Domain not found. Set PROXY_DOMAIN or run scripts/setup.py first."
    )


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


def reload_nginx() -> None:
    compose_file = REPO_ROOT / "docker-compose.yml"
    if not compose_file.is_file():
        raise SystemExit(f"docker-compose.yml not found at {REPO_ROOT}")

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
        print("Nginx reloaded successfully.")
        return

    print("Nginx reload failed, restarting container...", file=sys.stderr)
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
    if restart.returncode != 0:
        print(restart.stderr or restart.stdout, file=sys.stderr)
        raise SystemExit("Failed to reload or restart Nginx.")


def main() -> None:
    domain = load_domain()
    print(f"Deploying certificates for {domain}")
    copy_certs(domain)
    reload_nginx()


if __name__ == "__main__":
    main()
