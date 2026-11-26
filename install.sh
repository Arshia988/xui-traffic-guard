#!/usr/bin/env bash
# XUI Traffic Guard - اسکریپت نصب خودکار
# استفاده:  bash <(curl -Ls https://raw.githubusercontent.com/YOUR_GITHUB_USERNAME/xui-traffic-guard/main/install.sh)
set -e

# >>>>>> فقط این خط را با نام کاربری گیت‌هاب خودت عوض کن <<<<<<
REPO="YOUR_GITHUB_USERNAME/xui-traffic-guard"
BRANCH="main"

RAW="https://raw.githubusercontent.com/${REPO}/${BRANCH}"
DIR="/opt/xui-traffic-guard"
SERVICE="xui-traffic-guard"
SERVICE_FILE="/etc/systemd/system/${SERVICE}.service"

red()   { echo -e "\e[31m$*\e[0m"; }
green() { echo -e "\e[32m$*\e[0m"; }
yellow(){ echo -e "\e[33m$*\e[0m"; }

ask() { # ask "سوال" "پیش‌فرض" -> REPLY
    local q="$1" def="$2"
    read -r -p "$q [$def]: " REPLY </dev/tty || true
    REPLY="${REPLY:-$def}"
}

[ "$(id -u)" -eq 0 ] || { red "این اسکریپت باید با root اجرا شود (sudo -i)"; exit 1; }
command -v systemctl >/dev/null || { red "systemd پیدا نشد؛ این سیستم پشتیبانی نمی‌شود."; exit 1; }

install_deps() {
    yellow ">> نصب پیش‌نیازها..."
    if command -v apt-get >/dev/null; then
        apt-get update -y
        DEBIAN_FRONTEND=noninteractive apt-get install -y curl python3 python3-flask python3-requests
    elif command -v dnf >/dev/null; then
        dnf install -y curl python3 python3-pip
        python3 -m pip install flask requests || python3 -m pip install --break-system-packages flask requests
    elif command -v yum >/dev/null; then
        yum install -y curl python3 python3-pip
        python3 -m pip install flask requests
    else
        red "مدیر بسته شناخته نشد. python3 و flask و requests را دستی نصب کنید."; exit 1
    fi
    python3 -c "import flask, requests" || { red "نصب flask/requests ناموفق بود"; exit 1; }
}

download_app() {
    yellow ">> دانلود برنامه از گیت‌هاب..."
    mkdir -p "$DIR"
    curl -fsSL "${RAW}/app.py" -o "${DIR}/app.py.new" || { red "دانلود app.py ناموفق بود. مقدار REPO را چک کنید."; exit 1; }
    python3 -m py_compile "${DIR}/app.py.new" || { red "فایل دانلودشده معتبر نیست"; exit 1; }
    mv "${DIR}/app.py.new" "${DIR}/app.py"
    rm -rf "${DIR}/__pycache__"
}

write_service() {
    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=XUI Traffic Guard (traffic monitor & auto shutdown)
After=network-online.target x-ui.service
Wants=network-online.target

[Service]
WorkingDirectory=${DIR}
ExecStart=/usr/bin/env python3 ${DIR}/app.py
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable "$SERVICE" >/dev/null 2>&1
}

configure() {
    echo
    green "===== تنظیمات اولیه (Enter = مقدار پیش‌فرض) ====="
    ask "پورت پنل مانیتور" "8088";                         PORT="$REPLY"
    ask "رمز ورود پنل مانیتور" "$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 12)"; PASS="$REPLY"
    ask "سقف حجم به گیگابایت (1024 = 1TB)" "1024";        LIMIT="$REPLY"
    echo "اقدام هنگام رسیدن به سقف:  1) خاموش کردن سرور   2) توقف x-ui   3) فقط اطلاع"
    ask "انتخاب" "1"
    case "$REPLY" in 2) ACTION="stop_xui";; 3) ACTION="notify";; *) ACTION="shutdown";; esac
    ask "روز ریست ماهانه (0 = غیرفعال، 1 تا 28)" "0";      RESET_DAY="$REPLY"
    ask "شمارش از API پنل ثنایی باشد؟ (n = مستقیم از سیستم، پیشنهادی) y/n" "n"
    SOURCE="local"; XURL="http://127.0.0.1:2053"; XUSER="admin"; XPASS="admin"
    if [[ "$REPLY" =~ ^[Yy]$ ]]; then
        SOURCE="xui"
        ask "آدرس پنل ثنایی (همراه webBasePath)" "$XURL"; XURL="$REPLY"
        ask "یوزرنیم پنل ثنایی" "$XUSER";                 XUSER="$REPLY"
        ask "پسورد پنل ثنایی" "$XPASS";                   XPASS="$REPLY"
    fi
    ask "توکن ربات تلگرام (اختیاری، Enter = رد)" "";      TG_TOKEN="$REPLY"
    TG_CHAT=""
    [ -n "$TG_TOKEN" ] && { ask "Chat ID تلگرام" ""; TG_CHAT="$REPLY"; }

    PORT="$PORT" PASS="$PASS" LIMIT="$LIMIT" ACTION="$ACTION" RESET_DAY="$RESET_DAY" \
    SOURCE="$SOURCE" XURL="$XURL" XUSER="$XUSER" XPASS="$XPASS" TG_TOKEN="$TG_TOKEN" TG_CHAT="$TG_CHAT" \
    CFG="${DIR}/config.json" python3 - <<'PY'
