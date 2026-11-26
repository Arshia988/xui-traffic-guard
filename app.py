#!/usr/bin/env python3
# XUI Traffic Guard - مانیتور حجم سرور برای پنل ثنایی (3x-ui) با خاموشی خودکار
import os, json, time, threading, subprocess, datetime, hmac
import requests, urllib3
from functools import wraps
from flask import Flask, request, redirect, session, jsonify, render_template_string

VERSION = "1.0.0"
urllib3.disable_warnings()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
GB = 1024 ** 3

DEFAULT_CONFIG = {
    "listen": "0.0.0.0",
    "port": 8088,
    "panel_password": "admin123",          # رمز همین پنل مانیتور
    "secret_key": os.urandom(16).hex(),
    "source": "local",                     # local = مستقیم از سیستم | xui = از API پنل ثنایی
    "xui_url": "http://127.0.0.1:2053",    # اگر webBasePath داری: http://127.0.0.1:2053/yourpath
    "xui_username": "admin",
    "xui_password": "admin",
    "limit_gb": 1024,                      # 1024 GB = 1 TB
    "warn_percent": 90,
    "action": "shutdown",                  # shutdown | stop_xui | notify
    "reset_day": 0,                        # 0 = غیرفعال، 1 تا 28 = روز ریست ماهانه
    "boot_grace_min": 10,
    "poll_interval": 15,
    "tg_token": "",
    "tg_chat_id": "",
}
DEFAULT_STATE = {
    "up": 0, "down": 0, "last_sent": None, "last_recv": None,
    "boot_time": None, "last_poll": None, "speed_up": 0, "speed_down": 0,
    "cycle_key": None, "action_done": False, "warned": False, "error": None,
}

lock = threading.Lock()


def load_json(path, default):
    data = dict(default)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data.update(json.load(f))
        except Exception:
            pass
    return data


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


config = load_json(CONFIG_FILE, DEFAULT_CONFIG)
save_json(CONFIG_FILE, config)
try:
    os.chmod(CONFIG_FILE, 0o600)
except Exception:
    pass
state = load_json(STATE_FILE, DEFAULT_STATE)


# ---------------- اعلان تلگرام ----------------
def notify(msg):
    print("[notify]", msg, flush=True)
    if config.get("tg_token") and config.get("tg_chat_id"):
        try:
            requests.post(f"https://api.telegram.org/bot{config['tg_token']}/sendMessage",
                          data={"chat_id": config["tg_chat_id"], "text": msg}, timeout=10)
        except Exception as e:
            print("telegram error:", e, flush=True)


# ---------------- منابع داده ----------------
class XUIClient:
    def __init__(self):
        self.s = requests.Session()
        self.logged = False

    def login(self):
        url = config["xui_url"].rstrip("/") + "/login"
        r = self.s.post(url, data={"username": config["xui_username"],
                                   "password": config["xui_password"]},
                        verify=False, timeout=10)
        j = r.json()
        if not j.get("success"):
            raise Exception("ورود به پنل ثنایی ناموفق بود: " + str(j.get("msg", "")))
        self.logged = True

    def status(self):
        base = config["xui_url"].rstrip("/")
        endpoints = [("POST", "/server/status"),            # نسخه‌های قدیمی‌تر
                     ("GET", "/panel/api/server/status"),     # نسخه‌های جدیدتر
                     ("POST", "/panel/api/server/status")]
        for _ in range(2):
            if not self.logged:
                self.login()
            for method, path in endpoints:
                try:
                    r = self.s.request(method, base + path, verify=False, timeout=10)
                    j = r.json()
                except Exception:
                    continue
                if j.get("success") and j.get("obj"):
                    o = j["obj"]
                    nt = o.get("netTraffic", {})
                    return int(nt.get("sent", 0)), int(nt.get("recv", 0)), float(o.get("uptime", 0))
            self.logged = False   # احتمالاً سشن منقضی شده؛ دوباره لاگین
        raise Exception("دریافت وضعیت از پنل ثنایی ناموفق بود (آدرس/مسیر/یوزر را چک کنید)")


def local_status():
    sent = recv = 0
    with open("/proc/net/dev") as f:
        for line in f.readlines()[2:]:
            name, data = line.split(":", 1)
            if name.strip() == "lo":
                continue
            p = data.split()
            recv += int(p[0])
            sent += int(p[8])
    with open("/proc/uptime") as f:
        uptime = float(f.read().split()[0])
    return sent, recv, uptime


