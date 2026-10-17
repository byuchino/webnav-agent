#!/bin/sh
# Install cloudflared on the CCSE lab controller (LXC 904) from Cloudflare's apt repository.
# From the dev box:
#   ssh proxmox-1 'pct exec 904 -- sh' < ~/ccse-lab-runner/deploy/ccse/install-cloudflared.sh
# It installs the package only. Registering the tunnel (cloudflared service install <token>) is
# done by hand inside the container, so the token never sits in a file or in shell history here.
set -e
install -d -m 0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  -o /usr/share/keyrings/cloudflare-main.gpg
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared bookworm main' \
  > /etc/apt/sources.list.d/cloudflared.list
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cloudflared >/dev/null
cloudflared --version
