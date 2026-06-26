#!/usr/bin/env python3
"""Bootstrap a VLESS+Reality Self-Stealth proxy deployment."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
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
INSTALL_NATIVE_SCRIPT = REPO_ROOT / "scripts" / "install-native.sh"
SYSTEMD_DIR = REPO_ROOT / "scripts" / "systemd"
NGINX_NATIVE_CONF = REPO_ROOT / "nginx" / "nginx.native.conf"
XRAY_IMAGE = "ghcr.io/xtls/xray-core:latest"
ROLES = ("egress", "bridge", "entry")
STACKS = ("xray", "hysteria", "hybrid")
TRANSPORTS = ("tcp", "xhttp", "grpc")
XHTTP_MODES = ("stream-one", "packet-up", "stream-up", "auto")
CHAIN_ROLES = frozenset({"bridge", "entry"})
PROVIDER_REALITY_SNI = "tesla.com"
PROVIDER_REALITY_DEST = "www.tesla.com:443"
PROVIDER_FINGERPRINT = "qq"
PROVIDER_GRPC_SERVICE = "grpc"
PROVIDER_LISTEN_PORT = 6437
UTLS_FINGERPRINTS = (
    "chrome",
    "firefox",
    "safari",
    "ios",
    "android",
    "edge",
    "qq",
    "random",
    "randomized",
)
HYSTERIA_CONFIG = REPO_ROOT / "hysteria" / "config.yaml"
HYSTERIA_PROFILE = REPO_ROOT / "hysteria" / "profiles" / "server.yaml"
HYSTERIA_SECRETS = SECRETS_DIR / "hysteria.env"
HYSTERIA_COMPOSE = REPO_ROOT / "docker-compose.hysteria.yml"
HYBRID_COMPOSE = REPO_ROOT / "docker-compose.hybrid.yml"
NGINX_HYBRID_CONF = REPO_ROOT / "nginx" / "nginx-hybrid.conf"
NGINX_HYBRID_TEMPLATE = REPO_ROOT / "nginx" / "nginx-hybrid.template"


@dataclass
class EgressPeer:
    domain: str
    uuid: str
    public_key: str
    short_id: str
    port: int = 443
    transport: str = "xhttp"
    xhttp_path: str = ""
    chain_mode: str = "packet-up"
    reality_sni: str = PROVIDER_REALITY_SNI
    fingerprint: str = PROVIDER_FINGERPRINT
    grpc_service_name: str = PROVIDER_GRPC_SERVICE


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


def find_xray_binary() -> str | None:
    return shutil.which("xray")


def docker_xray(args: list[str]) -> str:
    result = run(["docker", "run", "--rm", XRAY_IMAGE] + args)
    return result.stdout.strip()


def xray_cli(args: list[str], *, prefer_native: bool = False) -> str:
    binary = find_xray_binary()
    if prefer_native and binary:
        return run([binary] + args).stdout.strip()
    if shutil.which("docker"):
        try:
            return docker_xray(args)
        except SystemExit:
            if binary:
                print("Docker xray failed; falling back to host xray binary.", file=sys.stderr)
                return run([binary] + args).stdout.strip()
            raise
    if binary:
        return run([binary] + args).stdout.strip()
    raise SystemExit(
        "Neither docker nor xray binary found. Install docker or xray-bin (Arch: pacman -S xray-bin)."
    )


def generate_uuid(*, prefer_native: bool = False) -> str:
    return xray_cli(["uuid"], prefer_native=prefer_native)


def generate_x25519(*, prefer_native: bool = False) -> tuple[str, str]:
    output = xray_cli(["x25519"], prefer_native=prefer_native)
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


def parse_egress_peer(args: argparse.Namespace, *, client_transport: str) -> EgressPeer:
    if args.egress_peer_file:
        env = parse_env_file(Path(args.egress_peer_file))
        transport = env.get("EGRESS_TRANSPORT", "xhttp")
        try:
            peer = EgressPeer(
                domain=env["EGRESS_DOMAIN"],
                uuid=env["EGRESS_UUID"],
                public_key=env["EGRESS_PUBLIC_KEY"],
                short_id=env.get("EGRESS_SHORT_ID", ""),
                port=int(env.get("EGRESS_PORT", "443")),
                transport=transport,
                xhttp_path=env.get("EGRESS_XHTTP_PATH", ""),
                chain_mode=env.get("EGRESS_CHAIN_MODE", "packet-up"),
                reality_sni=env.get("EGRESS_REALITY_SNI", PROVIDER_REALITY_SNI),
                fingerprint=env.get("EGRESS_FINGERPRINT", PROVIDER_FINGERPRINT),
                grpc_service_name=env.get("EGRESS_GRPC_SERVICE_NAME", PROVIDER_GRPC_SERVICE),
            )
        except KeyError as exc:
            raise SystemExit(f"Missing key in egress peer file: {exc}") from exc
        if peer.transport != client_transport:
            raise SystemExit(
                f"Egress peer transport is {peer.transport!r} but this node uses {client_transport!r}. "
                "Re-run egress with matching transport or use the correct egress-peer.env."
            )
        return peer

    if client_transport == "grpc":
        fields = {
            "egress-domain": args.egress_domain,
            "egress-uuid": args.egress_uuid,
            "egress-public-key": args.egress_public_key,
        }
        missing = [name for name, value in fields.items() if not value]
        if missing:
            raise SystemExit(
                "Bridge/entry role requires egress peer settings: "
                "use --egress-peer-file or provide "
                + ", ".join(f"--{name}" for name in missing)
            )
        return EgressPeer(
            domain=args.egress_domain,
            uuid=args.egress_uuid,
            public_key=args.egress_public_key,
            short_id=(args.egress_short_id or ""),
            port=args.egress_port,
            transport="grpc",
            reality_sni=args.egress_reality_sni or PROVIDER_REALITY_SNI,
            fingerprint=args.egress_fingerprint or PROVIDER_FINGERPRINT,
            grpc_service_name=args.egress_grpc_service_name or PROVIDER_GRPC_SERVICE,
        )

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
            "Bridge/entry role requires egress peer settings: "
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
        transport="xhttp",
    )


def resolve_transport_options(args: argparse.Namespace) -> tuple[str, int, str, str, str, str]:
    """Return transport, listen_port, reality_sni, reality_dest, fingerprint, grpc_service."""
    transport = "grpc" if args.provider else args.transport
    if transport == "grpc":
        listen_port = args.listen_port if args.listen_port is not None else PROVIDER_LISTEN_PORT
        reality_sni = args.reality_sni or PROVIDER_REALITY_SNI
        reality_dest = args.reality_dest or PROVIDER_REALITY_DEST
        fingerprint = args.fingerprint or PROVIDER_FINGERPRINT
        grpc_service = args.grpc_service_name or PROVIDER_GRPC_SERVICE
    else:
        listen_port = args.listen_port if args.listen_port is not None else 443
        reality_sni = args.reality_sni or ""
        reality_dest = args.reality_dest or ""
        fingerprint = args.fingerprint or "chrome"
        grpc_service = args.grpc_service_name or PROVIDER_GRPC_SERVICE
    return transport, listen_port, reality_sni, reality_dest, fingerprint, grpc_service


def write_compose_env(listen_port: int) -> None:
    env_path = REPO_ROOT / ".env"
    env_path.write_text(f"XRAY_LISTEN_PORT={listen_port}\n", encoding="utf-8")
    print(f"Wrote {env_path} (XRAY_LISTEN_PORT={listen_port})")


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


def validate_dns(domain: str, *, residential: bool = False) -> None:
    resolved = resolve_domain(domain)
    if not resolved:
        hint = "router's public IP" if residential else "this VPS"
        print(f"Warning: DNS lookup failed for {domain}. Ensure an A record points to {hint}.")
        return

    public_ip = get_public_ip()
    if residential:
        if public_ip and public_ip in resolved:
            print(
                f"DNS OK (residential): {domain} -> {', '.join(sorted(resolved))} "
                f"(matches this PC's outbound IP {public_ip})"
            )
        else:
            print(
                f"Residential DNS note: {domain} -> {', '.join(sorted(resolved))}. "
                f"This PC's outbound IP is {public_ip or 'unknown'}. "
                "Ensure the A record points to your router's WAN IP and TCP 443 is port-forwarded here."
            )
        return

    if public_ip and public_ip not in resolved:
        print(
            f"Warning: {domain} resolves to {', '.join(sorted(resolved))}, "
            f"but this host appears to be {public_ip}."
        )
    else:
        print(f"DNS OK: {domain} -> {', '.join(sorted(resolved))}")


def load_profile(role: str, transport: str) -> dict:
    profile_path = XRAY_PROFILES / f"{role}-{transport}.json"
    if not profile_path.is_file():
        raise SystemExit(f"Unknown profile: {profile_path}")
    return json.loads(profile_path.read_text(encoding="utf-8"))


def _client_inbound(config: dict, role: str) -> dict:
    preferred_tag = "phone-in" if role == "entry" else "client-in"
    for inbound in config["inbounds"]:
        if inbound.get("tag") == preferred_tag:
            return inbound
    return config["inbounds"][0]


def _patch_egress_outbound(config: dict, egress_peer: EgressPeer) -> None:
    egress_out = next(o for o in config["outbounds"] if o.get("tag") == "egress")
    vnext = egress_out["settings"]["vnext"][0]
    vnext["address"] = egress_peer.domain
    vnext["port"] = egress_peer.port
    vnext["users"][0]["id"] = egress_peer.uuid
    egress_reality = egress_out["streamSettings"]["realitySettings"]
    egress_reality["publicKey"] = egress_peer.public_key
    egress_reality["shortId"] = egress_peer.short_id

    if egress_peer.transport == "grpc":
        egress_out["streamSettings"]["grpcSettings"]["serviceName"] = egress_peer.grpc_service_name
        egress_reality["serverName"] = egress_peer.reality_sni
        egress_reality["fingerprint"] = egress_peer.fingerprint
        return

    egress_out["streamSettings"]["xhttpSettings"]["path"] = egress_peer.xhttp_path
    egress_out["streamSettings"]["xhttpSettings"]["mode"] = egress_peer.chain_mode
    egress_reality["serverName"] = egress_peer.domain


def patch_xray_config(
    role: str,
    transport: str,
    domain: str,
    uuid: str,
    private_key: str,
    short_id: str,
    *,
    listen_port: int = 443,
    reality_sni: str = "",
    reality_dest: str = "",
    fingerprint: str = "chrome",
    grpc_service_name: str = PROVIDER_GRPC_SERVICE,
    xhttp_path: str = "",
    xhttp_mode: str = "stream-one",
    egress_peer: EgressPeer | None = None,
    native: bool = False,
    hybrid: bool = False,
) -> None:
    config = load_profile(role, transport)
    inbound = _client_inbound(config, role)
    if hybrid:
        inbound["port"] = 8444
    else:
        inbound["port"] = listen_port
    inbound["settings"]["clients"][0]["id"] = uuid
    if transport == "tcp":
        inbound["settings"]["clients"][0]["flow"] = "xtls-rprx-vision"
    elif "flow" in inbound["settings"]["clients"][0]:
        del inbound["settings"]["clients"][0]["flow"]

    if transport == "xhttp":
        inbound["streamSettings"]["xhttpSettings"]["path"] = xhttp_path
        inbound["streamSettings"]["xhttpSettings"]["mode"] = xhttp_mode
    elif transport == "grpc":
        inbound["streamSettings"]["grpcSettings"]["serviceName"] = grpc_service_name

    reality = inbound["streamSettings"]["realitySettings"]
    reality["privateKey"] = private_key
    reality["shortIds"] = [short_id]

    if transport == "grpc":
        reality["serverNames"] = [reality_sni]
        reality["dest"] = reality_dest
        print(f"gRPC provider Reality: sni={reality_sni}, dest={reality_dest}, port={listen_port}")
    else:
        reality["serverNames"] = [domain]
        reality["dest"] = "nginx:8443"
        if native:
            reality["dest"] = "127.0.0.1:8443"
            print("Patched Reality dest -> 127.0.0.1:8443 (native nginx on host)")

    if role in CHAIN_ROLES:
        if egress_peer is None:
            raise SystemExit(f"Internal error: egress peer required for {role} role.")
        _patch_egress_outbound(config, egress_peer)
    elif transport == "grpc" and fingerprint:
        # fingerprint is client-side for direct connect; stored in secrets for URI export
        pass

    XRAY_CONFIG.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {XRAY_CONFIG} (role={role}, transport={transport}, port={inbound['port']})")


def write_egress_peer_file(
    domain: str,
    uuid: str,
    public_key: str,
    short_id: str,
    *,
    transport: str = "xhttp",
    xhttp_path: str = "",
    port: int = 443,
    chain_mode: str = "packet-up",
    reality_sni: str = PROVIDER_REALITY_SNI,
    fingerprint: str = PROVIDER_FINGERPRINT,
    grpc_service_name: str = PROVIDER_GRPC_SERVICE,
) -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        f"EGRESS_TRANSPORT={transport}",
        f"EGRESS_DOMAIN={domain}",
        f"EGRESS_PORT={port}",
        f"EGRESS_UUID={uuid}",
        f"EGRESS_PUBLIC_KEY={public_key}",
        f"EGRESS_SHORT_ID={short_id}",
    ]
    if transport == "grpc":
        lines.extend(
            [
                f"EGRESS_REALITY_SNI={reality_sni}",
                f"EGRESS_FINGERPRINT={fingerprint}",
                f"EGRESS_GRPC_SERVICE_NAME={grpc_service_name}",
            ]
        )
    else:
        lines.extend(
            [
                f"EGRESS_XHTTP_PATH={xhttp_path}",
                f"EGRESS_CHAIN_MODE={chain_mode}",
            ]
        )
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
    listen_port: int = 443,
    reality_sni: str = "",
    fingerprint: str = "chrome",
    grpc_service_name: str = PROVIDER_GRPC_SERVICE,
    xhttp_path: str = "",
    xhttp_mode: str = "stream-one",
    egress_peer: EgressPeer | None = None,
    native: bool = False,
) -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        f"ROLE={role}",
        f"TRANSPORT={transport}",
        f"DOMAIN={domain}",
        f"LISTEN_PORT={listen_port}",
        f"VLESS_UUID={uuid}",
        f"REALITY_PUBLIC_KEY={public_key}",
        f"SHORT_ID={short_id}",
    ]
    if native:
        lines.append("NATIVE=1")
    if transport == "grpc":
        lines.extend(
            [
                f"REALITY_SNI={reality_sni}",
                f"FINGERPRINT={fingerprint}",
                f"GRPC_SERVICE_NAME={grpc_service_name}",
            ]
        )
    if transport == "xhttp":
        lines.extend(
            [
                f"XHTTP_PATH={xhttp_path}",
                f"XHTTP_MODE={xhttp_mode}",
            ]
        )
    if role in CHAIN_ROLES and egress_peer is not None:
        lines.append(f"EGRESS_TRANSPORT={egress_peer.transport}")
        lines.extend(
            [
                f"EGRESS_DOMAIN={egress_peer.domain}",
                f"EGRESS_PORT={egress_peer.port}",
                f"EGRESS_UUID={egress_peer.uuid}",
                f"EGRESS_PUBLIC_KEY={egress_peer.public_key}",
                f"EGRESS_SHORT_ID={egress_peer.short_id}",
            ]
        )
        if egress_peer.transport == "grpc":
            lines.extend(
                [
                    f"EGRESS_REALITY_SNI={egress_peer.reality_sni}",
                    f"EGRESS_FINGERPRINT={egress_peer.fingerprint}",
                    f"EGRESS_GRPC_SERVICE_NAME={egress_peer.grpc_service_name}",
                ]
            )
        else:
            lines.extend(
                [
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
    listen_port: int = 443,
    reality_sni: str = "",
    fingerprint: str = "chrome",
    grpc_service_name: str = PROVIDER_GRPC_SERVICE,
    xhttp_path: str = "",
    xhttp_mode: str = "stream-one",
) -> str:
    sni = reality_sni or domain
    params: dict[str, str] = {
        "encryption": "none",
        "security": "reality",
        "sni": sni,
        "fp": fingerprint,
        "pbk": public_key,
    }
    if short_id:
        params["sid"] = short_id
    if transport == "tcp":
        params["flow"] = "xtls-rprx-vision"
        params["type"] = "tcp"
        label = "Self-Stealth-TCP"
    elif transport == "grpc":
        params["type"] = "grpc"
        params["serviceName"] = grpc_service_name
        label = "Provider-gRPC"
    else:
        params["type"] = "xhttp"
        params["path"] = xhttp_path
        params["mode"] = xhttp_mode
        label = "Self-Stealth-xHTTP"
    query = urllib.parse.urlencode(params)
    return f"vless://{uuid}@{domain}:{listen_port}?{query}#{label}"


def cert_deploy_env(domain: str, *, native: bool = False) -> dict[str, str]:
    env = {**os.environ, "PROXY_DOMAIN": domain}
    if native:
        env["PROXY_NATIVE"] = "1"
    return env


def issue_certificate(domain: str, email: str, skip_cert: bool, *, native: bool = False) -> None:
    if skip_cert:
        print("Skipping certificate issuance (--skip-cert).")
        return

    cert_path = Path(f"/etc/letsencrypt/live/{domain}/fullchain.pem")
    if cert_path.is_file():
        print(f"Certificate already exists at {cert_path}, deploying to ./certs/")
        run(
            [sys.executable, str(CERT_DEPLOY)],
            env=cert_deploy_env(domain, native=native),
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
        env=cert_deploy_env(domain, native=native),
    )


def start_compose() -> None:
    run(["docker", "compose", "up", "-d"])


def install_cron(domain: str, *, native: bool = False) -> None:
    native_env = "PROXY_NATIVE=1\n" if native else ""
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
                *([native_env.rstrip()] if native else []),
                cron_line,
                "",
            ]
        ),
        encoding="utf-8",
    )
    cron_path.chmod(0o644)
    print(f"Installed cron job at {cron_path}")


def write_native_nginx_conf(domain: str) -> None:
    www_root = REPO_ROOT / "www"
    content = f"""worker_processes 1;