import json, os
e = os.environ
cfg = {}
if os.path.exists(e["CFG"]):
    try: cfg = json.load(open(e["CFG"], encoding="utf-8"))
    except Exception: cfg = {}
cfg.update({
    "port": int(e["PORT"]), "panel_password": e["PASS"],
    "secret_key": cfg.get("secret_key") or os.urandom(16).hex(),
    "limit_gb": float(e["LIMIT"]), "action": e["ACTION"],
    "reset_day": max(0, min(28, int(e["RESET_DAY"] or 0))),
    "source": e["SOURCE"], "xui_url": e["XURL"], "xui_username": e["XUSER"],
    "xui_password": e["XPASS"], "tg_token": e["TG_TOKEN"], "tg_chat_id": e["TG_CHAT"],
})
json.dump(cfg, open(e["CFG"], "w", encoding="utf-8"), ensure_ascii=False, indent=2)
os.chmod(e["CFG"], 0o600)
PY
}

open_firewall() {
    if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
        ufw allow "${PORT}/tcp" >/dev/null && yellow ">> پورت ${PORT} در ufw باز شد"
    fi
}

show_info() {
    local ip port
    ip=$(curl -s4 --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')
    port=$(python3 -c "import json;print(json.load(open('${DIR}/config.json')).get('port',8088))" 2>/dev/null || echo 8088)
    echo
    green "=============================================="
    green " ✅ XUI Traffic Guard فعال است"
    green " آدرس پنل:  http://${ip}:${port}"
    [ -n "$PASS" ] && green " رمز ورود:  ${PASS}"
    green " وضعیت:     systemctl status ${SERVICE}"
    green " لاگ‌ها:     journalctl -u ${SERVICE} -f"
    green "=============================================="
}

do_install() {
    install_deps
    download_app
    configure
    write_service
    systemctl restart "$SERVICE"
    open_firewall
    sleep 2
    systemctl is-active --quiet "$SERVICE" || { red "سرویس اجرا نشد. لاگ: journalctl -u ${SERVICE} -n 50"; exit 1; }
    show_info
}

do_update() {
    [ -f "${DIR}/app.py" ] || { red "برنامه نصب نیست."; exit 1; }
    download_app
    write_service
    systemctl restart "$SERVICE"
    green "✅ به‌روزرسانی انجام شد (تنظیمات و آمار مصرف حفظ شد)."
}

do_uninstall() {
    ask "مطمئنی می‌خواهی حذف شود؟ آمار مصرف هم پاک می‌شود. y/n" "n"
    [[ "$REPLY" =~ ^[Yy]$ ]] || exit 0
    systemctl disable --now "$SERVICE" 2>/dev/null || true
    rm -f "$SERVICE_FILE"; systemctl daemon-reload
    rm -rf "$DIR"
    green "✅ حذف شد."
}

case "$1" in
    install)   do_install ;;
    update)    do_update ;;
    uninstall) do_uninstall ;;
    *)
        if [ -f "${DIR}/app.py" ]; then
            echo "XUI Traffic Guard قبلاً نصب شده است:"
            echo " 1) به‌روزرسانی (حفظ تنظیمات و آمار)"
            echo " 2) نصب مجدد / تغییر تنظیمات (آمار حفظ می‌شود)"
            echo " 3) حذف کامل"
            echo " 4) نمایش اطلاعات"
            echo " 0) خروج"
            ask "انتخاب" "1"
            case "$REPLY" in
                1) do_update ;; 2) do_install ;; 3) do_uninstall ;; 4) show_info ;; *) exit 0 ;;
            esac
        else
            do_install
        fi ;;
esac
