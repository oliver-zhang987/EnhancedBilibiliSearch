#!/bin/sh
set -e
cd /opt/ebsearch
VS=/opt/videosummary/.env
VS_API_KEY=$(grep '^VS_API_KEY=' "$VS" | cut -d= -f2- | tr -d '\r"')
DS_KEY=$(grep '^VS_LLM_API_KEY=' "$VS" | cut -d= -f2- | tr -d '\r"')
DS_BASE=$(grep '^VS_LLM_BASE_URL=' "$VS" | cut -d= -f2- | tr -d '\r"')
[ -z "$DS_BASE" ] && DS_BASE=https://api.deepseek.com
# Synthesis runs on DeepSeek dsv4pro (China-direct, no rate cap, richer output).
# To route it to Groq gpt-oss-120b via the relay instead, set EBS_SYNTH_BASE_URL=
# https://api.groq.com/openai/v1 + EBS_SYNTH_API_KEY + EBS_SYNTH_PROXY below.
# reuse an existing gate key if present, else mint one
if [ -f /opt/ebsearch/.env ] && grep -q '^EBS_SERVER_API_KEY=' /opt/ebsearch/.env; then
  EBS_KEY=$(grep '^EBS_SERVER_API_KEY=' /opt/ebsearch/.env | cut -d= -f2-)
else
  EBS_KEY=ebs_$(openssl rand -hex 16 2>/dev/null || (head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n'))
fi
cat > /opt/ebsearch/.env <<EOF
EBS_BACKEND_URL=http://127.0.0.1:8010
EBS_BACKEND_API_KEY=$VS_API_KEY
EBS_LLM_API_KEY=$DS_KEY
EBS_LLM_BASE_URL=$DS_BASE
EBS_SYNTH_MODEL=deepseek-v4-pro
EBS_COOKIES_FILE=/app/cookies.txt
EBS_SERVER_API_KEY=$EBS_KEY
EBS_HOST=127.0.0.1
EBS_PORT=8020
EBS_MAX_VIDEOS=4
EBS_MIN_PLAY=1000
EOF
chmod 600 /opt/ebsearch/.env
echo "=== build ==="
docker build -q -t ebsearch:latest /opt/ebsearch
docker rm -f ebsearch 2>/dev/null || true
mkdir -p /opt/ebsearch/data
docker run -d --name ebsearch --restart unless-stopped --network host -m 300m \
  --env-file /opt/ebsearch/.env \
  -v /opt/videosummary/cookies.txt:/app/cookies.txt:ro \
  -v /opt/ebsearch/data:/app/data \
  ebsearch:latest >/dev/null
sleep 5
echo "=== container ==="; docker ps --filter name=ebsearch --format '{{.Names}} {{.Status}}'
echo "=== health ==="; curl -s -m 8 http://127.0.0.1:8020/health; echo
curl -s -m 8 -o /dev/null -w 'GET / HTTP=%{http_code}\n' http://127.0.0.1:8020/
curl -s -m 8 -o /dev/null -w 'POST /api/research (no key, expect 401) HTTP=%{http_code}\n' \
  -X POST http://127.0.0.1:8020/api/research -H 'Content-Type: application/json' -d '{"topic":"x"}'
echo "SERVER_KEY=$EBS_KEY"