events {{
    worker_connections 512;
}}

http {{
    types_hash_max_size 2048;
    types_hash_bucket_size 64;

    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;

    sendfile    on;
    tcp_nopush  on;
    keepalive_timeout 65;

    access_log off;
    error_log  /var/log/nginx/error.log warn;

    gzip on;
    gzip_vary on;
    gzip_proxied any;
    gzip_comp_level 4;
    gzip_types text/plain text/css application/json application/javascript text/xml;

    server {{
        listen 8443 ssl;
        http2 on;
        server_name _;

        ssl_certificate     /etc/letsencrypt/live/{domain}/fullchain.pem;
        ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;

        ssl_protocols TLSv1.3 TLSv1.2;
        ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305;
        ssl_prefer_server_ciphers off;
        ssl_session_cache shared:SSL:10m;
        ssl_session_timeout 1d;

        add_header X-Content-Type-Options nosniff always;
        add_header Referrer-Policy strict-origin-when-cross-origin always;
        add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

        root {www_root};
        index index.html;

        location / {{
            try_files $uri $uri/ /index.html;
        }}
    }}
}}
"""
    NGINX_NATIVE_CONF.write_text(content, encoding="utf-8")
    print(f"Wrote {NGINX_NATIVE_CONF}")
    print(f"Suggested system path: /etc/nginx/conf.d/denko-entry.conf (symlink or copy from above)")


def print_native_instructions(domain: str, *, transport: str = "tcp", listen_port: int = 443) -> None:
    repo = REPO_ROOT
    print("\n" + "=" * 60)
    print("Native entry install (systemd xray on host, no Docker)")
    print("=" * 60)
    if transport == "grpc":
        print(
            f"""
