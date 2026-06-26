# Ansible automation for Ubuntu 22.04 VPS deployment.

One playbook installs Docker, certbot, and UFW rules, deploys this repository, and runs `scripts/setup.py`.

## Prerequisites (control machine — your laptop)

- Ansible 2.14+ (`pip install ansible` or your distro package)
- SSH access to the VPS as root or a sudo user
- DNS A record for your domain already pointing at the VPS

Install the Ansible collection used for file sync:

```bash
cd ansible
ansible-galaxy collection install -r requirements.yml
```

## Configure

```bash
cd ansible
cp inventory.example inventory
cp group_vars/all.example.yml group_vars/all.yml
# Edit inventory (VPS IP) and group_vars/all.yml (domain, email)
```

### Deploy methods

**Option A — rsync from laptop (default)**  
Leave `proxy_git_repo` empty in `group_vars/all.yml`. Ansible pushes the local repo copy to the VPS.

**Option B — git clone on VPS**  
Set `proxy_git_repo` to your remote URL. The VPS pulls the repo directly (requires deploy key or public repo).

## Run

From the `ansible/` directory:

```bash
ansible-playbook playbook.yml
```

Or pass variables inline:

```bash
ansible-playbook playbook.yml \
  -e proxy_domain=pocjp.denko.app \
  -e letsencrypt_email=me@rian.moe
```

First run takes a few minutes (Docker install + cert issuance + image pull).

## Idempotency

| Task | Re-run behavior |
|------|-----------------|
| Docker / certbot / UFW | Skipped if already installed |
| File sync / git pull | Updates changed files |
| `setup.py` | **Skipped** if `secrets/client.env` exists |
| `docker compose up -d` | Ensures containers are up |

To regenerate keys and re-issue config, set in `group_vars/all.yml`:

```yaml
proxy_force_reconfigure: true
```

## Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `proxy_domain` | *(required)* | Domain with A record → VPS |
| `letsencrypt_email` | *(required)* | Let's Encrypt contact |
| `proxy_install_dir` | `/opt/denko-node-poc` | Install path on VPS |
| `proxy_git_repo` | `""` | Git URL; empty = rsync from laptop |
| `proxy_git_version` | `main` | Git branch or tag |
| `proxy_install_cron` | `true` | Install cert renewal cron |
| `proxy_install_renewal_hook` | `true` | Install certbot deploy hook |
| `proxy_ufw_enable` | `true` | Open ports 22/80/443 via UFW |
| `proxy_role` | `egress` | `egress` (single node) or `bridge` (RU hop) |
| `proxy_transport` | `tcp` | `tcp` or `xhttp` (bridge requires `xhttp`) |
| `proxy_xhttp_path` | *(random)* | xHTTP path when transport is xhttp |
| `proxy_xhttp_mode` | `stream-one` | xHTTP mode for client inbound |
| `proxy_egress_peer_file` | `""` | Local path to `egress-peer.env` (bridge role) |
| `proxy_egress_domain` | `""` | Egress domain if not using peer file |
| `proxy_egress_uuid` | `""` | Egress UUID if not using peer file |
| `proxy_egress_public_key` | `""` | Egress Reality public key |
| `proxy_egress_short_id` | `""` | Egress Reality short ID |
| `proxy_egress_xhttp_path` | `""` | Egress xHTTP path |
| `proxy_egress_port` | `443` | Egress port |
| `proxy_egress_chain_mode` | `packet-up` | Bridge→egress xHTTP mode |
| `proxy_keep_secrets` | `false` | Pass `--keep-secrets` to setup.py |
| `proxy_force_reconfigure` | `false` | Re-run setup.py even if configured |

## What Ansible does not automate

- DNS configuration (set the A record at your registrar before running)
- Client app setup (see main README for v2rayN / Nekoray / sing-box)

## Troubleshooting

```bash
# Verbose run
ansible-playbook playbook.yml -vv

# Check connectivity
ansible proxy -m ping

# Re-run only bootstrap on VPS manually
ssh root@your-vps "cd /opt/denko-node-poc && python3 scripts/setup.py --domain ... --email ..."
```
