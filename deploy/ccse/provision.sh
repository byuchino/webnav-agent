#!/bin/sh
# Provision the CCSE lab controller (LXC 904). Run INSIDE the container, as root:
#   ssh proxmox-1 'pct exec 904 -- sh -c "curl -fsSL https://raw.githubusercontent.com/byuchino/webnav-agent/ccse-lab/deploy/ccse/provision.sh | sh"'
# or copy it in and run it. Idempotent: safe to re-run after a pull.
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3-venv curl ca-certificates >/dev/null

# The runner is the ccse-lab branch -- never main, which is the CCFA lab's.
if [ ! -d /opt/ccse-lab/.git ]; then
  git clone -q --branch ccse-lab https://github.com/byuchino/webnav-agent.git /opt/ccse-lab
else
  git -C /opt/ccse-lab pull -q --ff-only
fi
[ -x /opt/ccse-lab/.venv/bin/python ] || python3 -m venv /opt/ccse-lab/.venv
/opt/ccse-lab/.venv/bin/pip install -q --upgrade pip
/opt/ccse-lab/.venv/bin/pip install -q -r /opt/ccse-lab/requirements.txt

install -d -m 755 /opt/ccse-labs /srv/labs-landing
install -d -m 700 /root/.falcon-lab
install -m 644 /opt/ccse-lab/deploy/ccse/landing/index.html /srv/labs-landing/index.html
install -m 644 /opt/ccse-lab/deploy/ccse/ccse-lab.service /etc/systemd/system/ccse-lab.service
install -m 644 /opt/ccse-lab/deploy/ccse/labs-landing.service /etc/systemd/system/labs-landing.service
systemctl daemon-reload
systemctl enable -q --now labs-landing.service
systemctl enable -q ccse-lab.service
systemctl restart ccse-lab.service

echo "runner:  $(git -C /opt/ccse-lab log --oneline -1)"
echo "panel:   $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8902/)  (8902)"
echo "landing: $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8903/)  (8903)"
[ -f /root/.falcon-lab/api.json ] || echo "TODO: /root/.falcon-lab/api.json (0600) -- the CCSE lab's own Falcon API client"
[ -n "$(ls -A /opt/ccse-labs 2>/dev/null)" ] || echo "TODO: publish scenarios -- ~/ccse/tools/publish_labs.sh"
