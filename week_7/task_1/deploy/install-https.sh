#!/usr/bin/env bash
# Run as root with one public IP; the app remains bound to loopback.
set -euo pipefail
graphics_ip="${1:?Usage: install-https.sh PUBLIC_IP}"
python3 - "$graphics_ip" <<'PY'
import ipaddress, sys
ip = ipaddress.ip_address(sys.argv[1])
if ip.version != 4 or not ip.is_global:
    raise SystemExit('A public IPv4 address is required')
PY
if [ "$(id -u)" -ne 0 ]; then
    echo 'Run this script as root.' >&2
    exit 1
fi
install -d -m 755 /var/lib/ai-advent-day31/acme/.well-known/acme-challenge
graphics_nginx=/etc/nginx/sites-available/ai-advent-day31
cat > "$graphics_nginx" <<EOF
server {
    listen 80;
    server_name $graphics_ip;
    location ^~ /.well-known/acme-challenge/ {
        root /var/lib/ai-advent-day31/acme;
        default_type text/plain;
    }
    location / { return 503; }
}
EOF
ln -sfn "$graphics_nginx" /etc/nginx/sites-enabled/ai-advent-day31
nginx -t
systemctl reload nginx
/opt/ai-advent-day31-certbot/bin/certbot certonly --non-interactive --agree-tos \
    --register-unsafely-without-email --preferred-profile shortlived \
    --webroot -w /var/lib/ai-advent-day31/acme --ip-address "$graphics_ip" \
    --cert-name "$graphics_ip" --keep-until-expiring
cat > "$graphics_nginx" <<EOF
server {
    listen 80;
    server_name $graphics_ip;
    location ^~ /.well-known/acme-challenge/ {
        root /var/lib/ai-advent-day31/acme;
        default_type text/plain;
    }
    location / { return 301 https://$graphics_ip\$request_uri; }
}
server {
    listen 443 ssl;
    server_name $graphics_ip;
    ssl_certificate /etc/letsencrypt/live/$graphics_ip/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$graphics_ip/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 128k;
    add_header Strict-Transport-Security "max-age=86400" always;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy same-origin always;
    location / {
        proxy_pass http://127.0.0.1:5031;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$remote_addr;
        proxy_connect_timeout 5s;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        proxy_buffering off;
    }
}
EOF
nginx -t
systemctl reload nginx
cat > /etc/systemd/system/ai-advent-day31-cert-renew.service <<'EOF'
[Unit]
Description=Renew the short-lived graphics learning IP certificate
[Service]
Type=oneshot
ExecStart=/opt/ai-advent-day31-certbot/bin/certbot renew --quiet --deploy-hook "/usr/bin/systemctl reload nginx"
EOF
cat > /etc/systemd/system/ai-advent-day31-cert-renew.timer <<'EOF'
[Unit]
Description=Check graphics learning IP certificate twice a day
[Timer]
OnCalendar=*-*-* 03,15:25:00
RandomizedDelaySec=600
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now ai-advent-day31-cert-renew.timer
echo 'HTTPS configured; certificate renewal timer enabled.'
# end