xui = XUIClient()


# ---------------- منطق اصلی ----------------
def current_cycle_key():
    day = int(config.get("reset_day") or 0)
    if day <= 0:
        return None
    t = datetime.date.today()
    y, m = t.year, t.month
    if t.day < day:
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return f"{y}-{m:02d}"


def reset_usage(reason="دستی"):
    was_stopped = state.get("action_done") and config["action"] == "stop_xui"
    state.update(up=0, down=0, action_done=False, warned=False)
    save_json(STATE_FILE, state)
    if was_stopped:
        subprocess.run(["systemctl", "start", "x-ui"])
    notify(f"✅ مصرف حجم صفر شد ({reason})")


def do_action(total):
    act = config["action"]
    notify(f"⛔ مصرف به سقف رسید: {total / GB:.2f} GB از {config['limit_gb']} GB — اقدام: {act}")
    if act == "shutdown":
        save_json(STATE_FILE, state)
        subprocess.Popen(["sh", "-c", "sleep 5 && shutdown -h now"])
    elif act == "stop_xui":
        subprocess.run(["systemctl", "stop", "x-ui"])


def poll_once():
    sent, recv, uptime = local_status() if config["source"] == "local" else xui.status()
    now = time.time()
    with lock:
        # ریست دوره ماهانه
        key = current_cycle_key()
        if key and state.get("cycle_key") != key:
            if state.get("cycle_key") is not None:
                reset_usage("شروع دوره جدید")
            state["cycle_key"] = key

        boot = now - uptime if uptime else None
        rebooted = bool(boot and state["boot_time"] and abs(boot - state["boot_time"]) > 120)

        if state["last_sent"] is None:                       # اولین اجرا
            d_up = d_down = 0
        elif rebooted or sent < state["last_sent"] or recv < state["last_recv"]:
            d_up, d_down = sent, recv                         # شمارنده‌ها از صفر شروع شده‌اند
        else:
            d_up, d_down = sent - state["last_sent"], recv - state["last_recv"]

        if rebooted:
            state["action_done"] = False   # بعد از بوت دوباره کنترل شود (پس از مهلت)

        if state["last_poll"] and not rebooted:
            dt = max(now - state["last_poll"], 1)
            state["speed_up"], state["speed_down"] = d_up / dt, d_down / dt

        state["up"] += d_up
        state["down"] += d_down
        state.update(last_sent=sent, last_recv=recv, last_poll=now, error=None)
        if boot:
            state["boot_time"] = boot

        total = state["up"] + state["down"]
        limit = float(config["limit_gb"]) * GB
        in_grace = uptime and uptime < float(config["boot_grace_min"]) * 60

        if limit > 0:
            if total >= limit * float(config["warn_percent"]) / 100 and not state["warned"]:
                state["warned"] = True
                notify(f"⚠️ مصرف به {config['warn_percent']}% سقف رسید: {total / GB:.2f} GB")
            if total >= limit and not state["action_done"] and not in_grace:
                state["action_done"] = True
                save_json(STATE_FILE, state)
                do_action(total)
        save_json(STATE_FILE, state)


def monitor_loop():
    while True:
        try:
            poll_once()
        except Exception as e:
            with lock:
                state["error"] = str(e)
                save_json(STATE_FILE, state)
        time.sleep(max(5, int(config.get("poll_interval", 15))))


# ---------------- وب ----------------
app = Flask(__name__)
app.secret_key = config["secret_key"]


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("ok"):
            if request.path.startswith("/api/"):
                return jsonify(error="unauthorized"), 401
            return redirect("/login")
        return f(*a, **k)
    return w


LOGIN_HTML = """<!doctype html><html dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ورود</title>
<style>body{font-family:Tahoma;background:#0f172a;color:#e2e8f0;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}
form{background:#1e293b;padding:30px;border-radius:12px;width:280px}input,button{padding:10px;border-radius:6px;border:0;margin-top:10px;width:100%;box-sizing:border-box}
button{background:#3b82f6;color:#fff;cursor:pointer}</style></head><body><form method="post"><h3>ورود به مانیتور حجم</h3>
<input type="password" name="password" placeholder="رمز عبور" autofocus><button>ورود</button>
<p style="color:#f87171">{{err}}</p></form></body></html>"""

