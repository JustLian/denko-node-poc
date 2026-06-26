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
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
XRAY_CONFIG = REPO_ROOT / "xray" / "config.json"
XRAY_PROFILES = REPO_ROOT / "xray" / "profiles"
SECRETS_DIR = REPO_ROOT / "secrets"
SECRETS_FILE = SECRETS_DIR / "client.env"
EGRESS_PEER_FILE = SECRETS_DIR / "egress-peer.env"
CERT_DEPLOY = REPO_ROOT / "scripts" / "cert_deploy.py"
XRAY_IMAGE = "ghcr.io/xtls/xray-core:latest"
ROLES = ("egress", "bridge")
TRANSPORTS = ("tcp", "xhttp")
XHTTP_MODES = ("stream-one", "packet-up", "stream-up", "auto")


@dataclass
class EgressPeer:
    domain: str
    uuid: str
    public_key: str
    short_id: str
    xhttp_path: str
    port: int = 443
    chain_mode: str = "packet-up"


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


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise SystemExit(f"Env file not found: {path}")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_private_key_from_config() -> str:
    if not XRAY_CONFIG.is_file():
        raise SystemExit(f"Cannot reuse secrets: {XRAY_CONFIG} not found.")
    config = json.loads(XRAY_CONFIG.read_text(encoding="utf-8"))
    return config["inbounds"][0]["streamSettings"]["realitySettings"]["privateKey"]


def load_existing_client_secrets() -> dict[str, str]:
    return parse_env_file(SECRETS_FILE)


def parse_egress_peer(args: argparse.Namespace) -> EgressPeer:
    if args.egress_peer_file:
        env = parse_env_file(Path(args.egress_peer_file))
        try:
            return EgressPeer(
                domain=env["EGRESS_DOMAIN"],
                uuid=env["EGRESS_UUID"],
                public_key=env["EGRESS_PUBLIC_KEY"],
                short_id=env["EGRESS_SHORT_ID"],
                xhttp_path=env["EGRESS_XHTTP_PATH"],
                port=int(env.get("EGRESS_PORT", "443")),
                chain_mode=env.get("EGRESS_CHAIN_MODE", "packet-up"),
            )
        except KeyError as exc:
            raise SystemExit(f"Missing key in egress peer file: {exc}") from exc

    fields = {
        "egress-domain": args.egress_domain,
        "egress-uuid": args.egress_uuid,
        "egress-public-key": args.egress_public_key,
        "egress-short-id": args.egress_short_id,
        "egress-xhttp-path": args.egress_xhttp_path,
    }
    missing = [name for name, value in fields.items() if not value]
    if missing:
        raise SystemExit(
            "Bridge role requires egress peer settings: "
            "use --egress-peer-file or provide "
            + ", ".join(f"--{name}" for name in missing)
        )

    return EgressPeer(
        domain=args.egress_domain,
        uuid=args.egress_uuid,
        public_key=args.egress_public_key,
        short_id=args.egress_short_id.lower(),
        xhttp_path=validate_xhttp_path(args.egress_xhttp_path),
        port=args.egress_port,
        chain_mode=args.egress_chain_mode,
    )


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


def load_profile(role: str, transport: str) -> dict:
    if role == "bridge" and transport != "xhttp":
        raise SystemExit("Bridge role only supports xhttp transport.")
    profile_path = XRAY_PROFILES / f"{role}-{transport}.json"
    if not profile_path.is_file():
        raise SystemExit(f"Unknown profile: {profile_path}")
    return json.loads(profile_path.read_text(encoding="utf-8"))


def _client_inbound(config: dict) -> dict:
    for inbound in config["inbounds"]:
        if inbound.get("tag") == "client-in":
            return inbound
    return config["inbounds"][0]


def patch_xray_config(
    role: str,
    transport: str,
    domain: str,
    uuid: str,
    private_key: str,
    short_id: str,
    *,
    xhttp_path: str = "",
    xhttp_mode: str = "stream-one",
    egress_peer: EgressPeer | None = None,
) -> None:
    config = load_profile(role, transport)
    inbound = _client_inbound(config)
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

    if role == "bridge":
        if egress_peer is None:
            raise SystemExit("Internal error: egress peer required for bridge role.")
        egress_out = next(o for o in config["outbounds"] if o.get("tag") == "egress")
        vnext = egress_out["settings"]["vnext"][0]
        vnext["address"] = egress_peer.domain
        vnext["port"] = egress_peer.port
        vnext["users"][0]["id"] = egress_peer.uuid
        egress_out["streamSettings"]["xhttpSettings"]["path"] = egress_peer.xhttp_path
        egress_out["streamSettings"]["xhttpSettings"]["mode"] = egress_peer.chain_mode
        egress_reality = egress_out["streamSettings"]["realitySettings"]
        egress_reality["serverName"] = egress_peer.domain
        egress_reality["publicKey"] = egress_peer.public_key
        egress_reality["shortId"] = egress_peer.short_id

    XRAY_CONFIG.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {XRAY_CONFIG} (role={role}, transport={transport})")


def write_egress_peer_file(
    domain: str,
    uuid: str,
    public_key: str,
    short_id: str,
    xhttp_path: str,
    *,
    port: int = 443,
    chain_mode: str = "packet-up",
) -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        f"EGRESS_DOMAIN={domain}",
        f"EGRESS_PORT={port}",
        f"EGRESS_UUID={uuid}",
        f"EGRESS_PUBLIC_KEY={public_key}",
        f"EGRESS_SHORT_ID={short_id}",
        f"EGRESS_XHTTP_PATH={xhttp_path}",
        f"EGRESS_CHAIN_MODE={chain_mode}",
    ]
    EGRESS_PEER_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(EGRESS_PEER_FILE, 0o600)
    print(f"Wrote {EGRESS_PEER_FILE}")


