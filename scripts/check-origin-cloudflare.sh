#!/usr/bin/env bash
# Run ON THE SERVER (SSH as root). Diagnoses Cloudflare 521-style failures.
# Usage: sudo bash scripts/check-origin-cloudflare.sh
# Or from repo root: sudo bash scripts/check-origin-cloudflare.sh

set +e

APP_DOMAIN="${APP_DOMAIN:-app.treehouserly.asia}"
API_DOMAIN="${API_DOMAIN:-api.treehouserly.asia}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-compose.prod.yaml}"

pass() { echo "[OK] $*"; }
fail() { echo "[!!] $*"; }

echo "=== AgentFlow origin check (for Cloudflare) ==="
echo "APP_DOMAIN=$APP_DOMAIN API_DOMAIN=$API_DOMAIN"
echo ""

echo "--- 1) Nginx systemd ---"
if systemctl is-active --quiet nginx 2>/dev/null; then
  pass "nginx service is active"
else
  fail "nginx is NOT active. Fix: sudo systemctl start nginx && sudo systemctl enable nginx"
fi

echo ""
echo "--- 2) Ports 80 / 443 (must listen for Cloudflare -> origin) ---"
if ss -tlnp 2>/dev/null | grep -q ':80 '; then
  pass "something listens on tcp :80"
  ss -tlnp | grep ':80 ' || true
else
  fail "nothing on :80. Fix: sudo systemctl restart nginx ; check sudo nginx -t"
fi
if ss -tlnp 2>/dev/null | grep -q ':443 '; then
  pass "something listens on tcp :443"
else
  echo "    (no :443 yet is OK before certbot; Cloudflare may use 80 only in some modes)"
fi

echo ""
echo "--- 3) Nginx config test ---"
if sudo nginx -t 2>&1; then
  pass "nginx -t"
else
  fail "nginx config invalid"
fi

echo ""
echo "--- 4) Docker stack (backend for Nginx proxy) ---"
if command -v docker >/dev/null 2>&1; then
  (cd "$ROOT" && docker compose -f "$COMPOSE_FILE" ps) || fail "docker compose ps failed (wrong directory?)"
else
  fail "docker not installed"
fi

echo ""
echo "--- 5) Local upstreams (127.0.0.1) ---"
code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 5 http://127.0.0.1:8000/healthz 2>/dev/null)
if [ "$code" = "200" ]; then
  pass "API /healthz -> HTTP $code"
else
  fail "API /healthz -> HTTP $code (expect 200). Fix: docker logs, .env, DB"
fi
code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 5 http://127.0.0.1:8501/ 2>/dev/null)
if [ "$code" = "200" ] || [ "$code" = "302" ] || [ "$code" = "301" ]; then
  pass "Frontend / -> HTTP $code"
else
  fail "Frontend / -> HTTP $code (expect 200/302). Fix: docker compose logs frontend"
fi

echo ""
echo "--- 6) Nginx vhost routing (same as browser Host header) ---"
code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 5 -H "Host: $APP_DOMAIN" http://127.0.0.1/ 2>/dev/null)
if [ "$code" = "200" ] || [ "$code" = "302" ] || [ "$code" = "301" ]; then
  pass "Nginx+app ($APP_DOMAIN) -> HTTP $code"
else
  fail "Nginx+app ($APP_DOMAIN) -> HTTP $code (expect 200/302). Fix: server_name in /etc/nginx/sites-available/agentflow"
fi
code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 5 -H "Host: $API_DOMAIN" http://127.0.0.1/healthz 2>/dev/null)
if [ "$code" = "200" ]; then
  pass "Nginx+api ($API_DOMAIN /healthz) -> HTTP $code"
else
  fail "Nginx+api ($API_DOMAIN /healthz) -> HTTP $code"
fi

echo ""
echo "--- 7) This server's public IPv4 (compare with Cloudflare A record) ---"
curl -4 -sS --max-time 3 https://ifconfig.me/ip 2>/dev/null || curl -4 -sS --max-time 3 https://api.ipify.org 2>/dev/null || echo "could not detect public IP"

echo ""
echo "=== Done ==="
echo "If 1–2 fail or 6 fails: fix Nginx + Alibaba firewall (TCP 80, 443)."
echo "If 4–5 fail: fix Docker / .env first."
echo "If 1–6 pass but browser still 521: Cloudflare DNS A/AAAA must point to THIS server's IP; try orange-cloud off to test origin."
