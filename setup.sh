#!/usr/bin/env bash
# Установка «Домашних дел» на сервер Ubuntu / Debian.
# Первый запуск: спросит токен бота и всё настроит.
# Повторный запуск (после git pull): обновит код и перезапустит сервис, данные сохранятся.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите так: sudo bash setup.sh"
  exit 1
fi

SRC="$(cd "$(dirname "$0")" && pwd)"
APP=/opt/kidsbot
ENV_FILE="$APP/.env"

echo "==> Устанавливаю системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 python3-venv curl ca-certificates gnupg

if ! command -v caddy >/dev/null 2>&1; then
  echo "==> Устанавливаю Caddy (веб-сервер с автоматическим HTTPS)"
  if ! apt-get install -y -q caddy; then
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
      | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
      > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -q
    apt-get install -y -q caddy
  fi
fi

id kidsbot >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin kidsbot
mkdir -p "$APP"
cp "$SRC/server.py" "$SRC/index.html" "$SRC/requirements.txt" "$APP/"

if [ ! -f "$ENV_FILE" ]; then
  echo
  read -rp "Вставьте токен бота от @BotFather и нажмите Enter: " BOT_TOKEN
  BOT_TOKEN="$(echo "$BOT_TOKEN" | tr -d '[:space:]')"
  if ! echo "$BOT_TOKEN" | grep -Eq '^[0-9]+:[A-Za-z0-9_-]+$'; then
    echo "Это не похоже на токен бота. Запустите скрипт ещё раз."
    exit 1
  fi
  IP="$(curl -fs4 --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')"
  DEFAULT_DOMAIN="${IP//./-}.sslip.io"
  read -rp "Домен (если своего нет — просто нажмите Enter) [$DEFAULT_DOMAIN]: " DOMAIN
  DOMAIN="${DOMAIN:-$DEFAULT_DOMAIN}"
  DOMAIN="${DOMAIN#https://}"; DOMAIN="${DOMAIN%/}"
  printf 'BOT_TOKEN=%s\nPUBLIC_URL=https://%s\n' "$BOT_TOKEN" "$DOMAIN" > "$ENV_FILE"
fi
DOMAIN="$(grep '^PUBLIC_URL=' "$ENV_FILE" | sed 's#^PUBLIC_URL=https://##')"

echo "==> Устанавливаю Python-библиотеки"
[ -d "$APP/.venv" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
chown -R kidsbot:kidsbot "$APP"
chmod 600 "$ENV_FILE"

echo "==> Настраиваю автозапуск"
cat > /etc/systemd/system/kidsbot.service <<EOF
[Unit]
Description=Kids tasks Telegram Mini App
After=network-online.target
Wants=network-online.target

[Service]
User=kidsbot
WorkingDirectory=$APP
EnvironmentFile=$ENV_FILE
ExecStart=$APP/.venv/bin/uvicorn server:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:8000
}
EOF

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
fi

systemctl daemon-reload
systemctl enable kidsbot >/dev/null 2>&1
systemctl restart kidsbot
systemctl enable caddy >/dev/null 2>&1
systemctl restart caddy

echo "==> Проверяю, что всё работает (до минуты, пока выпускается HTTPS-сертификат)"
OK=0
for _ in $(seq 1 20); do
  if curl -fsS --max-time 5 "https://$DOMAIN/" -o /dev/null 2>/dev/null; then OK=1; break; fi
  sleep 3
done

echo "==> Проверяю бота (long polling)"
BOT_TOKEN_VAL="$(grep '^BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
PROXY_VAL="$(grep '^TELEGRAM_PROXY=' "$ENV_FILE" | cut -d= -f2- || true)"
CURL_PROXY=()
[ -n "$PROXY_VAL" ] && CURL_PROXY=(--proxy "$PROXY_VAL")
if ! curl -s --max-time 10 "${CURL_PROXY[@]}" "https://api.telegram.org/bot$BOT_TOKEN_VAL/getMe" -o /tmp/kidsbot_getme.json; then
  echo "!! Сервер не может связаться с api.telegram.org."
  echo "   Приложение будет открываться, но бот не ответит на /start и не пришлёт уведомления."
  echo "   Решение: сервер в другой стране или прокси (TELEGRAM_PROXY=socks5://... в $ENV_FILE)."
elif ! grep -q '"ok":true' /tmp/kidsbot_getme.json; then
  echo "!! Telegram отклонил токен: $(cat /tmp/kidsbot_getme.json)"
  echo "   Исправьте строку BOT_TOKEN в $ENV_FILE и выполните: systemctl restart kidsbot"
else
  BOT_OK=0
  for _ in $(seq 1 10); do
    if journalctl -u kidsbot --since "-3min" --no-pager | grep -q "long polling работает"; then BOT_OK=1; break; fi
    sleep 2
  done
  if [ "$BOT_OK" = 1 ]; then
    echo "Бот запущен: напишите ему /start в Telegram."
  else
    echo "!! Бот не запустился. Последние строки журнала:"
    journalctl -u kidsbot -n 15 --no-pager
  fi
fi
rm -f /tmp/kidsbot_getme.json

echo
if [ "$OK" = 1 ]; then
  echo "Готово! Адрес мини-приложения:"
  echo
  echo "    https://$DOMAIN"
  echo
  echo "Вставьте его в @BotFather (Bot Settings → Configure Mini App)."
else
  echo "Сервер запущен, но адрес https://$DOMAIN пока не открывается."
  echo "Посмотреть ошибки:  journalctl -u kidsbot -n 30   и   journalctl -u caddy -n 30"
fi