def write_secrets(
    role: str,
    transport: str,
    domain: str,
    uuid: str,
    public_key: str,
    short_id: str,
    *,
    xhttp_path: str = "",
    xhttp_mode: str = "stream-one",
    egress_peer: EgressPeer | None = None,
) -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        f"ROLE={role}",
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
    if role == "bridge" and egress_peer is not None:
        lines.extend(
            [
                f"EGRESS_DOMAIN={egress_peer.domain}",
                f"EGRESS_PORT={egress_peer.port}",
                f"EGRESS_UUID={egress_peer.uuid}",
                f"EGRESS_PUBLIC_KEY={egress_peer.public_key}",
                f"EGRESS_SHORT_ID={egress_peer.short_id}",
                f"EGRESS_XHTTP_PATH={egress_peer.xhttp_path}",
                f"EGRESS_CHAIN_MODE={egress_peer.chain_mode}",
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
        "--role",
        choices=ROLES,
        default="egress",
        help="Server role: egress (default) or bridge (RU split-routing hop)",
    )
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
        help="xHTTP mode for client inbound; stream-one recommended for direct Reality",
    )
    parser.add_argument(
        "--keep-secrets",
        action="store_true",
        help="Reuse UUID/keys/shortId/xhttp_path from secrets/client.env if present",
    )
    parser.add_argument(
        "--egress-peer-file",
        help="Path to egress-peer.env from egress setup (required for bridge unless CLI flags set)",
    )
    parser.add_argument("--egress-domain", help="Egress server domain (bridge role)")
    parser.add_argument("--egress-uuid", help="Egress VLESS UUID (bridge role)")
    parser.add_argument("--egress-public-key", help="Egress Reality public key (bridge role)")
    parser.add_argument("--egress-short-id", help="Egress Reality short ID (bridge role)")
    parser.add_argument("--egress-xhttp-path", help="Egress xHTTP path (bridge role)")
    parser.add_argument(
        "--egress-port",
        type=int,
        default=443,
        help="Egress server port (default: 443)",
    )
    parser.add_argument(
        "--egress-chain-mode",
        choices=XHTTP_MODES,
        default="packet-up",
        help="xHTTP mode on bridge→egress hop (default: packet-up)",
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
    role = args.role
    transport = args.transport

    if role == "bridge" and transport != "xhttp":
        raise SystemExit("Bridge role only supports --transport xhttp.")

    domain = prompt(args.domain, "Domain name")
    email = prompt(args.email, "Let's Encrypt email")
    validate_dns(domain)

    egress_peer: EgressPeer | None = None
    if role == "bridge":
        egress_peer = parse_egress_peer(args)
        print(f"Egress peer: {egress_peer.domain}:{egress_peer.port} ({egress_peer.chain_mode})")

    xhttp_path = ""
    xhttp_mode = args.xhttp_mode

    if args.keep_secrets and SECRETS_FILE.is_file():
        existing = load_existing_client_secrets()
        print(f"Reusing secrets from {SECRETS_FILE}")
        uuid = existing["VLESS_UUID"]
        public_key = existing["REALITY_PUBLIC_KEY"]
        short_id = existing["SHORT_ID"].lower()
        private_key = load_private_key_from_config()
        if transport == "xhttp":
            xhttp_path = validate_xhttp_path(
                args.xhttp_path or existing.get("XHTTP_PATH") or generate_xhttp_path()
            )
            xhttp_mode = existing.get("XHTTP_MODE", args.xhttp_mode)
        print(f"UUID: {uuid}")
        print(f"Public key (client pbk): {public_key}")
        print(f"Short ID: {short_id}")
    else:
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

        if transport == "xhttp":
            xhttp_path = validate_xhttp_path(args.xhttp_path or generate_xhttp_path())
            print(f"xHTTP path: {xhttp_path}")
            print(f"xHTTP mode: {xhttp_mode}")

    if transport == "xhttp" and not xhttp_path:
        xhttp_path = validate_xhttp_path(args.xhttp_path or generate_xhttp_path())
        print(f"xHTTP path: {xhttp_path}")
        print(f"xHTTP mode: {xhttp_mode}")

    patch_xray_config(
        role,
        transport,
        domain,
        uuid,
        private_key,
        short_id,
        xhttp_path=xhttp_path,
        xhttp_mode=xhttp_mode,
        egress_peer=egress_peer,
    )
    write_secrets(
        role,
        transport,
        domain,
        uuid,
        public_key,
        short_id,
        xhttp_path=xhttp_path,
        xhttp_mode=xhttp_mode,
        egress_peer=egress_peer,
    )

    if role == "egress" and transport == "xhttp":
        write_egress_peer_file(
            domain,
            uuid,
            public_key,
            short_id,
            xhttp_path,
            port=443,
            chain_mode="packet-up",
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
    print(f"Setup complete (role={role}, transport={transport}). Import this VLESS URI:\n")
    print(uri)
    if role == "bridge":
        print("\nClients connect to the bridge domain above. Non-RU traffic exits via egress.")
    if role == "egress" and transport == "xhttp":
        print(f"\nCopy {EGRESS_PEER_FILE} to the bridge machine for bridge setup.")
    print("\nSee README.md, docs/transports.md, and docs/multi-hop.md.")
    print("=" * 60)


if __name__ == "__main__":
    main()