1. Router: forward TCP {listen_port} → this PC's LAN IP (not 443 unless you changed --listen-port).
2. DNS: {domain} A record → router public IP.
3. gRPC provider mode uses Reality camo (tesla.com) — no nginx decoy required.
4. Install packages (Arch Linux example):
     sudo pacman -S xray-bin
5. Install systemd unit:
     sudo bash {INSTALL_NATIVE_SCRIPT}
6. Enable Xray only:
     sudo systemctl enable --now denko-xray
7. Import the VLESS URI below on LTE (fp=qq, type=grpc, sni=tesla.com).

Full guide: {repo}/docs/grpc-provider.md
"""
        )
        return
    print(
        f"""
1. Router: forward TCP 443 → this PC's LAN IP.
2. DNS: {domain} A record → router public IP (verify: curl ifconfig.me).
3. Certificate must exist BEFORE Xray binds :443 (setup.py runs certbot if needed).
4. Install packages (Arch Linux example):
     sudo pacman -S xray-bin nginx certbot
5. Install systemd units:
     sudo bash {INSTALL_NATIVE_SCRIPT}
   Unit templates: {SYSTEMD_DIR}/xray.service
                   {SYSTEMD_DIR}/nginx.service
6. Enable services (nginx first — Reality fallback target):
     sudo systemctl enable --now denko-nginx
     sudo systemctl enable --now denko-xray
