# Transport profiles: TCP vs xHTTP (TSPU notes)

This stack supports two VLESS+Reality transports. Both use the same **Self-Stealth** model: Reality `dest` points at internal Nginx with your Let's Encrypt cert; unauthorized probes see a normal HTTPS site on your domain.

## Quick comparison

| | **TCP + Vision** | **xHTTP + stream-one** |
|---|------------------|------------------------|
| **Best for** | Maximum client compatibility, simple direct connect | Resisting TSPU traffic analysis / long-session throttling |
| **Client `flow`** | `xtls-rprx-vision` (required) | *(omit — incompatible with xHTTP)* |
| **Client `type`** | `tcp` | `xhttp` |
| **Extra params** | — | `path`, `mode=stream-one` |
| **TSPU angle** | Strong when IP is clean; long single TCP sessions may be frozen after ~15–20 KB in some regions | Multiplexes payload across HTTP-style framing; harder to fingerprint as “plain VLESS” |

Select at bootstrap:

```bash
sudo python3 scripts/setup.py --transport tcp   # default
sudo python3 scripts/setup.py --transport xhttp   # TSPU-oriented alternative
```

## Russian TSPU — what the research says (2025–2026)

These points come from [Xray issue #6293](https://github.com/XTLS/Xray-core/issues/6293), [Habr chain guides](https://habr.com/en/articles/990206/), and community double-hop docs. They are operational heuristics, not guarantees.

### What gets flagged

- **Plain VLESS** (no Vision, weak Reality target) — increasingly fingerprinted.
- **Long-lived TCP sessions** — TSPU may stop forwarding after ~15–20 KB on a single connection instead of sending RST (session “freeze”).
- **TLS fingerprint** — use `fp=chrome` (or another real browser uTLS profile); avoid default Go/Python handshakes.
- **Connection rate** — many rapid TLS handshakes to the same IP+SNI from one NAT can trigger blocks; avoid hammering the server with parallel probes.
- **IP reputation** — datacenter IPs in “known VPN” ranges are blocked regardless of protocol; Self-Stealth (your domain on your IP) helps but does not eliminate this.

### Why xHTTP helps

- **xHTTP** splits traffic across HTTP request semantics instead of one raw VLESS stream over TCP.
- With **Reality**, Xray defaults to **`stream-one`** mode: one TLS connection, gRPC-style header padding, no separate download sub-connection. This is the recommended mode for direct Reality (not CDN relay).
- **`packet-up`** is for CDN / middlebox traversal (e.g. bridge → egress through Cloudflare). Use it on relay hops, not on this single-node Self-Stealth egress unless you know you need it.

### Why TCP + Vision still matters

- **Vision** removes the TLS-in-TLS signal that DPI uses against naive VLESS.
- Still the best default when clients are old or xHTTP is unsupported.
- Prefer **TCP** if you connect directly from outside Russia to this node and sessions stay short.

### Self-Stealth Reality target (both transports)

Keep:

- `serverNames` = your domain (matches LE cert on Nginx fallback)
- `dest` = `nginx:8443`
- Nginx: **TLS 1.3 + HTTP/2**, valid cert, realistic static site

Avoid foreign SNI spoofing on the same IP as your domain — that is what Self-Stealth fixes.

### If you are inside Russia

A single foreign VPS may still see throttling. Operators often add a **domestic bridge** (RU VPS → this egress) with **xHTTP `packet-up`** on the bridge leg. This repo is the **egress** node; see [petrochen/xray-double-hop](https://github.com/petrochen/xray-double-hop) for chain topology.

## xHTTP profile defaults in this repo

| Setting | Value | Rationale |
|---------|-------|-----------|
| `mode` | `stream-one` | Xray default for Reality; one connection, lower probe surface |
| `path` | random `/api/v1/<hex>` | Looks like an API route; generated per install |
| `fp` | `chrome` | Common browser uTLS |
| sniffing `quic` | enabled | Better routing metadata on modern clients |

Override path:

```bash
sudo python3 scripts/setup.py --transport xhttp --xhttp-path /api/v2/sync
```

Path must start with `/`, contain no spaces, and match exactly on client and server.

## Client URI examples

**TCP:**

```
vless://UUID@domain:443?encryption=none&flow=xtls-rprx-vision&security=reality&sni=domain&fp=chrome&pbk=PUBKEY&sid=SHORTID&type=tcp
```

**xHTTP:**

```
vless://UUID@domain:443?encryption=none&security=reality&sni=domain&fp=chrome&pbk=PUBKEY&sid=SHORTID&type=xhttp&path=%2Fapi%2Fv1%2Fabc12345&mode=stream-one
```

Note: no `flow` parameter on xHTTP.

## sing-box outbound (xHTTP)

```json
{
  "type": "vless",
  "server": "example.com",
  "server_port": 443,
  "uuid": "YOUR-UUID",
  "tls": {
    "enabled": true,
    "server_name": "example.com",
    "utls": { "enabled": true, "fingerprint": "chrome" },
    "reality": {
      "enabled": true,
      "public_key": "YOUR-PUBLIC-KEY",
      "short_id": "a1b2c3d4"
    }
  },
  "transport": {
    "type": "xhttp",
    "path": "/api/v1/abc12345",
    "mode": "stream-one"
  }
}
```

Requires sing-box 1.12+ with xHTTP support.