MAIN_HTML = """<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>مانیتور حجم سرور</title>
<style>
body{font-family:Tahoma,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:20px}
.card{background:#1e293b;border-radius:12px;padding:20px;max-width:780px;margin:0 auto 16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.box{background:#0f172a;border-radius:8px;padding:12px;text-align:center}.box b{display:block;font-size:1.3em;margin-top:6px}
.bar{height:24px;background:#334155;border-radius:12px;overflow:hidden;margin:16px 0 6px}
.fill{height:100%;width:0;background:#22c55e;transition:.5s}
label{display:block;margin:12px 0 4px}.row{display:flex;gap:8px}
input,select{width:100%;padding:8px;border-radius:6px;border:1px solid #475569;background:#0f172a;color:#e2e8f0;box-sizing:border-box}
button{padding:10px 18px;border:0;border-radius:8px;background:#3b82f6;color:#fff;cursor:pointer;margin-top:14px}
.red{background:#ef4444}.muted{color:#94a3b8;font-size:.9em;margin-top:6px}.err{color:#f87171}a{color:#93c5fd}
</style></head><body>
<div class="card">
  <h2>📊 مصرف حجم سرور <span class="muted">v{{version}}</span></h2>
  <div class="grid">
    <div class="box">ارسال (آپلود)<b id="up">-</b></div>
    <div class="box">دریافت (دانلود)<b id="down">-</b></div>
    <div class="box">مجموع<b id="total">-</b></div>
    <div class="box">باقی‌مانده<b id="remain">-</b></div>
  </div>
  <div class="bar"><div class="fill" id="fill"></div></div>
  <div id="pct"></div>
  <div class="muted">سرعت: ↑ <span id="su">-</span> | ↓ <span id="sd">-</span></div>
  <div class="muted">آخرین بروزرسانی: <span id="lp">-</span></div>
  <div class="err" id="err"></div>
  <button class="red" onclick="resetUsage()">صفر کردن مصرف</button>
  &nbsp;<a href="/logout">خروج</a>
</div>
<div class="card"><h3>⚙️ تنظیمات</h3>
<form id="f">
  <label>سقف حجم</label>
  <div class="row"><input name="limit_value" type="number" step="0.01" value="{{limit_value}}">
  <select name="limit_unit"><option value="GB" {{'selected' if limit_unit=='GB'}}>GB</option>
  <option value="TB" {{'selected' if limit_unit=='TB'}}>TB</option></select></div>
  <label>درصد هشدار</label><input name="warn_percent" type="number" value="{{c.warn_percent}}">
  <label>اقدام هنگام رسیدن به سقف</label>
  <select name="action">
    <option value="shutdown" {{'selected' if c.action=='shutdown'}}>خاموش کردن سرور</option>
    <option value="stop_xui" {{'selected' if c.action=='stop_xui'}}>متوقف کردن x-ui</option>
    <option value="notify" {{'selected' if c.action=='notify'}}>فقط اطلاع‌رسانی</option>
  </select>
  <label>روز ریست ماهانه (۰ = غیرفعال، ۱ تا ۲۸)</label><input name="reset_day" type="number" min="0" max="28" value="{{c.reset_day}}">
  <label>مهلت بعد از بوت (دقیقه)</label><input name="boot_grace_min" type="number" value="{{c.boot_grace_min}}">
  <label>منبع داده</label>
  <select name="source"><option value="local" {{'selected' if c.source=='local'}}>مستقیم از سیستم (پیشنهادی)</option>
  <option value="xui" {{'selected' if c.source=='xui'}}>API پنل ثنایی</option></select>
  <label>آدرس پنل ثنایی (با webBasePath)</label><input name="xui_url" value="{{c.xui_url}}">
  <label>یوزرنیم پنل ثنایی</label><input name="xui_username" value="{{c.xui_username}}">
  <label>پسورد پنل ثنایی (خالی = بدون تغییر)</label><input name="xui_password" type="password">
  <label>توکن ربات تلگرام (اختیاری)</label><input name="tg_token" value="{{c.tg_token}}">
  <label>Chat ID تلگرام</label><input name="tg_chat_id" value="{{c.tg_chat_id}}">
  <label>رمز جدید این پنل (خالی = بدون تغییر)</label><input name="panel_password" type="password">
  <button>ذخیره</button>
</form></div>
<script>
function fmt(b){const u=['B','KB','MB','GB','TB'];let i=0;while(b>=1024&&i<4){b/=1024;i++}return b.toFixed(2)+' '+u[i]}
async function load(){
  const r=await fetch('/api/status'); if(r.status==401){location='/login';return}
  const s=await r.json();
  up.textContent=fmt(s.up); down.textContent=fmt(s.down); total.textContent=fmt(s.total);
  remain.textContent=s.limit>0?fmt(Math.max(s.limit-s.total,0)):'نامحدود';
  const p=s.limit>0?Math.min(s.total/s.limit*100,100):0;
  fill.style.width=p+'%'; fill.style.background=p>=100?'#ef4444':p>=s.warn?'#f59e0b':'#22c55e';
  pct.textContent=p.toFixed(1)+'% از '+fmt(s.limit)+(s.action_done?' — ⛔ اقدام انجام شده':'')+(s.in_grace?' — ⏳ در مهلت بعد از بوت':'');
  su.textContent=fmt(s.speed_up)+'/s'; sd.textContent=fmt(s.speed_down)+'/s';
  lp.textContent=s.last_poll?new Date(s.last_poll*1000).toLocaleString('fa-IR'):'-';
  err.textContent=s.error||'';
}
f.onsubmit=async e=>{e.preventDefault();
  const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(Object.fromEntries(new FormData(f)))});
  alert((await r.json()).msg); load();}
async function resetUsage(){if(!confirm('مصرف صفر شود؟'))return;await fetch('/api/reset',{method:'POST'});load()}
load(); setInterval(load,5000);
</script></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("password", ""), config["panel_password"]):
            session["ok"] = True
            return redirect("/")
        time.sleep(1)
        err = "رمز اشتباه است"
    return render_template_string(LOGIN_HTML, err=err)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/")
@login_required
def index():
    lg = float(config["limit_gb"])
    if lg >= 1024 and lg % 1024 == 0:
        val, unit = lg / 1024, "TB"
    else:
        val, unit = lg, "GB"
    return render_template_string(MAIN_HTML, c=config, limit_value=f"{val:g}",
                                  limit_unit=unit, version=VERSION)


@app.route("/api/status")
@login_required
def api_status():
    with lock:
        s = dict(state)
    uptime = time.time() - s["boot_time"] if s.get("boot_time") else None
    return jsonify(up=s["up"], down=s["down"], total=s["up"] + s["down"],
                   limit=float(config["limit_gb"]) * GB, warn=float(config["warn_percent"]),
                   speed_up=s["speed_up"], speed_down=s["speed_down"], last_poll=s["last_poll"],
                   error=s["error"], action_done=s["action_done"],
                   in_grace=bool(uptime is not None and uptime < float(config["boot_grace_min"]) * 60))


@app.route("/api/settings", methods=["POST"])
@login_required
def api_settings():
    d = request.get_json(force=True)
    try:
        with lock:
            val = float(d["limit_value"])
            config["limit_gb"] = val * 1024 if d.get("limit_unit") == "TB" else val
            config["warn_percent"] = max(1, min(100, int(d["warn_percent"])))
            config["action"] = d["action"] if d["action"] in ("shutdown", "stop_xui", "notify") else "notify"
            config["reset_day"] = max(0, min(28, int(d["reset_day"])))
            config["boot_grace_min"] = max(0, int(d["boot_grace_min"]))
            config["source"] = "xui" if d.get("source") == "xui" else "local"
            for k in ("xui_url", "xui_username", "tg_token", "tg_chat_id"):
                config[k] = d.get(k, "").strip()
            for k in ("xui_password", "panel_password"):
                if d.get(k):
                    config[k] = d[k]
            xui.logged = False
            state["warned"] = False
            state["action_done"] = False
            save_json(CONFIG_FILE, config)
            save_json(STATE_FILE, state)
        return jsonify(msg="تنظیمات ذخیره شد")
    except Exception as e:
        return jsonify(msg="خطا: " + str(e)), 400


@app.route("/api/reset", methods=["POST"])
@login_required
def api_reset():
    with lock:
        reset_usage("دستی از پنل")
    return jsonify(msg="ok")


if __name__ == "__main__":
    threading.Thread(target=monitor_loop, daemon=True).start()
    app.run(host=config.get("listen", "0.0.0.0"), port=int(config.get("port", 8088)))