7. Connect from phone on LTE using the VLESS URI below.
8. Troubleshooting: on LTE visit https://{domain}/ — you should see the decoy site.

Full guide: {repo}/docs/residential-entry.md
"""
    )


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


def generate_hy2_password() -> str:
    return secrets.token_urlsafe(24)


def build_hy2_uri(domain: str, password: str, port: int = 443) -> str:
    user = urllib.parse.quote(password, safe="")
    return f"hy2://{user}@{domain}:{port}?insecure=0&sni={domain}#denko-hy2"


def write_hysteria_config(
    domain: str,
    password: str,
    *,
    hybrid: bool = False,
    native: bool = False,
) -> None:
    listen = ":8445" if hybrid else ":443"
    if native:
        cert_dir = str((REPO_ROOT / "certs").resolve())
        www_dir = str((REPO_ROOT / "www").resolve())
    else:
        cert_dir = "/etc/hysteria/certs"
        www_dir = "/etc/hysteria/www"

    template = HYSTERIA_PROFILE.read_text(encoding="utf-8")
    content = (
        template.replace("<LISTEN>", listen)
        .replace("<CERT_DIR>", cert_dir)
        .replace("<WWW_DIR>", www_dir)
        .replace("<HY2_PASSWORD>", password)
    )
    HYSTERIA_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    HYSTERIA_CONFIG.write_text(content, encoding="utf-8")
    print(f"Wrote {HYSTERIA_CONFIG} (listen={listen})")


def write_hysteria_secrets(
    role: str,
    domain: str,
    password: str,
    *,
    native: bool = False,
) -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "STACK=hysteria",
        f"ROLE={role}",
        f"DOMAIN={domain}",
        f"HY2_PASSWORD={password}",
    ]
    if native:
        lines.append("NATIVE=1")
    HYSTERIA_SECRETS.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(HYSTERIA_SECRETS, 0o600)
    print(f"Wrote {HYSTERIA_SECRETS}")


def write_hybrid_nginx_conf(domain: str) -> None:
    template_path = NGINX_HYBRID_TEMPLATE if NGINX_HYBRID_TEMPLATE.is_file() else NGINX_HYBRID_CONF
    if not template_path.is_file():
        raise SystemExit(f"Missing hybrid nginx template: {template_path}")
    content = template_path.read_text(encoding="utf-8").replace("<YOUR_DOMAIN>", domain)
    NGINX_HYBRID_CONF.write_text(content, encoding="utf-8")
    print(f"Wrote {NGINX_HYBRID_CONF} for domain {domain}")


def start_compose_hysteria() -> None:
    run(["docker", "compose", "-f", str(HYSTERIA_COMPOSE), "up", "-d"])


def start_compose_hybrid() -> None:
    run(
        [
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "-f",
            str(HYBRID_COMPOSE),
            "up",
            "-d",
        ]
    )


def print_hysteria_native_instructions(domain: str) -> None:
    print(
        f"""
