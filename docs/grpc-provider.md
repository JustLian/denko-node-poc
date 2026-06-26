# gRPC provider profile (tesla.com + qq)

Commercial-style VLESS + Reality + **gRPC** transport — matches configs that work on Russian mobile LTE when xHTTP/TCP fail.

| Setting | Default | Provider equivalent |
|---------|---------|---------------------|
| Transport | `grpc` | `network: grpc` |
| Listen port | `6437` | non-443 |
| Reality SNI | `tesla.com` | client `serverName` |
| Reality dest | `www.tesla.com:443` | server fallback |
| uTLS fingerprint | `qq` | QQ Browser ClientHello |
| gRPC serviceName | `grpc` | `/grpc` path |
| Short ID | *(empty)* | omitted in many exports |
| Flow | *(none)* | no Vision |

This is **not** Self-Stealth (your domain on Nginx). Probes without valid credentials see Tesla's TLS stack via Reality `dest`, not your decoy site.

## Quick start

**One flag** (all provider defaults):

```bash
sudo python3 scripts/setup.py --provider \
  --role egress \
  --domain egress.example.com \
  --email you@example.com
```

Equivalent explicit form:

```bash
sudo python3 scripts/setup.py \
  --transport grpc \
  --listen-port 6437 \
  --reality-sni tesla.com \
  --reality-dest www.tesla.com:443 \
  --fingerprint qq \
  --grpc-service-name grpc \
  --role egress \
  --domain egress.example.com \
  --email you@example.com
```

Open **TCP 6437** on the VPS firewall (`ufw allow 6437/tcp`).

## Residential entry (native, LTE test)

```bash
sudo python3 scripts/setup.py --provider \
  --role entry \
  --native \
  --egress-peer-file ./secrets/egress-peer.env \
  --domain yers.denko.app \
  --email you@example.com \
  --skip-compose

sudo bash scripts/install-native.sh
sudo systemctl enable --now denko-xray
```

Router: forward **TCP 6437** → home PC (not 443 unless you override `--listen-port`).

No nginx or Let's Encrypt required for the tunnel (setup skips cert issuance automatically).

## Multi-hop (grpc chain)

1. **Egress** with `--provider` → copies `secrets/egress-peer.env` (includes `EGRESS_TRANSPORT=grpc`).
2. **Entry / bridge** with `--provider` and the same peer file:

```bash
sudo python3 scripts/setup.py --provider \
  --role entry \
  --native \
  --egress-peer-file ./secrets/egress-peer.env \
  --domain yers.denko.app \
  --email you@example.com \
  --skip-compose
```

Split routing (RU direct, rest via egress) works the same as xHTTP chain profiles.

## Client URI shape

Printed by `setup.py`:

```
vless://UUID@your.domain:6437?encryption=none&security=reality&sni=tesla.com&fp=qq&pbk=...&type=grpc&serviceName=grpc#Provider-gRPC
```

Import into v2rayNG, Nekoray, or sing-box. Match **address** = your DNS name, **port** = 6437, **sni** = `tesla.com`, **fp** = `qq`.

## Overrides

| Flag | Example |
|------|---------|
| `--listen-port 443` | Listen on 443 like a normal HTTPS port |
| `--reality-sni www.apple.com` | Different camo SNI (set matching `--reality-dest`) |
| `--fingerprint ios` | Try other uTLS profiles |
| `--grpc-service-name MyService` | Custom gRPC path |
| `--short-id a1b2c3d4` | Add short ID filter (provider often uses none) |

## vs Self-Stealth (tcp/xhttp)

| | Self-Stealth | gRPC provider |
|---|--------------|---------------|
| SNI | Your domain | External (tesla.com) |
| Nginx decoy | Required | Not used |
| LE certificate | Required | Skipped |
| Port | 443 | 6437 (default) |
| Mobile LTE | Often blocked | Often works |

You can run **egress grpc** abroad and keep a separate **entry xhttp** chain only if egress exposes both — this repo expects one transport per node; use grpc end-to-end for provider parity.

## Firewall

```bash
sudo ufw allow 6437/tcp
ss -tlnp | grep 6437
```

Docker reads `XRAY_LISTEN_PORT` from `.env` (written by setup).
