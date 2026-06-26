# Hysteria2 (QUIC/UDP) stack

Alternative to Xray for paths where mobile DPI fingerprints VLESS/Reality. Hysteria2 uses **UDP/443** (QUIC) with TLS and HTTP masquerade.

## Stacks

| `--stack` | Services | Use case |
|-----------|----------|----------|
| `xray` (default) | Xray + Nginx decoy | VLESS + Reality Self-Stealth |
| `hysteria` | Hysteria2 only | Mobile-friendly QUIC; no Xray chain |
| `hybrid` | Nginx stream + Xray + Hysteria | TCP/443 → Xray decoy, UDP/443 → Hysteria |

Bridge/entry **split routing to Xray egress** requires `--stack xray`. Hysteria entry/egress is standalone (all traffic exits from that node).

## Bootstrap (Docker)

```bash
sudo python3 scripts/setup.py \
  --stack hysteria \
  --role egress \
  --domain hy.example.com \
  --email you@example.com

docker compose -f docker-compose.hysteria.yml up -d
```

**Native residential entry:**

```bash
sudo python3 scripts/setup.py \
  --stack hysteria \
  --role entry \
  --native \
  --domain yers.denko.app \
  --email you@example.com \
  --skip-compose

sudo bash scripts/install-native.sh --hysteria
sudo systemctl enable --now denko-hysteria
```

## Router / firewall

Forward **both** to your PC:

| Port | Protocol |
|------|----------|
| 443 | TCP |
| 443 | **UDP** |

```bash
sudo ufw allow 443/tcp
sudo ufw allow 443/udp
```

## Client URI

Printed by `setup.py`:

```
hy2://PASSWORD@domain:443?insecure=0&sni=domain#denko-hy2
```

Import into:

- **Hysteria2** apps (official clients)
- **v2rayNG** / **Nekoray** / **sing-box** with Hysteria2 outbound support

Parameters in `secrets/hysteria.env`:

```env
STACK=hysteria
DOMAIN=yers.denko.app
HY2_PASSWORD=...
```

## Masquerade

Server config uses **file masquerade** from `./www` (same decoy site as Xray). Probes see normal HTTP responses on the Hysteria port.

## Hybrid stack (VPS)

```bash
sudo python3 scripts/setup.py --stack hybrid --role egress --transport xhttp --domain ...
docker compose -f docker-compose.yml -f docker-compose.hybrid.yml up -d
```

See `nginx/nginx-hybrid.conf` — TCP 443 → Xray (via internal 8444) or decoy 8443; UDP 443 → Hysteria 8445.

## vs Xray on mobile

Hysteria does not fix all carriers (some throttle QUIC/UDP). It is a **different fingerprint**, not magic. Test on LTE separately.

## Further reading

- [Hysteria 2 docs](https://v2.hysteria.network/)
- [docs/transports.md](transports.md) — Xray transport notes
- [docs/residential-entry.md](residential-entry.md) — port forwarding