Hysteria native install:
  sudo pacman -S hysteria   # or install from GitHub releases
  sudo bash scripts/install-native.sh --hysteria
  sudo systemctl enable --now denko-hysteria

Router: forward TCP **and UDP** 443 → this PC.
LTE test: import hy2:// URI from secrets/hysteria.env

See {REPO_ROOT}/docs/hysteria.md
"""
    )


def run_hysteria_setup(
    args: argparse.Namespace,
    role: str,
    domain: str,
    email: str,
    native: bool,
) -> None:
    if role == "bridge":
        raise SystemExit(
            "Bridge split-routing requires --stack xray. Use --stack hysteria with --role egress or entry."
        )
    if role in CHAIN_ROLES and args.egress_peer_file:
        print(
            "Warning: egress-peer-file ignored for hysteria stack (no Xray chain). "
            "All traffic exits from this node.",
            file=sys.stderr,
        )

    if args.keep_secrets and HYSTERIA_SECRETS.is_file():
        env = parse_env_file(HYSTERIA_SECRETS)
        password = env["HY2_PASSWORD"]
        print(f"Reusing password from {HYSTERIA_SECRETS}")
    else:
        password = generate_hy2_password()
        print("Generated Hysteria auth password.")

    hybrid = False
    write_hysteria_config(domain, password, hybrid=hybrid, native=native)
    write_hysteria_secrets(role, domain, password, native=native)

    issue_certificate(domain, email, args.skip_cert, native=native)

    skip_compose = args.skip_compose or native
    if not skip_compose:
        start_compose_hysteria()
    elif native:
        print("Skipping Docker compose (--native).")
    else:
        print("Skipping Docker compose (--skip-compose).")

    if args.install_cron:
        install_cron(domain, native=native)
    if args.install_renewal_hook:
        install_renewal_hook(domain)

    uri = build_hy2_uri(domain, password)
    print("\n" + "=" * 60)
    print(f"Setup complete (stack=hysteria, role={role}). Import this Hysteria2 URI:\n")
    print(uri)
    if native:
        print_hysteria_native_instructions(domain)
    else:
        print(f"\nIf not already started: docker compose -f {HYSTERIA_COMPOSE.name} up -d")
        print("\nSee docs/hysteria.md")
    print("=" * 60)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap VLESS+Reality Self-Stealth proxy")
    parser.add_argument("--domain", help="Your domain (A record must point to this VPS)")
    parser.add_argument("--email", help="Email for Let's Encrypt registration")
    parser.add_argument("--short-id", help="Reality short ID (8 hex chars); generated if omitted")
    parser.add_argument(
        "--stack",
        choices=STACKS,
        default="xray",
        help="Protocol stack: xray (default), hysteria (QUIC/UDP), or hybrid (both on 443)",
    )
    parser.add_argument(
        "--role",
        choices=ROLES,
        default="egress",
        help="Server role: egress (default), bridge (RU split-routing hop), or entry (residential home PC)",
    )
    parser.add_argument(
        "--transport",
        choices=TRANSPORTS,
        default="tcp",
        help="Transport: tcp (Vision), xhttp (TSPU), or grpc (provider-style tesla.com + qq)",
    )
    parser.add_argument(
        "--provider",
        action="store_true",
        help="Shorthand for --transport grpc with tesla.com SNI, fp=qq, port 6437, serviceName=grpc",
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        default=None,
        help="Xray listen port (default: 443 for tcp/xhttp, 6437 for grpc/--provider)",
    )
    parser.add_argument(
        "--reality-sni",
        help=f"Reality client SNI (default: your domain, or {PROVIDER_REALITY_SNI} for grpc)",
    )
    parser.add_argument(
        "--reality-dest",
        help=f"Reality dest fallback (default: nginx decoy, or {PROVIDER_REALITY_DEST} for grpc)",
    )
    parser.add_argument(
        "--fingerprint",
        choices=UTLS_FINGERPRINTS,
        help="Client uTLS fingerprint in exported URI (default: chrome, or qq for grpc)",
    )
    parser.add_argument(
        "--grpc-service-name",
        default=PROVIDER_GRPC_SERVICE,
        help=f"gRPC serviceName (default: {PROVIDER_GRPC_SERVICE})",
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
        help="Path to egress-peer.env from egress setup (required for bridge/entry unless CLI flags set)",
    )
    parser.add_argument("--egress-domain", help="Egress server domain (bridge/entry role)")
    parser.add_argument("--egress-uuid", help="Egress VLESS UUID (bridge/entry role)")
    parser.add_argument("--egress-public-key", help="Egress Reality public key (bridge/entry role)")
    parser.add_argument("--egress-short-id", help="Egress Reality short ID (bridge/entry role)")
    parser.add_argument("--egress-xhttp-path", help="Egress xHTTP path (xhttp chain)")
    parser.add_argument(
        "--egress-reality-sni",
        help=f"Egress Reality SNI for grpc chain (default: {PROVIDER_REALITY_SNI})",
    )
    parser.add_argument(
        "--egress-fingerprint",
        choices=UTLS_FINGERPRINTS,
        help=f"Egress chain uTLS fingerprint for grpc (default: {PROVIDER_FINGERPRINT})",
    )
    parser.add_argument(
        "--egress-grpc-service-name",
        help=f"Egress gRPC serviceName for grpc chain (default: {PROVIDER_GRPC_SERVICE})",
    )
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
    parser.add_argument(
        "--native",
        action="store_true",
        help="Entry role: run xray+nginx on host via systemd (no Docker); patches Reality dest to 127.0.0.1:8443",
    )
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
    stack = args.stack
    native = args.native
    transport, listen_port, reality_sni, reality_dest, fingerprint, grpc_service = (
        resolve_transport_options(args)
    )

    if native and role != "entry":
        raise SystemExit("--native is only supported with --role entry.")
    if stack == "hybrid" and role in CHAIN_ROLES:
        raise SystemExit(
            "hybrid stack does not support bridge/entry chain roles. Use --stack xray or hysteria with --role entry."
        )
    if stack in ("hysteria", "hybrid") and native and stack == "hybrid":
        raise SystemExit("--native with --stack hybrid is not supported. Use hysteria or xray.")
    if transport == "grpc" and stack == "hybrid":
        raise SystemExit("hybrid stack does not support grpc transport.")

    domain = prompt(args.domain, "Domain name")
    email = prompt(args.email, "Let's Encrypt email")

    if stack == "hysteria":
        run_hysteria_setup(args, role, domain, email, native)
        return

    validate_dns(domain, residential=(role == "entry"))

    egress_peer: EgressPeer | None = None
    if role in CHAIN_ROLES:
        egress_peer = parse_egress_peer(args, client_transport=transport)
        chain_desc = (
            f"{egress_peer.reality_sni} grpc"
            if egress_peer.transport == "grpc"
            else f"{egress_peer.chain_mode} xhttp"
        )
        print(f"Egress peer: {egress_peer.domain}:{egress_peer.port} ({chain_desc})")

    prefer_native_xray = native or args.skip_compose
    xhttp_path = ""
    xhttp_mode = args.xhttp_mode
    skip_cert = args.skip_cert or transport == "grpc"

    if transport == "grpc":
        print(
            f"gRPC provider profile: port={listen_port}, sni={reality_sni}, "
            f"fp={fingerprint}, serviceName={grpc_service}"
        )
        if not args.skip_cert:
            print("Skipping Let's Encrypt (grpc uses external Reality camo, not your domain cert).")

    if args.keep_secrets and SECRETS_FILE.is_file():
        existing = load_existing_client_secrets()
        print(f"Reusing secrets from {SECRETS_FILE}")
        uuid = existing["VLESS_UUID"]
        public_key = existing["REALITY_PUBLIC_KEY"]
        short_id = existing.get("SHORT_ID", "")
        private_key = load_private_key_from_config()
        listen_port = int(existing.get("LISTEN_PORT", listen_port))
        if transport == "grpc":
            reality_sni = existing.get("REALITY_SNI", reality_sni)
            fingerprint = existing.get("FINGERPRINT", fingerprint)
            grpc_service = existing.get("GRPC_SERVICE_NAME", grpc_service)
        if transport == "xhttp":
            xhttp_path = validate_xhttp_path(
                args.xhttp_path or existing.get("XHTTP_PATH") or generate_xhttp_path()
            )
            xhttp_mode = existing.get("XHTTP_MODE", args.xhttp_mode)
        print(f"UUID: {uuid}")
        print(f"Public key (client pbk): {public_key}")
        print(f"Short ID: {short_id!r}")
    else:
        print("Generating VLESS UUID...")
        uuid = generate_uuid(prefer_native=prefer_native_xray)
        print(f"UUID: {uuid}")

        print("Generating Reality X25519 key pair...")
        private_key, public_key = generate_x25519(prefer_native=prefer_native_xray)
        print(f"Private key (server): {private_key}")
        print(f"Public key (client pbk): {public_key}")

        if transport == "grpc":
            short_id = args.short_id if args.short_id is not None else ""
        else:
            short_id = args.short_id or generate_short_id()
            if not re.fullmatch(r"[0-9a-fA-F]{8}", short_id):
                raise SystemExit("Short ID must be exactly 8 hexadecimal characters.")
            short_id = short_id.lower()
        print(f"Short ID: {short_id!r}")

        if transport == "xhttp":
            xhttp_path = validate_xhttp_path(args.xhttp_path or generate_xhttp_path())
            print(f"xHTTP path: {xhttp_path}")
            print(f"xHTTP mode: {xhttp_mode}")

    if transport == "xhttp" and not xhttp_path:
        xhttp_path = validate_xhttp_path(args.xhttp_path or generate_xhttp_path())
        print(f"xHTTP path: {xhttp_path}")
        print(f"xHTTP mode: {xhttp_mode}")

    hybrid = stack == "hybrid"

    patch_xray_config(
        role,
        transport,
        domain,
        uuid,
        private_key,
        short_id,
        listen_port=listen_port,
        reality_sni=reality_sni,
        reality_dest=reality_dest,
        fingerprint=fingerprint,
        grpc_service_name=grpc_service,
        xhttp_path=xhttp_path,
        xhttp_mode=xhttp_mode,
        egress_peer=egress_peer,
        native=native,
        hybrid=hybrid,
    )
    write_secrets(
        role,
        transport,
        domain,
        uuid,
        public_key,
        short_id,
        listen_port=listen_port,
        reality_sni=reality_sni,
        fingerprint=fingerprint,
        grpc_service_name=grpc_service,
        xhttp_path=xhttp_path,
        xhttp_mode=xhttp_mode,
        egress_peer=egress_peer,
        native=native,
    )
    write_compose_env(listen_port)

    if role == "egress" and transport == "xhttp":
        write_egress_peer_file(
            domain,
            uuid,
            public_key,
            short_id,
            transport="xhttp",
            xhttp_path=xhttp_path,
            port=listen_port,
            chain_mode="packet-up",
        )
    elif role == "egress" and transport == "grpc":
        write_egress_peer_file(
            domain,
            uuid,
            public_key,
            short_id,
            transport="grpc",
            port=listen_port,
            reality_sni=reality_sni,
            fingerprint=fingerprint,
            grpc_service_name=grpc_service,
        )

    if stack == "hybrid":
        if args.keep_secrets and HYSTERIA_SECRETS.is_file():
            hy2_password = parse_env_file(HYSTERIA_SECRETS)["HY2_PASSWORD"]
            print(f"Reusing Hysteria password from {HYSTERIA_SECRETS}")
        else:
            hy2_password = generate_hy2_password()
        write_hybrid_nginx_conf(domain)
        write_hysteria_config(domain, hy2_password, hybrid=True, native=False)
        write_hysteria_secrets(role, domain, hy2_password, native=False)

    if native and transport != "grpc":
        write_native_nginx_conf(domain)

    issue_certificate(domain, email, skip_cert, native=native and transport != "grpc")

    skip_compose = args.skip_compose or native
    if not skip_compose:
        if stack == "hybrid":
            start_compose_hybrid()
        else:
            start_compose()
    elif native:
        print("Skipping docker compose (--native).")
    else:
        print("Skipping docker compose (--skip-compose).")

    if args.install_cron and transport != "grpc":
        install_cron(domain, native=native)

    if args.install_renewal_hook and transport != "grpc":
        install_renewal_hook(domain)

    uri = build_vless_uri(
        transport,
        domain,
        uuid,
        public_key,
        short_id,
        listen_port=listen_port,
        reality_sni=reality_sni,
        fingerprint=fingerprint,
        grpc_service_name=grpc_service,
        xhttp_path=xhttp_path,
        xhttp_mode=xhttp_mode,
    )
    print("\n" + "=" * 60)
    print(f"Setup complete (stack={stack}, role={role}, transport={transport}). Import this VLESS URI:\n")
    print(uri)
    if stack == "hybrid" and HYSTERIA_SECRETS.is_file():
        hy2_env = parse_env_file(HYSTERIA_SECRETS)
        print(f"\nHysteria2 URI (UDP/443):\n{build_hy2_uri(domain, hy2_env['HY2_PASSWORD'])}")
    if role == "bridge":
        print("\nClients connect to the bridge domain above. Non-RU traffic exits via egress.")
    if role == "entry":
        print("\nClients connect to the entry domain above (e.g. phone on LTE). Non-RU traffic exits via egress.")
    if role == "egress" and transport in ("xhttp", "grpc"):
        print(f"\nCopy {EGRESS_PEER_FILE} to the bridge/entry machine for chain setup.")
    if transport == "grpc":
        print(
            f"\nProvider-style gRPC: connect to {domain}:{listen_port}, "
            f"sni={reality_sni}, fp={fingerprint}, serviceName={grpc_service}. "
            "Open this port in firewall / router."
        )
    if stack == "hybrid":
        print("\nHybrid: TCP/443 → VLESS (Xray), UDP/443 → Hysteria2. Forward UDP 443 on router/firewall.")
    if native:
        print_native_instructions(domain, transport=transport, listen_port=listen_port)
    else:
        print("\nSee README.md, docs/grpc-provider.md, docs/transports.md, docs/multi-hop.md.")
    print("=" * 60)


if __name__ == "__main__":
    main()
