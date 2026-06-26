#!/usr/bin/env python3
"""Bootstrap a VLESS+Reality Self-Stealth proxy deployment."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
XRAY_CONFIG = REPO_ROOT / "xray" / "config.json"
XRAY_PROFILES = REPO_ROOT / "xray" / "profiles"
SECRETS_DIR = REPO_ROOT / "secrets"
SECRETS_FILE = SECRETS_DIR / "client.env"
CERT_DEPLOY = REPO_ROOT / "scripts" / "cert_deploy.py"
XRAY_IMAGE = "ghcr.io/xtls/xray-core:latest"
TRANSPORTS = ("tcp", "xhttp")
XHTTP_MODES = ("stream-one", "packet-up", "stream-up", "auto")


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )
    if check and result.returncode != 0:
        if result.stdout:
            print(result.stdout, file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        raise SystemExit(result.returncode)
    return result


def docker_xray(args: list[str]) -> str:
    result = run(["docker", "run", "--rm", XRAY_IMAGE] + args)
    return result.stdout.strip()


def generate_uuid() -> str:
    return docker_xray(["uuid"])


def generate_x25519() -> tuple[str, str]:
    output = docker_xray(["x25519"])
    private_key = ""
    public_key = ""
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("PrivateKey:"):
            private_key = line.split(":", 1)[1].strip()
        elif line.startswith("Password"):
            # "Password:" (legacy) or "Password (PublicKey):" (current xray-core)
            public_key = line.split(":", 1)[1].strip()
        elif line.startswith("Public key:") or line.startswith("PublicKey:"):
            public_key = line.split(":", 1)[1].strip()
    if not private_key or not public_key:
        raise SystemExit(f"Could not parse x25519 output:\n{output}")
    return private_key, public_key


def generate_short_id(length: int = 8) -> str:
    return secrets.token_hex(length // 2)


def generate_xhttp_path() -> str:
    return f"/api/v1/{secrets.token_hex(4)}"


def validate_xhttp_path(path: str) -> str:
    if not path.startswith("/") or " " in path:
        raise SystemExit("xHTTP path must start with '/' and contain no spaces.")
    return path


def get_public_ip() -> str | None:
    for host in ("ifconfig.me", "api.ipify.org", "icanhazip.com"):
        try:
            with socket.create_connection((host, 80), timeout=5) as sock:
                sock.sendall(f"GET / HTTP/1.0\r\nHost: {host}\r\n\r\n".encode())
                response = sock.recv(4096).decode(errors="ignore")
            body = response.split("\r\n\r\n", 1)[-1].strip()
            if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", body):
                return body
        except OSError:
            continue
    return None


def resolve_domain(domain: str) -> set[str]:
    try:
        infos = socket.getaddrinfo(domain, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return set()
    return {info[4][0] for info in infos if info[4]}


def validate_dns(domain: str) -> None:
    resolved = resolve_domain(domain)
    if not resolved:
        print(f"Warning: DNS lookup failed for {domain}. Ensure an A record points to this VPS.")
        return

    public_ip = get_public_ip()
    if public_ip and public_ip not in resolved:
        print(
            f"Warning: {domain} resolves to {', '.join(sorted(resolved))}, "
            f"but this host appears to be {public_ip}."
        )
    else:
        print(f"DNS OK: {domain} -> {', '.join(sorted(resolved))}")


def load_profile(transport: str) -> dict:
    profile_path = XRAY_PROFILES / f"{transport}.json"
    if not profile_path.is_file():
        raise SystemExit(f"Unknown transport profile: {profile_path}")
    return json.loads(profile_path.read_text(encoding="utf-8"))


def patch_xray_config(
    transport: str,
    domain: str,
    uuid: str,
    private_key: str,
    short_id: str,
    *,
    xhttp_path: str = "",
    xhttp_mode: str = "stream-one",
) -> None:
    config = load_profile(transport)
    inbound = config["inbounds"][0]
    inbound["settings"]["clients"][0]["id"] = uuid
    if transport == "tcp":
        inbound["settings"]["clients"][0]["flow"] = "xtls-rprx-vision"
    elif "flow" in inbound["settings"]["clients"][0]:
        del inbound["settings"]["clients"][0]["flow"]

    if transport == "xhttp":
        inbound["streamSettings"]["xhttpSettings"]["path"] = xhttp_path
        inbound["streamSettings"]["xhttpSettings"]["mode"] = xhttp_mode

    reality = inbound["streamSettings"]["realitySettings"]
    reality["serverNames"] = [domain]
    reality["privateKey"] = private_key
    reality["shortIds"] = [short_id]
    XRAY_CONFIG.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {XRAY_CONFIG} (transport={transport})")


def write_secrets(
    transport: str,
    domain: str,
    uuid: str,
    public_key: str,
    short_id: str,
    *,
    xhttp_path: str = "",
    xhttp_mode: str = "stream-one",
) -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        f"TRANSPORT={transport}",
        f"DOMAIN={domain}",
        f"VLESS_UUID={uuid}",
        f"REALITY_PUBLIC_KEY={public_key}",
        f"SHORT_ID={short_id}",
    ]
    if transport == "xhttp":
        lines.extend(
            [
                f"XHTTP_PATH={xhttp_path}",
                f"XHTTP_MODE={xhttp_mode}",
            ]
        )
    SECRETS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(SECRETS_FILE, 0o600)
    print(f"Wrote {SECRETS_FILE}")


def build_vless_uri(
    transport: str,
    domain: str,
    uuid: str,
    public_key: str,
    short_id: str,
    *,
    xhttp_path: str = "",
    xhttp_mode: str = "stream-one",
) -> str:
    params: dict[str, str] = {
        "encryption": "none",
        "security": "reality",
        "sni": domain,
        "fp": "chrome",
        "pbk": public_key,
        "sid": short_id,
    }
    if transport == "tcp":
        params["flow"] = "xtls-rprx-vision"
        params["type"] = "tcp"
        label = "Self-Stealth-TCP"
    else:
        params["type"] = "xhttp"
        params["path"] = xhttp_path
        params["mode"] = xhttp_mode
        label = "Self-Stealth-xHTTP"
    query = urllib.parse.urlencode(params)
    return f"vless://{uuid}@{domain}:443?{query}#{label}"


def issue_certificate(domain: str, email: str, skip_cert: bool) -> None:
    if skip_cert:
        print("Skipping certificate issuance (--skip-cert).")
        return

    cert_path = Path(f"/etc/letsencrypt/live/{domain}/fullchain.pem")
    if cert_path.is_file():
        print(f"Certificate already exists at {cert_path}, deploying to ./certs/")
        run(
            [sys.executable, str(CERT_DEPLOY)],
            env={**os.environ, "PROXY_DOMAIN": domain},
        )
        return

    cmd = [
        "certbot",
        "certonly",
        "--standalone",
        "--preferred-challenges",
        "http",
        "-d",
        domain,
        "--non-interactive",
        "--agree-tos",
        "-m",
        email,
    ]
    run(cmd)
    run(
        [sys.executable, str(CERT_DEPLOY)],
        env={**os.environ, "PROXY_DOMAIN": domain},
    )


def start_compose() -> None:
    run(["docker", "compose", "up", "-d"])


def install_cron(domain: str) -> None:
    cron_line = (
        f"0 3 * * * root certbot renew --quiet "
        f'--deploy-hook "{sys.executable} {CERT_DEPLOY}"'
    )
    cron_path = Path("/etc/cron.d/certbot-proxy-renew")
    cron_path.write_text(
        "\n".join(
            [
                "# Renew Let's Encrypt certificates and reload Nginx for the proxy stack",
                "SHELL=/bin/bash",
                "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                f"PROXY_DOMAIN={domain}",
                cron_line,
                "",
            ]
        ),
        encoding="utf-8",
    )
    cron_path.chmod(0o644)
    print(f"Installed cron job at {cron_path}")


def install_renewal_hook(domain: str) -> None:
    renewal_conf = Path(f"/etc/letsencrypt/renewal/{domain}.conf")
    if not renewal_conf.is_file():
        print(f"Renewal config not found at {renewal_conf}, skipping hook install.")
        return

    hook_line = f"deploy_hook = {sys.executable} {CERT_DEPLOY}\n"
    content = renewal_conf.read_text(encoding="utf-8")
    if "deploy_hook" in content:
        lines = []
        for line in content.splitlines():
            if line.strip().startswith("deploy_hook"):
                lines.append(hook_line.strip())
            else:
                lines.append(line)
        content = "\n".join(lines) + "\n"
    else:
        content = content.rstrip() + "\n" + hook_line

    renewal_conf.write_text(content, encoding="utf-8")
    print(f"Installed deploy_hook in {renewal_conf}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap VLESS+Reality Self-Stealth proxy")
    parser.add_argument("--domain", help="Your domain (A record must point to this VPS)")
    parser.add_argument("--email", help="Email for Let's Encrypt registration")
    parser.add_argument("--short-id", help="Reality short ID (8 hex chars); generated if omitted")
    parser.add_argument(
        "--transport",
        choices=TRANSPORTS,
        default="tcp",
        help="Transport profile: tcp (Vision) or xhttp (TSPU-oriented, no Vision)",
    )
    parser.add_argument(
        "--xhttp-path",
        help="xHTTP path (xhttp transport only); random /api/v1/<hex> if omitted",
    )
    parser.add_argument(
        "--xhttp-mode",
        choices=XHTTP_MODES,
        default="stream-one",
        help="xHTTP mode; stream-one recommended for direct Reality (default)",
    )
    parser.add_argument("--skip-cert", action="store_true", help="Skip certbot certificate issuance")
    parser.add_argument("--skip-compose", action="store_true", help="Skip docker compose up -d")
    parser.add_argument("--install-cron", action="store_true", help="Install /etc/cron.d/certbot-proxy-renew")
    parser.add_argument(
        "--install-renewal-hook",
        action="store_true",
        help="Add deploy_hook to certbot renewal config",
    )
    return parser.parse_args()


def prompt(value: str | None, message: str) -> str:
    if value:
        return value.strip()
    try:
        answer = input(f"{message}: ").strip()
    except EOFError:
        raise SystemExit("Input required.") from None
    if not answer:
        raise SystemExit(f"{message} is required.")
    return answer


def main() -> None:
    args = parse_args()

    domain = prompt(args.domain, "Domain name")
    email = prompt(args.email, "Let's Encrypt email")

    validate_dns(domain)

    print("Generating VLESS UUID...")
    uuid = generate_uuid()
    print(f"UUID: {uuid}")

    print("Generating Reality X25519 key pair...")
    private_key, public_key = generate_x25519()
    print(f"Private key (server): {private_key}")
    print(f"Public key (client pbk): {public_key}")

    short_id = args.short_id or generate_short_id()
    if not re.fullmatch(r"[0-9a-fA-F]{8}", short_id):
        raise SystemExit("Short ID must be exactly 8 hexadecimal characters.")
    short_id = short_id.lower()
    print(f"Short ID: {short_id}")

    transport = args.transport
    xhttp_path = ""
    xhttp_mode = args.xhttp_mode
    if transport == "xhttp":
        xhttp_path = validate_xhttp_path(args.xhttp_path or generate_xhttp_path())
        print(f"xHTTP path: {xhttp_path}")
        print(f"xHTTP mode: {xhttp_mode}")

    patch_xray_config(
        transport,
        domain,
        uuid,
        private_key,
        short_id,
        xhttp_path=xhttp_path,
        xhttp_mode=xhttp_mode,
    )
    write_secrets(
        transport,
        domain,
        uuid,
        public_key,
        short_id,
        xhttp_path=xhttp_path,
        xhttp_mode=xhttp_mode,
    )

    issue_certificate(domain, email, args.skip_cert)

    if not args.skip_compose:
        start_compose()

    if args.install_cron:
        install_cron(domain)

    if args.install_renewal_hook:
        install_renewal_hook(domain)

    uri = build_vless_uri(
        transport,
        domain,
        uuid,
        public_key,
        short_id,
        xhttp_path=xhttp_path,
        xhttp_mode=xhttp_mode,
    )
    print("\n" + "=" * 60)
    print(f"Setup complete (transport={transport}). Import this VLESS URI:\n")
    print(uri)
    print("\nSee README.md and docs/transports.md for client configuration.")
    print("=" * 60)


if __name__ == "__main__":
    main()
