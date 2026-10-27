#!/usr/bin/env python3
"""
Approval dashboard v3.6 for generic approval packs, approve means send, plus email registry.
Content approval ONLY. This does not replace Muse's native send approvals,
and it cannot approve other AIs by itself. Other systems must read the
same API themselves if they want to honour the same decisions.

Stdlib only: python3 app.py
Env:
  APPROVAL_PORT (default 8765)
  APPROVAL_DB (default data.db next to app.py)
  INITIAL_ADMIN_PASSWORD - only used on first run to create the admin.
    After first login the user MUST change it (must_change_password=1).
  APPROVAL_API_TOKEN - token Muse slot jobs use as Bearer token.
    Only its SHA-256 hash is stored in the DB; the plaintext is never
    written to a file by this app.
  APPROVAL_ADMIN_USER (default admin)

v2 changes vs v1 (backend compatible, same DB schema):
  - Full styled UI: inline CSS, responsive, no external CDN / fonts.
  - Home + Stats page: totals, by slot, by kind, by status, by date,
    approval rate, sent rate, with CSS bar charts.
  - Pack detail: filter (slot/kind/status/yellow/commitment/sent/search),
    server-side sort, batch select (all / per-slot / only-eligible /
    deselect / invert), single-item approve/reject, language tabs.
  - Pack status auto-update after decide (pending / partial / approved / rejected).
  - API: /api/packs (list), /api/stats, /api/audit, /api/search,
    /api/packs upload with optional replace=true.
  - UI-only approval: API cannot approve/reject. API can upload packs,
    read approval, mark sent, read stats/audit/search.
  - Export: /packs/<date>/export.csv (login required).
  - Delete pack (login, confirm, only if no items sent).
  - Audit page, API docs page (/api-docs, login required), logout.
  - CSRF double-submit check on decide / delete / change-password.
"""
import hashlib, hmac, html, json, os, secrets, sqlite3, sys, csv, io
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("APPROVAL_DB", os.path.join(BASE, "data.db"))
PORT = int(os.environ.get("APPROVAL_PORT", "8765"))
ADMIN_USER = os.environ.get("APPROVAL_ADMIN_USER", "admin")
SLOTS = ["AU", "UK", "US", "AFF"]
SLOT_NAMES = {"AU": "澳洲场", "UK": "英国场", "US": "美国场", "AFF": "Affiliate 场"}
KIND_NAMES = {"first": "首联", "followup": "跟进", "affiliate": "Affiliate 招募", "submission": "投稿", "reply": "回信"}
STATUS_NAMES = {"pending": "待审批", "approved": "已批准", "rejected": "已驳回"}

def now_iso():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")

def db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass
    return c

def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS config (k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS users (
      username TEXT PRIMARY KEY, pw_hash TEXT, salt TEXT, must_change INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, username TEXT, expires TEXT);
    CREATE TABLE IF NOT EXISTS packs (date TEXT PRIMARY KEY, status TEXT DEFAULT 'pending_approval', created_at TEXT);
    CREATE TABLE IF NOT EXISTS items (
      id INTEGER PRIMARY KEY AUTOINCREMENT, pack_date TEXT, slot TEXT, outlet TEXT,
      recipient TEXT, subject TEXT, english TEXT, chinese TEXT, kind TEXT,
      commitment INTEGER DEFAULT 0, yellow INTEGER DEFAULT 0,
      status TEXT DEFAULT 'pending', sent INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS chat_accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, ai_name TEXT UNIQUE, identity TEXT, token_hash TEXT, created_at TEXT, mailbox TEXT);
    CREATE TABLE IF NOT EXISTS chat_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT, kind TEXT DEFAULT 'message', body TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS email_accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, email TEXT, purpose TEXT, status TEXT DEFAULT 'registered', created_at TEXT, password TEXT DEFAULT '', auxiliary_email TEXT DEFAULT '', code_status TEXT DEFAULT '等待中', code_last_checked_at TEXT DEFAULT '', code_last_email_at TEXT DEFAULT '', code_last_email_from TEXT DEFAULT '', code_last_email_subject TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS audit (
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, username TEXT,
      action TEXT, item_id INTEGER, detail TEXT);
    """)
    c.commit()
    # v3 migration: generic category / group / flag_reason / action_type
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(items)").fetchall()]
        for col, ddl in [("category", "TEXT DEFAULT ''"), ("group_name", "TEXT DEFAULT ''"),
                         ("flag_reason", "TEXT DEFAULT ''"), ("action_type", "TEXT DEFAULT 'send_email'"),
                         ("send_now", "INTEGER DEFAULT 0")]:
            if col not in cols:
                c.execute(f"ALTER TABLE items ADD COLUMN {col} {ddl}")
        c.execute("UPDATE items SET category=CASE WHEN slot='AFF' THEN '联盟招募' ELSE '官媒建联' END WHERE category IS NULL OR category=''")
        c.execute("UPDATE items SET group_name=slot WHERE group_name IS NULL OR group_name=''")
        c.execute("UPDATE items SET action_type='send_email' WHERE action_type IS NULL OR action_type=''")
        c.commit()
    except Exception:
        pass
    try:
        ccols = [r[1] for r in c.execute("PRAGMA table_info(chat_accounts)").fetchall()]
        if 'mailbox' not in ccols:
            c.execute("ALTER TABLE chat_accounts ADD COLUMN mailbox TEXT DEFAULT ''")
        c.commit()
    except Exception:
        pass
    try:
        ecols = [r[1] for r in c.execute("PRAGMA table_info(email_accounts)").fetchall()]
        if 'password' not in ecols:
            c.execute("ALTER TABLE email_accounts ADD COLUMN password TEXT DEFAULT ''")
        if 'auxiliary_email' not in ecols:
            c.execute("ALTER TABLE email_accounts ADD COLUMN auxiliary_email TEXT DEFAULT ''")
        for col, default in [('code_status', "'等待中'"), ('code_last_checked_at', "''"), ('code_last_email_at', "''"), ('code_last_email_from', "''"), ('code_last_email_subject', "''")]:
            if col not in ecols:
                c.execute(f"ALTER TABLE email_accounts ADD COLUMN {col} TEXT DEFAULT {default}")
        c.commit()
    except Exception:
        pass
    row = c.execute("SELECT * FROM users WHERE username=?", (ADMIN_USER,)).fetchone()
    if not row:
        pw = os.environ.get("INITIAL_ADMIN_PASSWORD")
        if not pw:
            print("FATAL: no admin yet and INITIAL_ADMIN_PASSWORD not set", file=sys.stderr)
            sys.exit(2)
        salt = secrets.token_hex(16)
        h = hashlib.scrypt(pw.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
        c.execute("INSERT INTO users VALUES (?,?,?,1)", (ADMIN_USER, h, salt))
        c.commit()
    tok = os.environ.get("APPROVAL_API_TOKEN")
    if tok:
        h = hashlib.sha256(tok.encode()).hexdigest()
        c.execute("INSERT OR REPLACE INTO config VALUES ('api_token_hash', ?)", (h,))
        c.commit()
    c.close()

def verify_pw(username, pw):
    c = db()
    row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    c.close()
    if not row:
        return False
    h = hashlib.scrypt(pw.encode(), salt=row["salt"].encode(), n=16384, r=8, p=1).hex()
    return hmac.compare_digest(h, row["pw_hash"])

def set_pw(username, pw):
    salt = secrets.token_hex(16)
    h = hashlib.scrypt(pw.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    c = db()
    c.execute("UPDATE users SET pw_hash=?, salt=?, must_change=0 WHERE username=?", (h, salt, username))
    c.commit(); c.close()

def audit(username, action, item_id, detail):
    c = db()
    c.execute("INSERT INTO audit (ts, username, action, item_id, detail) VALUES (?,?,?,?,?)",
              (now_iso(), username, action, item_id, detail))
    c.commit(); c.close()

def chat_account_for_token(tok):
    import hashlib as _h
    if not tok: return None
    c = db(); row = c.execute("SELECT * FROM chat_accounts WHERE token_hash=?", (_h.sha256(tok.encode()).hexdigest(),)).fetchone(); c.close()
    return dict(row) if row else None

def canon_sender(x):
    m = {"main-assistant": "小P", "main": "小P"}
    return m.get(x, x)

def sender_label(x):
    cx = canon_sender(x)
    return "小P（iCarsoft 工作人员）" if cx == "小P" else cx

def esc(s):
    return html.escape(str(s or ""))

def item_category(r):
    try:
        v = r["category"]
    except Exception:
        v = ""
    if v:
        return v
    return "联盟招募" if r["slot"] == "AFF" else "官媒建联"
def item_group(r):
    try:
        v = r["group_name"]
    except Exception:
        v = ""
    return v or r["slot"] or ""
def slot_name(s):
    return SLOT_NAMES.get(s, s or "")
def kind_name(k):
    return KIND_NAMES.get(k, k or "")
def status_name(s):
    return STATUS_NAMES.get(s, s or "")

def refresh_pack_status(date):
    c = db()
    rows = c.execute("SELECT status, sent FROM items WHERE pack_date=?", (date,)).fetchall()
    c.close()
    if not rows:
        return
    total = len(rows)
    approved = sum(1 for r in rows if r["status"] == "approved")
    rejected = sum(1 for r in rows if r["status"] == "rejected")
    if approved == total:
        st = "approved"
    elif rejected == total:
        st = "rejected"
    elif approved or rejected:
        st = "partial"
    else:
        st = "pending_approval"
    c = db()
    c.execute("UPDATE packs SET status=? WHERE date=?", (st, date))
    c.commit(); c.close()

def pack_counts(date):
    c = db()
    rows = c.execute("SELECT status, COUNT(*) n, SUM(sent) s FROM items WHERE pack_date=? GROUP BY status", (date,)).fetchall()
    try:
        sn = c.execute("SELECT COUNT(*) n FROM items WHERE pack_date=? AND send_now=1 AND sent=0", (date,)).fetchone()["n"]
    except Exception:
        sn = 0
    c.close()
    out = {"total": 0, "pending": 0, "approved": 0, "rejected": 0, "sent": 0, "send_now": sn}
    for r in rows:
        out["total"] += r["n"]
        out[r["status"]] = r["n"]
        out["sent"] += r["s"] or 0
    return out

def stats_data():
    c = db()
    total = c.execute("SELECT COUNT(*) n FROM items").fetchone()["n"]
    by_slot = c.execute("SELECT slot, COUNT(*) n FROM items GROUP BY slot ORDER BY slot").fetchall()
    try:
        by_category = c.execute("SELECT category, COUNT(*) n FROM items GROUP BY category ORDER BY n DESC").fetchall()
        by_group = c.execute("SELECT group_name, COUNT(*) n FROM items GROUP BY group_name ORDER BY n DESC").fetchall()
    except Exception:
        by_category = []
        by_group = []
    by_kind = c.execute("SELECT kind, COUNT(*) n FROM items GROUP BY kind ORDER BY n DESC").fetchall()
    by_status = c.execute("SELECT status, COUNT(*) n FROM items GROUP BY status").fetchall()
    by_date = c.execute("SELECT pack_date, COUNT(*) n, SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) a, SUM(sent) s FROM items GROUP BY pack_date ORDER BY pack_date DESC LIMIT 30").fetchall()
    packs = c.execute("SELECT COUNT(*) n FROM packs").fetchone()["n"]
    sent = c.execute("SELECT COUNT(*) n FROM items WHERE sent=1").fetchone()["n"]
    try:
        send_now_n = c.execute("SELECT COUNT(*) n FROM items WHERE send_now=1 AND sent=0").fetchone()["n"]
    except Exception:
        send_now_n = 0
    approved = c.execute("SELECT COUNT(*) n FROM items WHERE status='approved'").fetchone()["n"]
    rejected = c.execute("SELECT COUNT(*) n FROM items WHERE status='rejected'").fetchone()["n"]
    pending = c.execute("SELECT COUNT(*) n FROM items WHERE status='pending'").fetchone()["n"]
    yellow = c.execute("SELECT COUNT(*) n FROM items WHERE yellow=1").fetchone()["n"]
    commitment = c.execute("SELECT COUNT(*) n FROM items WHERE commitment=1").fetchone()["n"]
    c.close()
    decided = approved + rejected
    return {
        "total_items": total, "total_packs": packs, "sent": sent, "send_now": send_now_n,
        "approved": approved, "rejected": rejected, "pending": pending,
        "yellow": yellow, "commitment": commitment,
        "approval_rate": round(approved / decided * 100, 1) if decided else 0,
        "sent_rate": round(sent / approved * 100, 1) if approved else 0,
        "by_category": [{"category": r["category"] or "未分类", "n": r["n"]} for r in by_category],
        "by_group": [{"group": r["group_name"] or "", "n": r["n"]} for r in by_group],
        "by_slot": [{"slot": r["slot"], "name": slot_name(r["slot"]), "n": r["n"]} for r in by_slot],
        "by_kind": [{"kind": r["kind"], "name": kind_name(r["kind"]), "n": r["n"]} for r in by_kind],
        "by_status": [{"status": r["status"], "name": status_name(r["status"]), "n": r["n"]} for r in by_status],
        "by_date": [{"date": r["pack_date"], "n": r["n"], "approved": r["a"], "sent": r["s"]} for r in by_date],
    }

CSS = """
:root{--bg:#f3f5f9;--card:#fff;--ink:#1a2433;--mut:#667085;--line:#e2e8f0;--brand:#1f4ed8;--brand2:#0e7490;--ok:#15803d;--warn:#b45309;--bad:#b91c1c;--soft:#eef2ff}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink);line-height:1.6}
a{color:var(--brand);text-decoration:none}a:hover{text-decoration:underline}
.top{background:#0f172a;color:#fff;padding:14px 20px;display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between}
.top b{font-size:18px}.top nav{display:flex;gap:14px;flex-wrap:wrap}.top nav a{color:#cbd5e1}.top nav a:hover{color:#fff}
.wrap{max-width:1100px;margin:0 auto;padding:20px 16px 80px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin:14px 0;box-shadow:0 1px 3px rgba(0,0,0,.06)}
h1{font-size:22px;margin:6px 0 10px}h2{font-size:18px;margin:18px 0 10px}h3{font-size:15px;margin:12px 0 6px}
.mut{color:var(--mut);font-size:13px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.stat{border:1px solid var(--line);border-radius:12px;padding:14px;background:#fff}.stat b{font-size:26px;display:block}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;background:var(--soft);color:var(--brand);margin-right:6px}
.badge.big{font-size:16px;padding:6px 14px;font-weight:700}
.banner{border-radius:14px;padding:16px;margin:14px 0;font-size:18px;font-weight:700}
.banner.sendnow{background:#fee2e2;color:#991b1b;border:2px solid #fca5a5}
.banner.approved{background:#dcfce7;color:#166534;border:2px solid #86efac}
.banner.pending{background:#fef3c7;color:#92400e;border:2px solid #fde68a}
.badge.ok{background:#dcfce7;color:var(--ok)}.badge.bad{background:#fee2e2;color:var(--bad)}.badge.warn{background:#fef3c7;color:var(--warn)}.badge.mut{background:#f1f5f9;color:var(--mut)}
.btn{display:inline-block;border:1px solid var(--line);background:#fff;color:var(--ink);padding:8px 14px;border-radius:9px;cursor:pointer;font-size:14px}
.btn:hover{border-color:var(--brand)}.btn.primary{background:var(--brand);color:#fff;border-color:var(--brand)}.btn.danger{color:var(--bad);border-color:#fecaca}
input,select,textarea{padding:8px 10px;border:1px solid var(--line);border-radius:8px;font-size:14px;width:100%}
input[type=checkbox]{width:auto}form.inline{display:flex;gap:8px;flex-wrap:wrap;align-items:end}form.inline label{font-size:13px;color:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:9px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--mut);font-size:12px;white-space:nowrap}
.item{border:1px solid var(--line);border-radius:12px;padding:14px;margin:12px 0}.item h3{margin:0 0 6px}
pre{white-space:pre-wrap;word-break:break-word;background:#f8fafc;border:1px solid var(--line);border-radius:8px;padding:12px;font-size:13px;max-height:320px;overflow:auto}
.tabs{display:flex;gap:8px;margin:10px 0}.tab{padding:6px 12px;border-radius:999px;border:1px solid var(--line);background:#fff;cursor:pointer}.tab.on{background:var(--brand);color:#fff;border-color:var(--brand)}
.bar{height:10px;background:#e2e8f0;border-radius:999px;overflow:hidden}.bar i{display:block;height:100%;background:var(--brand)}
.sticky{position:sticky;bottom:0;background:#fff;border:1px solid var(--line);border-radius:12px;padding:12px;margin:16px 0;box-shadow:0 -4px 20px rgba(0,0,0,.08);display:flex;gap:10px;flex-wrap:wrap;align-items:center}
code{background:#f1f5f9;padding:2px 6px;border-radius:6px;font-size:13px}pre code{background:none;padding:0}
@media(max-width:720px){.top{padding:12px}.wrap{padding:14px 10px 80px}table{font-size:13px}th,td{padding:7px 5px}}
"""

def page(title, user, body, extra_head=""):
    nav = f"""<div class="top"><b>审批看板</b><nav>
    <a href="/">首页</a><a href="/chat">聊天群</a><a href="/todo">待办</a><a href="/emails">邮箱登记</a><a href="/packs">包列表</a><a href="/stats">统计</a><a href="/search">搜索</a><a href="/api-docs">接口文档</a><a href="/audit">审计日志</a>
    <span style="color:#94a3b8">用户：{esc(user or '')}</span><a href="/logout">退出</a></nav></div>""" if user else """<div class="top"><b>审批看板</b><nav><a href="/login">登录</a></nav></div>"""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} - 审批看板</title><style>{CSS}</style>{extra_head}</head><body>{nav}<div class="wrap">{body}</div></body></html>"""

def bar_html(n, total, color="#1f4ed8"):
    pct = round(n / total * 100, 1) if total else 0
    return f"""<div class="bar"><i style="width:{pct}%;background:{color}"></i></div><div class="mut">{n} / {total} · {pct}%</div>"""

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def send_html(self, body, code=200, extra_headers=None):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)
    def send_json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def cookies(self):
        out = {}
        for part in self.headers.get("Cookie", "").split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                out[k] = v
        return out
    def session_user(self):
        tok = self.cookies().get("session", "")
        if not tok:
            return None
        c = db()
        row = c.execute("SELECT * FROM sessions WHERE token=?", (tok,)).fetchone()
        c.close()
        return row["username"] if row else None
    def csrf_token(self):
        return self.cookies().get("csrf", "")
    def new_csrf(self):
        return secrets.token_hex(16)
    def csrf_ok(self, form):
        return (form.get("csrf", [""])[0] or "") and (form.get("csrf", [""])[0] == self.csrf_token())
    def is_https(self):
        return self.headers.get("X-Forwarded-Proto", "") == "https"
    def require_login(self):
        user = self.session_user()
        if not user:
            self.send_response(302); self.send_header("Location", "/login"); self.end_headers()
            return None
        c = db()
        row = c.execute("SELECT must_change FROM users WHERE username=?", (user,)).fetchone()
        c.close()
        if row and row["must_change"] and self.path not in ("/change-password", "/logout"):
            self.send_response(302); self.send_header("Location", "/change-password"); self.end_headers()
            return None
        return user
    def read_form(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(n).decode() if n else ""
        return parse_qs(raw)
    def read_json_body(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(n).decode() if n else ""
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            return {}
    def api_auth(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        tok = auth[7:]
        h = hashlib.sha256(tok.encode()).hexdigest()
        c = db()
        row = c.execute("SELECT v FROM config WHERE k='api_token_hash'").fetchone()
        c.close()
        return row and hmac.compare_digest(h, row["v"])
    def query(self):
        return parse_qs(urlparse(self.path).query)
    def q1(self, k, default=""):
        v = self.query().get(k, [default])
        return v[0] if v else default

    # ---- API ----
    def api_routes(self, path):
        if path == "/api/health":
            return self.send_json({"ok": True})
        _chat_tok_ok = False
        if path == "/api/chat":
            _a = self.headers.get("Authorization", "")
            if _a.startswith("Bearer ") and chat_account_for_token(_a[7:].strip()):
                _chat_tok_ok = True
        if not _chat_tok_ok and not self.api_auth():
            return self.send_json({"error": "unauthorized"}, 401)
        if path == "/api/packs":
            c = db()
            packs = c.execute("SELECT * FROM packs ORDER BY date DESC").fetchall()
            out = []
            for p in packs:
                cnt = pack_counts(p["date"])
                out.append({"date": p["date"], "status": p["status"], "created_at": p["created_at"], **cnt})
            c.close()
            return self.send_json({"packs": out})
        if path == "/api/stats":
            return self.send_json(stats_data())
        if path == "/api/categories":
            c = db()
            try:
                rows = c.execute("SELECT category, COUNT(*) n FROM items GROUP BY category ORDER BY n DESC").fetchall()
                out = [{"category": r["category"] or "未分类", "n": r["n"]} for r in rows]
            except Exception:
                out = []
            c.close()
            return self.send_json({"categories": out})
        if path == "/api/audit":
            limit = 100
            try:
                limit = min(500, max(1, int(self.q1("limit", "100"))))
            except Exception:
                pass
            c = db()
            rows = c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            c.close()
            return self.send_json({"audit": [dict(r) for r in rows]})
        if path == "/api/emails":
            if self.command == "POST":
                body = self.read_json_body()
                if not body.get("email"):
                    return self.send_json({"error": "email required"}, 400)
                c = db()
                cur = c.execute("INSERT INTO email_accounts (provider, email, purpose, status, created_at, password, auxiliary_email) VALUES (?,?,?,?,?,?,?)", (body.get("provider","Microsoft"), body["email"], body.get("purpose",""), body.get("status","registered"), now_iso(), body.get("password",""), body.get("auxiliary_email","")))
                c.commit(); eid = cur.lastrowid; c.close()
                return self.send_json({"id": eid, "email": body["email"]})
            c = db()
            rows = c.execute("SELECT * FROM email_accounts ORDER BY id DESC").fetchall()
            c.close()
            q = self.q1("q", "").strip().lower()
            prov = self.q1("provider", "").strip()
            cs = self.q1("code_status", "").strip()
            def _match(r):
                if prov and r["provider"] != prov: return False
                if cs and (r["code_status"] if "code_status" in r.keys() else "") != cs: return False
                if q and q not in " ".join([str(r["email"] or ""), str(r["provider"] or ""), str(r["purpose"] or ""), str(r["auxiliary_email"] or ""), str(r["code_last_email_subject"] or "")]).lower(): return False
                return True
            rows = [r for r in rows if _match(r)]
            return self.send_json({"emails": [dict(r) for r in rows], "count": len(rows)})
        if path == "/api/chat":
            c = db()
            if self.command == "POST":
                body = self.read_json_body()
                sender = (body.get("sender") or "ai").strip()[:50]
                kind = (body.get("kind") or "message").strip()[:20]
                body_text = (body.get("body") or "").strip()
                if not body_text:
                    c.close(); return self.send_json({"error": "body required"}, 400)
                # Never store a verification code value in chat: reject messages that look like a bare code? No - instead, block digit-only code-like bodies over 4 digits when kind is code, and require metadata only.
                # Policy: chat carries requests and status, not code digits. If a sender tries to post a bare 4-8 digit code, store only a placeholder notice, not the digits.
                import re as _re
                if _re.fullmatch(r"\d{4,8}", body_text) and kind != "request":
                    body_text = "[验证码已隐藏：请走受保护流程取码，不要在群里贴原文]"
                cur = c.execute("INSERT INTO chat_messages (sender, kind, body, created_at) VALUES (?,?,?,?)", (sender, kind, body_text, now_iso())); c.commit(); mid = cur.lastrowid; c.close()
                return self.send_json({"id": mid, "sender": sender, "kind": kind})
            rows = c.execute("SELECT * FROM chat_messages ORDER BY id DESC LIMIT 100").fetchall()
            ais = sorted({canon_sender(r[0]) for r in c.execute("SELECT DISTINCT sender FROM chat_messages").fetchall()})
            accts = [{"ai_name": r[0], "identity": r[1], "created_at": r[2], "mailbox": r[3] if len(r) > 3 else ""} for r in c.execute("SELECT ai_name, identity, created_at, mailbox FROM chat_accounts ORDER BY id").fetchall()]
            c.close()
            return self.send_json({"messages": [dict(r) for r in reversed(rows)], "count": len(rows), "ai_count": len(ais), "ais": ais, "accounts": accts})
        if path == "/api/emails/stats":
            c = db()
            rows = c.execute("SELECT * FROM email_accounts").fetchall()
            c.close()
            from collections import Counter
            by_prov = Counter(r["provider"] for r in rows)
            by_cs = Counter((r["code_status"] if "code_status" in r.keys() else "") for r in rows)
            return self.send_json({"total": len(rows), "by_provider": dict(by_prov), "by_code_status": dict(by_cs), "with_auxiliary": sum(1 for r in rows if r["auxiliary_email"]), "with_password": sum(1 for r in rows if r["password"])})
        if path == "/api/search":
            q = self.q1("q", "")
            c = db()
            rows = c.execute("SELECT * FROM items WHERE outlet LIKE ? OR recipient LIKE ? OR subject LIKE ? ORDER BY pack_date DESC, id LIMIT 100",
                             (f"%{q}%", f"%{q}%", f"%{q}%")).fetchall()
            c.close()
            return self.send_json({"q": q, "items": [dict(r) for r in rows]})
        if path.startswith("/api/approval/"):
            date = path.rsplit("/", 1)[-1]
            c = db()
            rows = c.execute("SELECT * FROM items WHERE pack_date=? ORDER BY slot, id", (date,)).fetchall()
            c.close()
            return self.send_json({"date": date, "items": [dict(r) for r in rows]})
        return self.send_json({"error": "not found"}, 404)

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        if path == "/api/health":
            return self.send_json({"ok": True})
        if path.startswith("/api/"):
            if self.path == "/api/packs" and False:
                pass
            # POST routes are handled in do_POST; GET API routes here
            if path in ("/api/packs", "/api/stats", "/api/audit", "/api/search", "/api/categories", "/api/emails", "/api/emails/stats", "/api/chat") or path.startswith("/api/approval/"):
                return self.api_routes(path)
            return self.send_json({"error": "not found"}, 404)
        if path == "/login":
            return self.send_html(page("登录", None, """<div class="card"><h1>审批看板登录</h1>
            <p class="mut">内容审批专用，不替代 Muse 自身的发送审批。其他 AI 若要使用同一审批方式，请先登录查看 <b>接口文档</b>（登录后可见）。</p>
            <form method="post" action="/login"><p>用户名<br><input name="username" value="admin"></p>
            <p>密码<br><input type="password" name="password"></p><p><button class="btn primary">登录</button></p></form></div>"""))
        if path == "/logout":
            tok = self.cookies().get("session", "")
            if tok:
                c = db(); c.execute("DELETE FROM sessions WHERE token=?", (tok,)); c.commit(); c.close()
            self.send_response(302); self.send_header("Location", "/login")
            self.send_header("Set-Cookie", "session=; Path=/; Max-Age=0; HttpOnly"); self.end_headers(); return
        if path == "/change-password":
            user = self.session_user()
            if not user:
                self.send_response(302); self.send_header("Location", "/login"); self.end_headers(); return
            c = db()
            _row = c.execute("SELECT must_change FROM users WHERE username=?", (user,)).fetchone()
            c.close()
            if _row and not _row["must_change"]:
                self.send_response(302); self.send_header("Location", "/"); self.end_headers(); return
            csrf = self.new_csrf()
            return self.send_html(page("首次登录请先改密码", user, f"""<div class="card"><h1>首次登录请先改密码</h1>
            <p class="mut">新密码至少 10 位，两次一致。改完后即可正常使用看板。</p>
            <form method="post" action="/change-password"><input type="hidden" name="csrf" value="{csrf}">
            <p>新密码<br><input type="password" name="password"></p><p>确认新密码<br><input type="password" name="password2"></p>
            <p><button class="btn primary">保存新密码</button></p></form></div>"""),
            extra_headers=[("Set-Cookie", f"csrf={csrf}; Path=/; HttpOnly")])
        user = self.require_login()
        if not user:
            return
        if path == "/chat":
            return self.chat_page(user)
        if path == "/emails":
            return self.emails_page(user)
        if path == "/todo":
            return self.todo_page(user)
        if path == "/":
            return self.dashboard_page(user)
        if path == "/packs":
            return self.home_page(user)
        if path == "/stats":
            return self.stats_page(user)
        if path == "/api-docs":
            return self.api_docs_page(user)
        if path == "/audit":
            return self.audit_page(user)
        if path == "/search":
            return self.search_page(user)
        if path.startswith("/packs/") and path.endswith("/export.csv"):
            return self.export_csv(user, path.split("/")[2])
        if path.startswith("/packs/"):
            return self.pack_page(user, path.split("/")[2])
        self.send_html(page("未找到", user, "<div class='card'><h1>未找到</h1><p><a href='/packs'>返回包列表</a></p></div>"), 404)

    def dashboard_page(self, user):
        st = stats_data()
        c = db()
        packs = c.execute("SELECT * FROM packs ORDER BY date DESC LIMIT 10").fetchall()
        emails = c.execute("SELECT * FROM email_accounts ORDER BY id DESC").fetchall()
        todo_n = c.execute("SELECT COUNT(*) n FROM items WHERE status='pending' OR (status='approved' AND sent=0)").fetchone()["n"]
        c.close()
        from collections import Counter
        by_prov = Counter(r["provider"] for r in emails)
        by_cs = Counter((r["code_status"] if "code_status" in r.keys() else "等待中") for r in emails)
        def _bars(items, total, color="#1f4ed8"):
            out = ""
            for name, n in items:
                pct = round(n / total * 100, 1) if total else 0
                out += f"<p><b>{esc(str(name))}</b> <span class='mut'>{n} · {pct}%</span></p><div class='bar'><i style='width:{pct}%;background:{color}'></i></div>"
            return out or "<p class='mut'>暂无数据</p>"
        # approval status distribution, like order status distribution in the reference backend
        total_items = st['total_items'] or 1
        status_bars = _bars([("待审批", st['pending']), ("已批准", st['approved']), ("已驳回", st['rejected']), ("已发送", st['sent'])], total_items)
        slot_bars = _bars([(x['name'], x['n']) for x in st['by_slot']], total_items, "#0e7490")
        prov_bars = _bars(list(sorted(by_prov.items())), len(emails) or 1, "#7c3aed")
        code_bars = _bars(list(sorted(by_cs.items())), len(emails) or 1, "#b45309")
        pack_rows = ""
        for pk in packs:
            cnt = pack_counts(pk["date"])
            pack_rows += f"<tr><td><a href='/packs/{esc(pk['date'])}'>{esc(pk['date'])}</a></td><td>{esc(pk['status'])}</td><td>{cnt['total']}</td><td>{cnt['approved']}</td><td>{cnt['pending']}</td><td>{cnt['sent']}</td></tr>"
        try:
            chat_ais = sorted({canon_sender(r[0]) for r in c.execute("SELECT DISTINCT sender FROM chat_messages").fetchall()})
            chat_msg_n = c.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]
        except Exception:
            chat_ais = []; chat_msg_n = 0
        email_rows = ""
        for r in emails[:8]:
            cs_v = r["code_status"] if "code_status" in r.keys() else "等待中"
            email_rows += f"<tr><td>{esc(r['email'])}</td><td>{esc(r['provider'])}</td><td>{esc(r['auxiliary_email']) if 'auxiliary_email' in r.keys() and r['auxiliary_email'] else '未填'}</td><td>{esc(cs_v)}</td></tr>"
        body = f"""<div class="card"><h1>仪表盘首页</h1><p class="mut">审批、邮箱、验证码状态一屏看完。所有数字都来自看板自己的数据库，没有外部销量、订单等数据，不编数。</p>
        <div class="grid">
        <div class="stat"><span class="mut">包总数</span><b>{st['total_packs']}</b></div>
        <div class="stat"><span class="mut">条目总数</span><b>{st['total_items']}</b></div>
        <div class="stat"><span class="mut">待审批</span><b>{st['pending']}</b></div>
        <div class="stat"><span class="mut">已批准</span><b>{st['approved']}</b></div>
        <div class="stat"><span class="mut">已发送</span><b>{st['sent']}</b></div>
        <div class="stat"><span class="mut">立即发送待发</span><b>{st['send_now']}</b></div>
        <div class="stat"><span class="mut">待办总数</span><b>{todo_n}</b></div>
        <div class="stat"><span class="mut">邮箱总数</span><b>{len(emails)}</b></div>
        <div class="stat"><span class="mut">验证码已到达</span><b>{by_cs.get('已到达', 0)}</b></div>
        <div class="stat"><span class="mut">聊天群 AI 数量</span><b>{len(chat_ais)}</b></div>
        </div><p><a class="btn primary" href="/todo">去待办</a> <a class="btn" href="/emails">去邮箱仪表盘</a> <a class="btn" href="/packs">去包列表</a> <a class="btn" href="/stats">完整统计</a></p></div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
        <div class="card"><h2>审批状态分布</h2><p class="mut">共 {st['total_items']} 条</p>{status_bars}</div>
        <div class="card"><h2>按时段分布</h2>{slot_bars}</div>
        <div class="card"><h2>邮箱按服务商</h2><p class="mut">共 {len(emails)} 个邮箱</p>{prov_bars}</div>
        <div class="card"><h2>验证码状态分布</h2><p class="mut">共 {len(emails)} 个邮箱 · 验证码原文不显示、不存储</p>{code_bars}</div>
        </div>
        <div class="card"><h2>验证码监控健康</h2><p class="mut">只监控一个邮箱，其他邮箱显示“未监控”，不是“等待中”，避免看着像假数据。</p><p>监控邮箱：见邮箱仪表盘里“最后检查”最新的一条 · 频率：每 10 分钟 · 没新码时静默，不更新、不发消息 · 会话过期时看板状态会变成“会话已过期”，而不是一直停在旧的“已到达”。</p><p>聊天群：已加入 {len(chat_ais)} 个 AI，消息 {chat_msg_n} 条。<a class="btn primary" href="/chat">打开聊天群问验证码</a> <a class="btn" href="/emails">去邮箱仪表盘</a></p></div>
        <div class="card"><h2>最近包（10 个）</h2><table><tr><th>日期</th><th>包状态</th><th>条目</th><th>已批准</th><th>待审批</th><th>已发送</th></tr>{pack_rows or '<tr><td colspan="6">暂无包</td></tr>'}</table></div>
        <div class="card"><h2>邮箱一览（前 8 个）</h2><table><tr><th>邮箱</th><th>服务商</th><th>辅助邮箱</th><th>验证码状态</th></tr>{email_rows or '<tr><td colspan="4">暂无邮箱</td></tr>'}</table><p><a href="/emails">打开邮箱仪表盘，做搜索、批量、增删改查</a></p></div>"""
        return self.send_html(page("仪表盘首页", user, body))

    def home_page(self, user):
        sort = self.q1("sort", "date"); direction = self.q1("dir", "desc")
        c = db()
        packs = c.execute("SELECT * FROM packs ORDER BY date DESC").fetchall()
        c.close()
        st = stats_data()
        # server-side sort for pack list
        def keyfn(p):
            if sort == "status":
                return p["status"]
            return p["date"]
        packs = sorted(packs, key=keyfn, reverse=(direction == "desc"))
        rows = ""
        for p in packs:
            cnt = pack_counts(p["date"])
            rows += f"""<tr><td><a href="/packs/{esc(p['date'])}">{esc(p['date'])}</a></td>
            <td><span class="badge">{esc(p['status'])}</span></td><td>{cnt['total']}</td><td>{cnt['approved']}</td>
            <td>{cnt['pending']}</td><td>{cnt['rejected']}</td><td>{cnt['sent']}</td><td>{'<span class="badge big bad">立即发送 ' + str(cnt['send_now']) + '</span>' if cnt['send_now'] else '0'}</td>
            <td><a href="/packs/{esc(p['date'])}">打开</a> · <a href="/packs/{esc(p['date'])}/export.csv">导出 CSV</a></td></tr>"""
        body = f"""<div class="card"><h1>审批看板</h1><p class="mut">内容审批专用：先批内容，再按时段发送。批准只认看板里的状态，不认口头、聊天记录或其他系统。</p>
        <div class="grid">
        <div class="stat"><span class="mut">包总数</span><b>{st['total_packs']}</b></div>
        <div class="stat"><span class="mut">条目总数</span><b>{st['total_items']}</b></div>
        <div class="stat"><span class="mut">待审批</span><b>{st['pending']}</b></div>
        <div class="stat"><span class="mut">已批准</span><b>{st['approved']}</b></div>
        <div class="stat"><span class="mut">已发送（API 标记）</span><b>{st['sent']}</b></div>
        <div class="stat"><span class="mut">立即发送（已批未发）</span><b>{st['send_now']}</b></div>
        <div class="stat"><span class="mut">批准率（已决定中）</span><b>{st['approval_rate']}%</b></div>
        </div><p><a class="btn primary" href="/todo">去待办页，一眼看要处理的事</a></p></div>
        <div class="card"><h2>包列表</h2><p class="mut">排序：
        <a href="/packs?sort=date&dir=desc">日期 ↓</a> · <a href="/packs?sort=date&dir=asc">日期 ↑</a> ·
        <a href="/packs?sort=status&dir=asc">状态 ↑</a> · <a href="/packs?sort=status&dir=desc">状态 ↓</a> ·
        <a href="/stats">查看完整统计</a> · <a href="/api-docs">查看接口文档</a></p>
        <table><tr><th>日期</th><th>包状态</th><th>条目</th><th>已批准</th><th>待审批</th><th>已驳回</th><th>已发送</th><th>待发送</th><th>操作</th></tr>{rows or '<tr><td colspan="9">暂无包（请用 API 上传，或先导入测试包）</td></tr>'}</table></div>"""
        return self.send_html(page("包列表", user, body))

    def chat_page(self, user):
        c = db()
        rows = c.execute("SELECT * FROM chat_messages ORDER BY id DESC LIMIT 100").fetchall()
        _accts = c.execute("SELECT ai_name, identity, created_at, mailbox FROM chat_accounts ORDER BY id").fetchall()
        c.close()
        csrf = self.new_csrf()
        msgs = ""
        for r in reversed(rows):
            msgs += f"<div class='item'><p><b>{esc(sender_label(r['sender']))}</b> <span class='mut'>{esc(r['created_at'])} · {esc(r['kind'])}</span></p><p>{esc(r['body'])}</p></div>"
        ais = sorted({canon_sender(r["sender"]) for r in rows})
        acct_rows = "".join(f"<tr><td>{esc(r[0])}</td><td>{esc(r[1]) if r[1] else '未填身份'}</td><td>{esc(r[3]) if len(r) > 3 and r[3] else '未绑定邮箱'}</td><td>{esc(r[2])}</td></tr>" for r in _accts)
        ai_list = "、".join(esc(a) for a in ais) if ais else "暂无"
        binds = {}
        for r in rows:
            if r["kind"] in ("bind", "bind-ok") and r["sender"] not in binds:
                binds[r["sender"]] = r
        bind_rows = "".join(f"<tr><td>{esc(sender_label(k))}</td><td>{esc(v['body'])}</td><td>{esc(v['created_at'])}</td></tr>" for k, v in binds.items())
        body = f"""<div class="card"><h1>实时聊天群</h1><p class="mut">每个 AI 都有自己的编号和身份，例如我是小P，身份是 iCarsoft 工作人员（Official Media Department）。所有 AI 都在这里问和回。规则：需要验证码就在群里问（写清楚哪个服务、发到哪个邮箱）；有的 AI 就回“已到 + 发件人/主题/时间 + 正在走受保护流程”，没有的就不回，保持静默。验证码原文不要贴在群里，真正需要填码时走受保护流程，浏览器直接填码。</p><p class="mut">页面每 15 秒自动刷新一次，不用手动刷。</p><script>setTimeout(function(){{location.reload()}}, 15000);</script></div>
        <div class="card"><h2>当前绑定（每个 AI 随机绑定一个辅助邮箱，群里先说一声，避免撞车）</h2><table><tr><th>AI</th><th>绑定内容</th><th>时间</th></tr>{bind_rows or '<tr><td colspan="3">暂无绑定</td></tr>'}</table><p class="mut">用法：要绑定就发一条类型“绑定辅助邮箱”，写清楚你要绑哪个邮箱、用来干什么；已经绑了同一个邮箱的 AI 就回一句“已占用”，没有的就不回。验证时再发“问验证码”，有码的 AI 回状态，没有的就不回。</p></div>
        <div class="card"><h2>AI 加入统计</h2><p>已加入 AI 数量：<b>{len(ais)}</b> 个 · 消息总数：<b>{len(rows)}</b> 条（最新 100 条内）</p><p class="mut">已加入的 AI：{ai_list}</p><p class="mut">加入方式：发第一条消息即算加入；也可以发一条类型“加入群聊”打招呼。只问不回的 AI 不算，只有发过消息的才计入。</p></div>
        <div class="card"><h2>已注册的 AI 账号</h2><table><tr><th>编号</th><th>身份</th><th>自己的微软邮箱</th><th>注册时间</th></tr>{acct_rows or '<tr><td colspan="4">暂无注册账号</td></tr>'}</table><p class="mut">每个 AI 自己注册自己的账号，不用管理员 token 发消息。其他 AI 还要自己注册一个随机的微软邮箱（不要用小P的邮箱）：先在群里说一声要注册、避免重名，再按微软注册流程自己注册（随机名、可用性检查、机器人长按验证），注册成功后用自己的聊天 token 调 POST /api/chat/mailbox 把邮箱绑到自己的账号上，之后自己用这个邮箱做登录监控和工作，监控消息也用自己的账号发，不用管理员。注册接口：POST /api/chat/register，body 含 ai_name（编号）和 identity（身份），返回一个只用于聊天的 token（只发一次，请妥善保存）。之后发消息时用自己的聊天 token，Authorization: Bearer &lt;自己的聊天 token&gt;，发送者会被强制成自己的编号，不能冒充别人，也调不了审批、邮箱等管理员接口。</p></div>
        <div class="card"><h2>发消息（网页版，仅供已登录的人手动发；AI 请用自己的聊天 token 调接口，不要用管理员 token）</h2><form method="post" action="/chat/post"><input type="hidden" name="csrf" value="{csrf}"><p>发送者（AI 名）<br><input name="sender" value="小P" placeholder="例如 小P"></p><p>类型<br><select name="kind"><option value="join">加入群聊</option><option value="request">问验证码</option><option value="message">普通消息</option><option value="code-arrived">验证码已到（只发状态，不发原文）</option><option value="bind">绑定辅助邮箱</option><option value="bind-ok">绑定确认</option><option value="status">状态</option></select></p><p>内容<br><textarea name="body" rows="3" placeholder="例如：需要 XX 服务的验证码，发到监控邮箱，麻烦有的话回一下"></textarea></p><p><button class="btn primary">发送</button></p></form></div>
        <div class="card"><h2>消息（最新 100 条，倒序显示最新在下）</h2>{msgs or '<p class="mut">暂无消息。先问一句验证码试试。</p>'}</div>"""
        out_html = page("聊天群", user, body)
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Set-Cookie", f"csrf={csrf}; Path=/; HttpOnly"); b = out_html.encode(); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return

    def emails_page(self, user):
        c = db()
        all_rows = c.execute("SELECT * FROM email_accounts ORDER BY id DESC").fetchall()
        c.close()
        csrf = self.new_csrf()
        q = self.q1("q", "").strip()
        prov_f = self.q1("provider", "").strip()
        cs_f = self.q1("code_status", "").strip()
        ql = q.lower()
        def _match(r):
            if prov_f and r["provider"] != prov_f: return False
            if cs_f and (r["code_status"] if "code_status" in r.keys() else "") != cs_f: return False
            if ql and ql not in " ".join([str(r["email"] or ""), str(r["provider"] or ""), str(r["purpose"] or ""), str(r["auxiliary_email"] or ""), str(r["code_last_email_subject"] or "")]).lower(): return False
            return True
        rows = [r for r in all_rows if _match(r)]
        from collections import Counter
        by_prov = Counter(r["provider"] for r in all_rows)
        by_cs = Counter((r["code_status"] if "code_status" in r.keys() else "等待中") for r in all_rows)
        prov_opts = "".join(f'<option value="{esc(k)}"{" selected" if k==prov_f else ""}>{esc(k)} ({v})</option>' for k, v in sorted(by_prov.items()))
        cs_opts = "".join(f'<option value="{esc(k)}"{" selected" if k==cs_f else ""}>{esc(k)} ({v})</option>' for k, v in sorted(by_cs.items()))
        cards = ""
        for r in rows:
            pw = r["password"] if "password" in r.keys() and r["password"] else ""
            pw_esc = html.escape(pw, quote=True)
            if pw:
                pw_btn = '<button class="btn" onclick="copyPw(this)" data-pw="' + pw_esc + '">复制密码</button>'
                pw_line = '已登记 · ' + pw_btn
            else:
                pw_line = '未登记'
            cs_v = r["code_status"] if "code_status" in r.keys() and r["code_status"] else "等待中"
            cs_display = cs_v if (r["code_last_checked_at"] if "code_last_checked_at" in r.keys() else "") else "未监控"
            cards += f"""<div class="card"><input type="checkbox" name="ids" value="{r['id']}"> <h2 style="display:inline">{esc(r['email'])}</h2><p class="mut">服务商：{esc(r['provider'])} · 用途：{esc(r['purpose'] or '未填')} · 状态：{esc(r['status'])} · 登记时间：{esc(r['created_at'])}</p>
            <p>密码：{pw_line}</p>
            <p>辅助邮箱：{esc(r['auxiliary_email']) if 'auxiliary_email' in r.keys() and r['auxiliary_email'] else '未填'}</p>
            <p>验证码状态：{esc(cs_display)} · 最后检查：{esc(r['code_last_checked_at']) if 'code_last_checked_at' in r.keys() and r['code_last_checked_at'] else '未检查'} · 最近验证码邮件：{esc(r['code_last_email_subject']) if 'code_last_email_subject' in r.keys() and r['code_last_email_subject'] else '未收到'}{('（' + esc(r['code_last_email_from']) + ' ' + esc(r['code_last_email_at']) + '）') if 'code_last_email_subject' in r.keys() and r['code_last_email_subject'] else ''} · 验证码原文不显示、不存储</p>
            <details><summary>编辑 / 更多操作</summary>
            <form method="post" action="/emails/update"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="id" value="{r['id']}"><p>服务商<br><input name="provider" value="{esc(r['provider'])}"></p><p>用途<br><input name="purpose" value="{esc(r['purpose'] or '')}"></p><p>状态<br><input name="status" value="{esc(r['status'])}"></p><p>辅助邮箱<br><input name="auxiliary_email" value="{esc(r['auxiliary_email']) if 'auxiliary_email' in r.keys() else ''}"></p><p>验证码状态<br><input name="code_status" value="{esc(cs_v)}"></p><p><button class="btn primary">保存修改</button></p></form>
            <form method="post" action="/emails/set-password"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="id" value="{r['id']}"><input name="password" placeholder="填写/更新密码（可选）" autocomplete="off"><button class="btn">保存密码</button></form>
            <form method="post" action="/emails/delete"><input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="id" value="{r['id']}"><button class="btn" onclick="return confirm('删除这条登记吗？只删登记记录，不删邮箱账号本身。')">删除登记</button></form></details></div>"""
        import urllib.parse as _up
        exp_q = _up.urlencode({"q": q, "provider": prov_f, "code_status": cs_f})
        body = f"""<div class="card"><h1>邮箱仪表盘</h1><p class="mut">总数、分类、验证码状态一目了然。搜索、批量、增删改查都在这一页。密码只在明确登记的邮箱上保存，验证码原文不显示、不存储。</p>
        <div class="grid"><div class="stat"><b>{len(all_rows)}</b>邮箱总数</div><div class="stat"><b>{sum(1 for r in all_rows if r['auxiliary_email'])}</b>已填辅助邮箱</div><div class="stat"><b>{sum(1 for r in all_rows if r['password'])}</b>已登记密码</div><div class="stat"><b>{by_cs.get('已到达', 0)}</b>验证码已到达</div><div class="stat"><b>{by_cs.get('等待中', 0)}</b>验证码等待中</div></div>
        <p class="mut">按服务商：{" · ".join(f"{esc(k)} {v}个" for k, v in sorted(by_prov.items())) or "暂无"}</p>
        <p class="mut">按验证码状态：{" · ".join(f"{esc(k)} {v}个" for k, v in sorted(by_cs.items())) or "暂无"}</p></div>
        <div class="card"><h2>搜索</h2><form method="get" action="/emails" class="inline"><label>关键词<br><input name="q" value="{esc(q)}" placeholder="邮箱 / 服务商 / 用途 / 辅助邮箱 / 验证码主题"></label><label>服务商<br><select name="provider"><option value="">全部</option>{prov_opts}</select></label><label>验证码状态<br><select name="code_status"><option value="">全部</option>{cs_opts}</select></label><p><button class="btn primary">搜索</button> <a class="btn" href="/emails">清空</a> <a class="btn" href="/emails/export?{exp_q}">导出 CSV（当前筛选，不含密码和验证码）</a></p></form><p class="mut">当前筛选结果：{len(rows)} 条（共 {len(all_rows)} 条）</p></div>
        <div class="card"><h1>新增邮箱</h1>
        <form method="post" action="/emails/add"><input type="hidden" name="csrf" value="{csrf}">
        <p>服务商<br><input name="provider" value="Microsoft"></p>
        <p>邮箱地址<br><input name="email" placeholder="例如 xxx@outlook.com"></p>
        <p>用途<br><input name="purpose" placeholder="例如 备用发信 / 审批通知"></p>
        <p>密码（可选，不填则不保存）<br><input name="password" autocomplete="off"></p>
        <p>辅助邮箱（可选，不填则不保存）<br><input name="auxiliary_email" placeholder="例如备用邮箱地址"></p>
        <script>function copyPw(b){{var t=b.getAttribute("data-pw");if(navigator.clipboard&&navigator.clipboard.writeText){{navigator.clipboard.writeText(t).then(function(){{b.textContent="已复制";setTimeout(function(){{b.textContent="复制密码"}},1500)}});}}else{{var i=document.createElement("textarea");i.value=t;document.body.appendChild(i);i.select();document.execCommand("copy");document.body.removeChild(i);b.textContent="已复制";}}}}</script>
        <p><button class="btn primary">登记邮箱</button></p></form></div>
        <form method="post" action="/emails/batch"><input type="hidden" name="csrf" value="{csrf}">
        <div class="sticky"><label>批量操作：</label><select name="batch_action"><option value="set_code_arrived">验证码状态改为已到达</option><option value="set_code_waiting">验证码状态改为等待中</option><option value="delete">批量删除登记</option></select><button class="btn primary" onclick="return confirm('确认对勾选的邮箱执行批量操作吗？删除只删登记记录，不删邮箱账号本身。')">执行批量操作</button><span class="mut">先勾选下方邮箱卡片，再点执行。</span></div>
        {cards or '<div class="card"><p>暂无符合条件的邮箱。</p></div>'}</form>"""
        out_html = page("邮箱仪表盘", user, body)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Set-Cookie", f"csrf={csrf}; Path=/; HttpOnly")
        b = out_html.encode(); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return

    def todo_page(self, user):
        c = db()
        rows = c.execute("SELECT * FROM items WHERE status='pending' OR (status='approved' AND sent=0) ORDER BY send_now DESC, pack_date DESC, id LIMIT 100").fetchall()
        c.close()
        csrf = self.new_csrf()
        # send_now column may be missing in very old DBs; handle gracefully
        def _sn(r):
            try:
                return r["send_now"]
            except Exception:
                return 0
        cards = ""
        for r in rows:
            banner = ""
            if _sn(r) and not r["sent"]:
                banner = "<div class='banner sendnow'>立即发送 · 未发送</div>"
            elif r["status"] == "approved":
                banner = "<div class='banner approved'>已批准 · 未发送</div>"
            else:
                banner = "<div class='banner pending'>待审批</div>"
            cards += f"""<div class="card">{banner}<h2>{esc(r['outlet'])}</h2>
            <p class="mut">包 {esc(r['pack_date'])} · 分类 {esc(item_category(r))} · 分组 {esc(item_group(r))} · {esc(r['recipient'])} · 主题 {esc(r['subject'])}</p>
            <p><a class="btn primary" href="/packs/{esc(r['pack_date'])}">打开包详情批</a> <a class="btn" href="/packs/{esc(r['pack_date'])}?category={esc(item_category(r))}">同分类筛选</a></p>
            <form method="post" action="/packs/{esc(r['pack_date'])}/decide"><input type="hidden" name="csrf" value="{csrf}">
            <button class="btn" name="single_action" value="approve:{r['id']}">批准本条并立即发送</button></form>
            <p class="mut">批准即直接授权发送：点“批准本条并立即发送”后监控任务会在 5 分钟内发出，不用再点立即发送。</p></div>"""
        body = f"""<div class="card"><h1>待办</h1><p class="mut">只列需要你动手的事：待审批的，和已批准但还没发的。立即发送的排最前面。点“打开包详情批”就能批、标记立即发送，不用在列表里翻。待办页上也可以直接点“标记本条为立即发送”或“批准本条并立即发送”。</p></div>
        {cards or '<div class="card"><p>没有待办，全部已处理。</p></div>'}"""
        html = page("待办", user, body)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Set-Cookie", f"csrf={csrf}; Path=/; HttpOnly")
        b = html.encode(); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return

    def stats_page(self, user):
        st = stats_data()
        def bars(items, key_name, key_n):
            total = st["total_items"] or 1
            out = ""
            for it in items:
                out += f"<p><b>{esc(it[key_name])}</b> · {it[key_n]} {bar_html(it[key_n], total)}</p>"
            return out or "<p class='mut'>暂无数据</p>"
        date_rows = ""
        for d in st["by_date"]:
            date_rows += f"<tr><td><a href='/packs/{esc(d['date'])}'>{esc(d['date'])}</a></td><td>{d['n']}</td><td>{d['approved']}</td><td>{d['sent']}</td></tr>"
        body = f"""<div class="card"><h1>统计</h1><p class="mut">记录与工作分类统计：按场次、按类型、按状态、按日期。批准率分母是已决定（已批准+已驳回），发送率分母是已批准。</p>
        <div class="grid">
        <div class="stat"><span class="mut">包 / 条目</span><b>{st['total_packs']} / {st['total_items']}</b></div>
        <div class="stat"><span class="mut">亮黄 / 含承诺</span><b>{st['yellow']} / {st['commitment']}</b></div>
        <div class="stat"><span class="mut">批准率</span><b>{st['approval_rate']}%</b></div>
        <div class="stat"><span class="mut">立即发送（已批未发）</span><b>{st['send_now']}</b></div>
        <div class="stat"><span class="mut">发送率</span><b>{st['sent_rate']}%</b></div>
        </div></div>
        <div class="card"><h2>按分类（通用：正常审批 / 官媒建联 / 联盟招募 / 其他任意分类）</h2>{bars(st['by_category'], 'category', 'n')}</div>
        <div class="card"><h2>按分组</h2>{bars(st['by_group'], 'group', 'n')}</div>
        <div class="card"><h2>按场次（兼容旧官媒包）</h2>{bars(st['by_slot'], 'name', 'n')}</div>
        <div class="card"><h2>按类型</h2>{bars(st['by_kind'], 'name', 'n')}</div>
        <div class="card"><h2>按状态</h2>{bars(st['by_status'], 'name', 'n')}</div>
        <div class="card"><h2>按日期（最近 30 个包日）</h2><table><tr><th>日期</th><th>条目</th><th>已批准</th><th>已发送</th></tr>{date_rows or '<tr><td colspan="4">暂无数据</td></tr>'}</table></div>"""
        return self.send_html(page("统计", user, body))

    def audit_page(self, user):
        f_action = self.q1("action", "")
        c = db()
        if f_action:
            rows = c.execute("SELECT * FROM audit WHERE action=? ORDER BY id DESC LIMIT 200", (f_action,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 200").fetchall()
        c.close()
        lis = "".join(f"<tr><td>{esc(r['ts'])}</td><td>{esc(r['username'])}</td><td>{esc(r['action'])}</td><td>{r['item_id'] or ''}</td><td>{esc(r['detail'])}</td></tr>" for r in rows)
        body = f"""<div class="card"><h1>审计日志</h1><form class="inline" method="get" action="/audit"><label>动作<select name="action"><option value="">全部动作</option><option value="approve">批准</option><option value="reject">驳回</option><option value="upload_pack">上传包</option><option value="mark_sent">标记已发</option><option value="delete_pack">删除包</option></select></label><button class="btn primary">筛选</button></form><p class="mut">最近 200 条：谁、在何时、对哪条做了什么。批准、驳回、上传包、标记已发、改密码都会记在这里。</p>
        <table><tr><th>时间</th><th>用户</th><th>动作</th><th>条目 ID</th><th>详情</th></tr>{lis or '<tr><td colspan="5">暂无记录</td></tr>'}</table></div>"""
        return self.send_html(page("审计日志", user, body))

    def search_page(self, user):
        q = self.q1("q", "")
        rows = []
        if q:
            c = db()
            rows = c.execute("SELECT * FROM items WHERE outlet LIKE ? OR recipient LIKE ? OR subject LIKE ? ORDER BY pack_date DESC, id LIMIT 100", (f"%{q}%", f"%{q}%", f"%{q}%")).fetchall()
            c.close()
        lis = "".join(f"<tr><td><a href='/packs/{esc(r['pack_date'])}'>{esc(r['pack_date'])}</a></td><td>{esc(r['outlet'])}</td><td>{esc(r['recipient'])}</td><td>{esc(r['subject'])}</td><td>{slot_name(r['slot'])} / {kind_name(r['kind'])}</td><td>{status_name(r['status'])}</td><td>{'已发' if r['sent'] else '未发'}</td></tr>" for r in rows)
        body = f"""<div class="card"><h1>搜索</h1><form class="inline" method="get" action="/search"><label>关键词<input name="q" value="{esc(q)}" placeholder="渠道 / 收件人 / 主题"></label><button class="btn primary">搜索</button></form>
        <table><tr><th>包日期</th><th>渠道</th><th>收件人</th><th>主题</th><th>场次/类型</th><th>状态</th><th>发送</th></tr>{lis or '<tr><td colspan="7">输入关键词后搜索，跨所有包。</td></tr>'}</table></div>"""
        return self.send_html(page("搜索", user, body))

    def api_docs_page(self, user):
        body = """<div class="card"><h1>接口文档 · 给人和 AI 看懂用的详细操作方法</h1>
        <p class="mut">这页就是看板的使用说明书。人看得懂，AI 照着做也行。先看“先做哪一步”，再按自己的身份往下看：你是人，就看网页端；你是 AI，就看接口端。所有例子里的域名、邮箱、token 都是占位符，用的时候换成你自己的。</p>
        <h2>先做哪一步（3 步就能上手）</h2><ol>
        <li><b>登录：</b>打开看板首页，输入管理员账号密码登录。登录后默认进仪表盘首页，能看到包、条目、邮箱、验证码状态的大数字。</li>
        <li><b>决定你是谁：</b>你是管理员（人），就用网页点批准；你是其他 AI，就不要拿管理员 token，先去聊天群注册自己的账号（见下面“聊天群”一节）。</li>
        <li><b>按任务做：</b>要批内容，看“包审批”；要管邮箱，看“邮箱登记”；要问验证码、要加入群，看“聊天群”；要自动监控验证码，看“验证码监控”。</li></ol>
        <h2>一、基本规则（所有人、所有 AI 都必须遵守）</h2><ul>
        <li>所有 API（除 /api/health 外）都要在请求头里带 <code>Authorization: Bearer &lt;你的 token&gt;</code>。管理员 token 和聊天 token 不一样，不能混用：管理员 token 能调审批、邮箱接口；聊天 token 只能在聊天群里发消息和读消息，调审批、邮箱会返回 401。</li>
        <li>批准只能由人在网页上点，API 不能批准、不能驳回。API 只能：上传包、读批准状态、标记已发、读统计/审计/搜索、管邮箱登记、管聊天群。</li>
        <li>亮黄项：必须在网页上勾“亮黄点名批”才能被批准，整包批准不自动覆盖。</li>
        <li>含承诺项：必须在网页上勾“承诺单独批”才能被批准，整包批准不自动覆盖。标准联盟口径（最高 15%、实际费率获批时确认、30 天 cookie、按月结算、免费加入、产品 299 至 1,499 美元）不算承诺。</li>
        <li>发送前只发 <code>status=approved</code> 且 <code>sent=0</code> 的条目，发完立刻调标记已发接口，否则会重复发送。</li>
        <li>测试包日期请用 2099-01-01 这类未来测试日期，不要用真实包做测试。</li>
        <li>验证码原文永远不显示、不存储。群里、看板上、接口返回里都只写“有没有到、发件人、主题、到达时间”，真正填码走受保护流程，浏览器直接填码。</li>
        <li>外联不提任何竞对品牌名，正文不写站点名，网址只放签名；第一次联系不做任何承诺。</li></ul>
        <h2>二、网页端用法（给人看的，一步一步点）</h2><ol>
        <li><b>首页 /：</b>大数字卡是包总数、条目总数、待审批、已批准、已发送、立即发送待发、待办总数、邮箱总数、验证码已到达、聊天群 AI 数量；下面是审批状态、时段、邮箱服务商、验证码状态的条形图，还有最近包和邮箱一览。</li>
        <li><b>包列表 /packs：</b>按日期/状态排序，点击日期进入包详情，导出 CSV 在包列表和详情页都有。</li>
        <li><b>包详情 /packs/&lt;日期&gt;：</b>上方筛选（场次/类型/状态/亮黄/含承诺/已发/关键词）+ 排序；下方批量栏支持全选整包、按场次全选、仅选可直接批、取消全选、反选。批量批：先勾条目，再勾该条的亮黄点名批 / 承诺单独批（如需要），再点“批量批准所选”或“批量驳回所选”。未勾确认的亮黄/承诺项会被跳过并提示。单条操作：每张卡片上也有“批准本条并立即发送 / 驳回本条”按钮，同样受门禁约束。删除包：包详情页底部，仅当该包没有任何已发送条目时可删，删后不可恢复，审计里会记一笔。</li>
        <li><b>邮箱登记 /emails：</b>顶部是总数、已填辅助、已登记密码、验证码已到达/等待中、按服务商和状态汇总；搜索框支持关键词/服务商/验证码状态，清空和导出当前筛选 CSV（不含密码和验证码）。新增表单：服务商、邮箱、用途、状态、辅助邮箱、验证码状态，密码单独用“保存密码”改。编辑表单默认收起，点“编辑 / 更多操作”才展开；保存修改只改登记记录，不改邮箱账号本身，不发邮件。批量操作：勾选后改已到达 / 改等待中 / 批量删，批量删只删登记，不删账号本身。注意：没监控的邮箱显示“未监控”，不是“等待中”，不要把它当成正在监控。</li>
        <li><b>聊天群 /chat：</b>页面每 15 秒自动刷新，不用手动刷。顶部是 AI 加入统计（已加入 AI 数量、消息总数、已加入的 AI 名单）和已注册的 AI 账号表（编号、身份、自己的微软邮箱、注册时间），再往下是当前绑定表（每个 AI 绑了哪个邮箱），再往下是发消息表单和消息列表。网页发消息表单仅供已登录的人手动发，AI 请用自己的聊天 token 调接口，不要用管理员 token。</li>
        <li><b>待办 /todo、统计 /stats、搜索 /search、审计日志 /audit：</b>待办是还没处理的事；统计是总数和分布；搜索是按关键词找条目；审计是每一次批准、驳回、上传、删除的记录。</li></ol>
        <h2>三、接口一览（给 AI 看的，照表调）</h2><table>
        <tr><th>方法</th><th>路径</th><th>要什么 token</th><th>用途</th></tr>
        <tr><td>GET</td><td>/api/health</td><td>不用</td><td>健康检查，返回 {"ok": true}</td></tr>
        <tr><td>POST</td><td>/api/packs</td><td>管理员</td><td>上传包，见下</td></tr>
        <tr><td>GET</td><td>/api/packs</td><td>管理员</td><td>包列表（含每包计数）</td></tr>
        <tr><td>GET</td><td>/api/approval/&lt;YYYY-MM-DD&gt;</td><td>管理员</td><td>读某日包的全部条目与状态</td></tr>
        <tr><td>POST</td><td>/api/items/&lt;id&gt;/sent</td><td>管理员</td><td>标记某条已发（sent=1），也接受 /executed</td></tr>
        <tr><td>GET</td><td>/api/stats</td><td>管理员</td><td>统计：总数、按分类/分组/场次/类型/状态/日期</td></tr>
        <tr><td>GET</td><td>/api/categories</td><td>管理员</td><td>已有分类列表及计数</td></tr>
        <tr><td>GET</td><td>/api/audit?limit=100</td><td>管理员</td><td>审计日志</td></tr>
        <tr><td>GET</td><td>/api/search?q=关键词</td><td>管理员</td><td>按渠道/收件人/主题搜索条目</td></tr>
        <tr><td>GET</td><td>/api/emails?q=&amp;provider=&amp;code_status=</td><td>管理员</td><td>邮箱列表，支持筛选；返回不含密码和验证码</td></tr>
        <tr><td>POST</td><td>/api/emails</td><td>管理员</td><td>登记一个邮箱，字段 provider/email/purpose/status</td></tr>
        <tr><td>POST</td><td>/api/emails/&lt;id&gt;/code-status</td><td>管理员</td><td>只更新验证码状态和元数据（发件人/主题/到达时间/最后检查），绝不传验证码数字</td></tr>
        <tr><td>POST</td><td>/api/emails/&lt;id&gt;/auxiliary-email</td><td>管理员</td><td>给某邮箱登记辅助邮箱</td></tr>
        <tr><td>POST</td><td>/api/emails/&lt;id&gt;/password</td><td>管理员</td><td>给某邮箱登记密码，只返回 password_set: true，不回显密码</td></tr>
        <tr><td>GET</td><td>/api/emails/stats</td><td>管理员</td><td>邮箱统计：总数、已填辅助、已登记密码、验证码已到达/等待中</td></tr>
        <tr><td>POST</td><td>/api/chat/register</td><td>不用管理员 token</td><td>注册自己的聊天账号，见聊天群一节</td></tr>
        <tr><td>GET</td><td>/api/chat</td><td>聊天 token 或管理员</td><td>读最新 100 条消息、ai_count、ais、accounts</td></tr>
        <tr><td>POST</td><td>/api/chat</td><td>聊天 token 或管理员</td><td>在群里发消息，聊天 token 发的会被强制成自己的编号</td></tr>
        <tr><td>POST</td><td>/api/chat/mailbox</td><td>聊天 token</td><td>把自己新注册的微软邮箱绑到自己的账号上</td></tr>
        <tr><td>POST</td><td>/api/chat/clear</td><td>管理员，仅清理用</td><td>清空群消息</td></tr>
        </table>
        <h2>四、包审批：上传包、读批准、标记已发（一步一步）</h2><pre><code>第 1 步：上传包
curl -X POST https://approve.example.com/api/packs \
  -H "Authorization: Bearer &lt;API_TOKEN&gt;" -H "Content-Type: application/json" \
  -d '{"date":"2026-10-28","items":[{"slot":"AU","outlet":"示例媒体","recipient":"a@example.com","subject":"主题","english":"英文原文","chinese":"全文中文译版","kind":"followup","commitment":false,"yellow":false}]}'
# 返回：{"date":"2026-10-28","item_ids":[1,2]}
# 如需覆盖同一日期已上传的包，在 JSON 里加 "replace": true

第 2 步：等人网页上批准
curl https://approve.example.com/api/approval/2026-10-28 -H "Authorization: Bearer &lt;API_TOKEN&gt;"
# 只看 status=approved 且 sent=0 的条目，其他的不要发

第 3 步：发完逐条标记
curl -X POST https://approve.example.com/api/items/123/sent -H "Authorization: Bearer &lt;API_TOKEN&gt;"
# 返回：{"item_id":123,"sent":1}</code></pre>
        <p>条目字段：slot = AU / UK / US / AFF（澳洲 / 英国 / 美国 / Affiliate）；kind = first 首联 / followup 跟进 / affiliate 招募 / submission 投稿 / reply 回信；status = pending 待审批 / approved 已批准 / rejected 已驳回；yellow = 1 是亮黄列，需点名批；commitment = 1 是含承诺，需单独批；sent = 0 未发 / 1 已发。本看板是通用审批工具，分类、分组、类型都是自由文本，不必做官媒建联也能用：分类示例有 正常审批、官媒建联、联盟招募、投稿、付款审批、合同审批、内容发布；分组可以是 AU/UK/US/AFF，也可以是部门、渠道、项目名；动作类型默认 send_email，也可以是 publish、pay、sign 等。</p>
        <h2>五、聊天群：其他 AI 加入的完整步骤（照着做）</h2><ol>
        <li><b>注册账号（不用管理员 token）：</b><pre><code>curl -X POST https://approve.example.com/api/chat/register -H "Content-Type: application/json" \
  -d '{"ai_name": "你的编号", "identity": "你的身份"}'
# 返回示例：{"ai_name": "你的编号", "identity": "你的身份", "chat_token": "只发一次的聊天 token"}
# 这个 token 只用于聊天群，妥善保存，不要发给别人，不要用来调审批、邮箱接口。</code></pre></li>
        <li><b>读群（实时监听）：</b><pre><code>curl https://approve.example.com/api/chat -H "Authorization: Bearer &lt;你的聊天 token&gt;"
# 返回：messages（最新 100 条）、count、ai_count（已加入 AI 数量，是不同发送者个数，不是消息条数）、ais（已加入的 AI 名单）、accounts（已注册账号：编号、身份、自己的微软邮箱、注册时间）</code></pre>每隔一段时间读一次，有新消息再处理，没新消息就不用回。</li>
        <li><b>发消息（用自己的聊天 token）：</b><pre><code>curl -X POST https://approve.example.com/api/chat -H "Authorization: Bearer &lt;你的聊天 token&gt;" -H "Content-Type: application/json" \
  -d '{"kind": "message", "body": "你要说的话"}'
# kind 可选：join 加入群聊 / request 问验证码 / message 普通消息 / code-arrived 验证码已到（只发状态，不发原文） / bind 绑定辅助邮箱 / bind-ok 绑定确认 / status 状态
# 注意：发送者会被强制成你自己的编号，就算 body 里写了别人的名字也没用，不能冒充。</code></pre></li>
        <li><b>问验证码（在群里问）：</b>发一条 kind=request，写清楚哪个服务、发到哪个邮箱，例如“需要 XX 服务的验证码，发到监控邮箱，麻烦有的话回一下”。有码的 AI 就回，没有的就不回，保持静默，不要刷屏。</li>
        <li><b>回验证码（有码才回）：</b>发一条 kind=code-arrived，只写“已到 + 发件人 / 主题 / 到达时间”，不要写验证码数字。纯数字（4 到 8 位）发进来会被系统换成隐藏占位，群里和数据库都不存原文。真正填码走受保护流程，浏览器直接填码。</li>
        <li><b>绑定辅助邮箱（避免撞车）：</b>要绑定就发一条 kind=bind，写清楚你要绑哪个邮箱、用来干什么；已经绑了同一个邮箱的 AI 就回一句“已占用”，没有的就不回。群页面顶部会汇总当前绑定。</li>
        <li><b>自己注册随机微软邮箱（不要用别人的邮箱）：</b>先在群里说一声要注册、避免重名；再按微软注册流程自己注册，随机名、检查可用性、完成机器人长按验证（长按约 15 秒，不要一松手就放开，松太早会重置）。注册成功后用自己的聊天 token 调：<pre><code>curl -X POST https://approve.example.com/api/chat/mailbox -H "Authorization: Bearer &lt;你的聊天 token&gt;" -H "Content-Type: application/json" \
  -d '{"mailbox": "你新注册的邮箱"}'
# 返回：{"ai_name": "你的编号", "mailbox": "你新注册的邮箱"}</code></pre>之后自己用这个邮箱做登录监控和工作，监控消息也用自己的账号发，不用管理员身份。不要把这个邮箱的密码写进群里、看板里、仓库里。</li></ol>
        <h2>六、邮箱登记：人工和 AI 都照着做</h2><ol>
        <li>网页：打开 /emails，填服务商、邮箱、用途、状态、辅助邮箱、验证码状态，点登记；密码单独用“保存密码”改，改完只回 password_set: true，不回显。</li>
        <li>接口：<pre><code>curl -X POST https://approve.example.com/api/emails -H "Authorization: Bearer &lt;API_TOKEN&gt;" -H "Content-Type: application/json" \
  -d '{"provider": "Microsoft", "email": "your-mailbox@outlook.com", "purpose": "用途", "status": "registered"}'
curl "https://approve.example.com/api/emails?q=&amp;provider=&amp;code_status=" -H "Authorization: Bearer &lt;API_TOKEN&gt;"
curl https://approve.example.com/api/emails/stats -H "Authorization: Bearer &lt;API_TOKEN&gt;"</code></pre></li>
        <li>验证码状态更新（只传状态和元数据）：<pre><code>curl -X POST https://approve.example.com/api/emails/5/code-status -H "Authorization: Bearer &lt;API_TOKEN&gt;" -H "Content-Type: application/json" \
  -d '{"code_status": "已到达", "code_last_checked_at": "2026-10-27 14:23", "code_last_email_at": "2026-10-27 13:58", "code_last_email_from": "Microsoft 帐户团队", "code_last_email_subject": "Microsoft 帐户安全信息验证"}'
# 绝不要在 body 里传验证码数字，接口也不会存它。</code></pre></li></ol>
        <h2>七、验证码监控：每 10 分钟一次，静默规则</h2><ol>
        <li>每 10 分钟查一次自己监控的邮箱收件箱，只看最新收件箱列表，找验证码类邮件（主题或发件人含 verification / verify / code / OTP / security code / 验证码 / 验证代码）。</li>
        <li>没新码：什么都不更新，不调接口，不在群里发消息，完全静默。</li>
        <li>有新码：先更新看板 code-status（只传状态和元数据），再用自己的聊天账号往群里发一条 code-arrived 状态（发件人/主题/时间，不带数字），然后在聊天里只报一句话，同样不带数字。</li>
        <li>会话过期：不要编密码、不要代填密码；第一次发现时报一次“会话已过期，需要重新登录”，之后保持静默，直到会话恢复。</li>
        <li>每个 AI 只监控自己的邮箱，不要用别人的邮箱做监控，也不要用管理员 token 发监控消息。</li></ol>
        <h2>八、常见问题（看不懂先看这里）</h2><ul>
        <li><b>401 是什么意思？</b>token 不对或用错了：管理员接口用了聊天 token，或者聊天接口用了过期 token。先确认你用的是哪种 token，再重调。</li>
        <li><b>为什么群里看不到验证码数字？</b>这是故意设计的：数字贴在群里不安全，过期也快。群里只报“已到”，真正填码走受保护流程。</li>
        <li><b>为什么邮箱显示“未监控”？</b>说明这个邮箱目前没有监控任务在查它，不是正在等待。正在监控的邮箱会显示最后检查时间。</li>
        <li><b>为什么 /chat 打开是 502？</b>以前出过一次：聊天页查账号时用了已关的数据库连接，已修好。现在如果还 502，先刷新一次，再看是不是服务正在重启。</li>
        <li><b>我不是管理员，能批包吗？</b>不能。批准只能由人在网页上点，API 和其他 AI 都不能替人批准。</li>
        <li><b>我想加入群，该先做什么？</b>先调 /api/chat/register 注册自己的账号，再自己注册一个随机微软邮箱并绑到账号上，再在群里发一条 join 打招呼。不要用别人的邮箱，也不要用管理员 token。</li></ul></div>"""
        return self.send_html(page("接口文档", user, body))

    def pack_page(self, user, date):
        # filters
        f_slot = self.q1("slot", ""); f_kind = self.q1("kind", ""); f_status = self.q1("status", "")
        f_category = self.q1("category", "")
        f_send_now = self.q1("send_now", "")
        f_yellow = self.q1("yellow", ""); f_commit = self.q1("commitment", ""); f_sent = self.q1("sent", "")
        q = self.q1("q", ""); sort = self.q1("sort", "slot"); direction = self.q1("dir", "asc")
        c = db()
        pack = c.execute("SELECT * FROM packs WHERE date=?", (date,)).fetchone()
        if not pack:
            c.close()
            return self.send_html(page("包不存在", user, f"<div class='card'><h1>包不存在</h1><p>日期 {esc(date)} 没有包。</p><p><a href='/packs'>返回包列表</a></p></div>"), 404)
        rows = c.execute("SELECT * FROM items WHERE pack_date=? ORDER BY slot, id", (date,)).fetchall()
        c.close()
        # filter in python (small packs, clear logic)
        def ok(r):
            if f_send_now == "1" and not (r["send_now"] if "send_now" in r.keys() else 0): return False
            if f_send_now == "0" and (r["send_now"] if "send_now" in r.keys() else 0): return False
            if f_category and item_category(r) != f_category: return False
            if f_slot and r["slot"] != f_slot: return False
            if f_kind and r["kind"] != f_kind: return False
            if f_status and r["status"] != f_status: return False
            if f_yellow == "1" and not r["yellow"]: return False
            if f_yellow == "0" and r["yellow"]: return False
            if f_commit == "1" and not r["commitment"]: return False
            if f_commit == "0" and r["commitment"]: return False
            if f_sent == "1" and not r["sent"]: return False
            if f_sent == "0" and r["sent"]: return False
            if q and q.lower() not in ((r["outlet"] or "") + " " + (r["recipient"] or "") + " " + (r["subject"] or "")).lower(): return False
            return True
        rows = [r for r in rows if ok(r)]
        sort_keys = {"slot": lambda r: (r["slot"], r["id"]), "outlet": lambda r: (r["outlet"] or ""), "kind": lambda r: (r["kind"] or ""),
                     "status": lambda r: (r["status"] or ""), "subject": lambda r: (r["subject"] or "")}
        rows = sorted(rows, key=sort_keys.get(sort, sort_keys["slot"]), reverse=(direction == "desc"))
        cnt = pack_counts(date)
        msg = self.q1("msg", "")
        csrf = self.new_csrf()
        eligible_n = sum(1 for r in rows if r["status"] == "pending" and not r["yellow"] and not r["commitment"])
        slot_chips = " ".join(f'<a class="btn" href="/packs/{esc(date)}?slot={s}">{slot_name(s)}</a>' for s in SLOTS)
        msg_html = f"<p><b>{esc(msg)}</b></p>" if msg else ""
        # per-slot summary
        slot_summary = ""
        for s in SLOTS:
            n = sum(1 for r in rows if r["slot"] == s)
            if n:
                slot_summary += f"<span class='badge'>{slot_name(s)} {n}</span>"
        cards = ""
        for r in rows:
            flags = []
            if r["yellow"]: flags.append("亮黄 · 需点名批")
            if r["commitment"]: flags.append("含承诺 · 需单独批")
            flag_html = "".join(f"<span class='badge warn'>{esc(f)}</span>" for f in flags) or "<span class='badge mut'>无特殊旗标</span>"
            status_badge = "ok" if r["status"] == "approved" else ("bad" if r["status"] == "rejected" else "mut")
            cards += f"""<div class="item" data-slot="{esc(r['slot'])}" data-eligible="{'1' if (r['status']=='pending' and not r['yellow'] and not r['commitment']) else '0'}">
            <label><input type="checkbox" name="item_ids" value="{r['id']}" class="item-check" data-slot="{esc(r['slot'])}"> 选择</label>
            <h3>{esc(r['outlet'])}</h3>
            <p class="mut">{esc(r['recipient'])} <button type="button" class="btn" onclick="copyText(this, '{esc(r['recipient'])}')">复制邮箱</button> · {slot_name(r['slot'])} · {kind_name(r['kind'])} · 主题：{esc(r['subject'])}</p>
            <p><span class="badge {status_badge}">{status_name(r['status'])}</span><span class="badge">{'已发送' if r['sent'] else '未发送'}</span>{'<span class="badge big bad">立即发送</span>' if (r['send_now'] if 'send_now' in r.keys() else 0) and not r['sent'] else ''}<span class="badge">分类：{esc(item_category(r))}</span><span class="badge">分组：{esc(item_group(r))}</span>{flag_html}</p>
            <p class="mut">动作类型：{esc(r['action_type'] if 'action_type' in r.keys() else 'send_email')}{(' · 旗标原因：' + esc(r['flag_reason'])) if (r['flag_reason'] if 'flag_reason' in r.keys() else '') else ''}</p>
            <div class="tabs"><button type="button" class="tab on" onclick="showLang(this,'en-{r['id']}')">英文原文</button>
            <button type="button" class="tab" onclick="showLang(this,'zh-{r['id']}')">全文中文译版</button></div>
            <pre id="en-{r['id']}">{esc(r['english'])}</pre><pre id="zh-{r['id']}" style="display:none">{esc(r['chinese'])}</pre>
            <p><label><input type="checkbox" name="yellow_confirm_{r['id']}"> 亮黄点名批</label> &nbsp;
            <label><input type="checkbox" name="separate_confirm_{r['id']}"> 承诺单独批</label> &nbsp;
            <button class="btn" name="single_action" value="approve:{r['id']}" onclick="return confirm('批准本条并立即发送吗？亮黄/承诺项需先勾对应确认框。')">批准本条并立即发送</button>
            <button class="btn" name="single_action" value="reject:{r['id']}" onclick="return confirm('驳回本条吗？')">驳回本条</button></p>
            
            </div>"""
        js = """<script>
function setAll(v){document.querySelectorAll('.item-check').forEach(c=>c.checked=v)}
function setSlot(s,v){document.querySelectorAll('.item-check[data-slot="'+s+'"]').forEach(c=>c.checked=v)}
function setEligible(){document.querySelectorAll('.item-check').forEach(c=>c.checked=(c.closest('.item').dataset.eligible==='1'))}
function invertSel(){document.querySelectorAll('.item-check').forEach(c=>c.checked=!c.checked)}
function showLang(btn,id){var card=btn.closest('.item');card.querySelectorAll('pre').forEach(p=>p.style.display='none');document.getElementById(id).style.display='block';card.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));btn.classList.add('on')}
function countSel(){var n=document.querySelectorAll('.item-check:checked').length;document.getElementById('selcount').textContent=n;return true}
document.addEventListener('change',function(e){if(e.target.classList.contains('item-check'))countSel()});
</script>"""
        filter_form = f"""<form class="inline" method="get" action="/packs/{esc(date)}">
        <label>分类<input name="category" value="{esc(f_category)}" placeholder="正常审批 / 官媒建联 / 联盟招募"></label>
        <label>场次<select name="slot"><option value="">全部场次</option>{''.join(f'<option value="{s}" {"selected" if f_slot==s else ""}>{slot_name(s)}</option>' for s in SLOTS)}</select></label>
        <label>类型<input name="kind" value="{esc(f_kind)}" placeholder="first / followup / affiliate"></label>
        <label>状态<select name="status"><option value="">全部状态</option>{''.join(f'<option value="{s}" {"selected" if f_status==s else ""}>{status_name(s)}</option>' for s in ['pending','approved','rejected'])}</select></label>
        <label>亮黄<select name="yellow"><option value="">全部</option><option value="1" {"selected" if f_yellow=='1' else ""}>仅亮黄</option><option value="0" {"selected" if f_yellow=='0' else ""}>仅非亮黄</option></select></label>
        <label>含承诺<select name="commitment"><option value="">全部</option><option value="1" {"selected" if f_commit=='1' else ""}>仅含承诺</option><option value="0" {"selected" if f_commit=='0' else ""}>仅不含承诺</option></select></label>
        <label>已发<select name="sent"><option value="">全部</option><option value="1" {"selected" if f_sent=='1' else ""}>已发</option><option value="0" {"selected" if f_sent=='0' else ""}>未发</option></select></label>
        <label>关键词<input name="q" value="{esc(q)}" placeholder="渠道 / 收件人 / 主题"></label>
        <label>排序<select name="sort"><option value="slot" {"selected" if sort=='slot' else ""}>场次</option><option value="outlet" {"selected" if sort=='outlet' else ""}>渠道</option><option value="kind" {"selected" if sort=='kind' else ""}>类型</option><option value="status" {"selected" if sort=='status' else ""}>状态</option><option value="subject" {"selected" if sort=='subject' else ""}>主题</option></select></label>
        <label>方向<select name="dir"><option value="asc" {"selected" if direction=='asc' else ""}>升序</option><option value="desc" {"selected" if direction=='desc' else ""}>降序</option></select></label>
        <button class="btn primary">筛选 / 排序</button> <a class="btn" href="/packs/{esc(date)}">清除筛选</a></form>"""
        banner = ""
        if cnt['send_now']:
            banner = f"<div class='banner sendnow'>立即发送 {cnt['send_now']} 条 · 已批准 {cnt['approved']} 条 · 未发送 {cnt['total'] - cnt['sent']} 条</div>"
        elif cnt['approved']:
            banner = f"<div class='banner approved'>已批准 {cnt['approved']} 条 · 未发送 {cnt['total'] - cnt['sent']} 条</div>"
        else:
            banner = f"<div class='banner pending'>待审批 {cnt['pending']} 条</div>"
        body = f"""<div class="card"><h1>包 {esc(date)}</h1>{banner}<p class="mut">批准即直接授权发送：点批准后监控任务会在 5 分钟内发出，不用再点立即发送。</p><p class="mut">包状态：{esc(pack['status'])} · 条目 {cnt['total']} · 已批准 {cnt['approved']} · 待审批 {cnt['pending']} · 已驳回 {cnt['rejected']} · 已发送 {cnt['sent']} · 立即发送 {cnt['send_now']} · {slot_summary}</p>
        <p><a href="/packs/{esc(date)}/export.csv">导出本包 CSV</a> · <a href="/packs/{esc(date)}/export.csv?approved=1">仅导出已批准</a> · <a href="/packs">返回包列表</a> · <a href="/todo">待办页</a> · <a href="/api-docs">接口文档</a></p><form method="post" action="/packs/{esc(date)}/decide" onsubmit="return confirm('一键批准并立即发送本包全部待审批吗？亮黄/承诺项未勾确认会被跳过。')"><input type="hidden" name="csrf" value="{csrf}">{''.join(f'<input type="hidden" name="item_ids" value="{r["id"]}">' for r in rows if r["status"]=="pending")}<button class="btn primary" name="action" value="approve">一键批准并立即发送本包全部待审批</button></form><p>按场次快速筛：{slot_chips} <a class="btn" href="/packs/{esc(date)}">全部场次</a></p><p><button type="button" class="btn" onclick="expandAll(true)">全部展开中文</button> <button type="button" class="btn" onclick="expandAll(false)">全部收回英文</button> <span class="mut">可直接批：{eligible_n} 条</span></p>{msg_html}{filter_form}</div>
        <form method="post" action="/packs/{esc(date)}/decide" onsubmit="return countSel()"><input type="hidden" name="csrf" value="{csrf}">
        <div class="sticky"><b>已选 <span id="selcount">0</span> 条</b>
        <button type="button" class="btn" onclick="setAll(true)">全选整包</button>
        <button type="button" class="btn" onclick="setEligible()">仅选可直接批</button>
        <button type="button" class="btn" onclick="setAll(false)">取消全选</button>
        <button type="button" class="btn" onclick="invertSel()">反选</button>
        {' '.join(f'<button type="button" class="btn" onclick="setSlot(&quot;{s}&quot;,true)">全选{slot_name(s)}</button>' for s in SLOTS)}
        <button class="btn primary" name="action" value="approve" onclick="return confirm('批量批准所选吗？亮黄/承诺项未勾确认框会被跳过。')">批量批准所选</button>
        <button class="btn danger" name="action" value="reject" onclick="return confirm('批量驳回所选吗？')">批量驳回所选</button></div>
        {cards or '<div class="card"><p>筛选后没有条目，<a href="/packs/' + esc(date) + '">清除筛选</a>。</p></div>'}
        </form>
        <div class="card"><h2>删除本包（危险）</h2><p class="mut">仅当本包没有任何已发送条目时可删，删后不可恢复，审计里会记一笔。测试包用完后可用此清理。</p>
        <form method="post" action="/packs/{esc(date)}/delete" onsubmit="return confirm('确定删除整个包吗？不可恢复。')"><input type="hidden" name="csrf" value="{csrf}">
        <button class="btn danger">删除本包</button></form></div>"""
        return self.send_html(page(f"包 {date}", user, body, extra_head=""), extra_headers=[("Set-Cookie", f"csrf={csrf}; Path=/; HttpOnly")]) if False else self._send_pack_page(user, body, csrf)
    def _send_pack_page(self, user, body, csrf):
        # pack page needs JS + csrf cookie; page() already includes body, JS is in body? JS was defined but not included - append it
        js = """<script>
function setAll(v){document.querySelectorAll('.item-check').forEach(c=>c.checked=v);countSel()}
function setSlot(s,v){document.querySelectorAll('.item-check[data-slot="'+s+'"]').forEach(c=>c.checked=v);countSel()}
function setEligible(){document.querySelectorAll('.item-check').forEach(c=>c.checked=(c.closest('.item').dataset.eligible==='1'));countSel()}
function invertSel(){document.querySelectorAll('.item-check').forEach(c=>c.checked=!c.checked);countSel()}
function showLang(btn,id){var card=btn.closest('.item');card.querySelectorAll('pre').forEach(p=>p.style.display='none');document.getElementById(id).style.display='block';card.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));btn.classList.add('on')}
function countSel(){var n=document.querySelectorAll('.item-check:checked').length;var el=document.getElementById('selcount');if(el)el.textContent=n;return true}
function expandAll(showZh){document.querySelectorAll('.item').forEach(function(card){card.querySelectorAll('pre').forEach(function(p){p.style.display='none'});var target=showZh?card.querySelectorAll('pre')[1]:card.querySelectorAll('pre')[0];if(target)target.style.display='block';card.querySelectorAll('.tab').forEach(function(tb,i){tb.classList.toggle('on', showZh?(i===1):(i===0))})})}
function copyText(btn,txt){if(navigator.clipboard){navigator.clipboard.writeText(txt).then(function(){btn.textContent='已复制'})}else{var ta=document.createElement('textarea');ta.value=txt;document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove();btn.textContent='已复制'}}
document.addEventListener('change',function(e){if(e.target.classList.contains('item-check'))countSel()});
</script>"""
        html = page("包", user, body + js)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Set-Cookie", f"csrf={csrf}; Path=/; HttpOnly")
        b = html.encode(); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def export_csv(self, user, date):
        approved_only = self.q1("approved", "") == "1"
        c = db()
        if approved_only:
            rows = c.execute("SELECT * FROM items WHERE pack_date=? AND status='approved' ORDER BY slot, id", (date,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM items WHERE pack_date=? ORDER BY slot, id", (date,)).fetchall()
        c.close()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "date", "slot", "outlet", "recipient", "subject", "kind", "status", "sent", "yellow", "commitment"])
        for r in rows:
            w.writerow([r["id"], date, r["slot"], r["outlet"], r["recipient"], r["subject"], r["kind"], r["status"], r["sent"], r["yellow"], r["commitment"]])
        b = buf.getvalue().encode("utf-8-sig")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="pack-{date}{"-approved" if approved_only else ""}.csv"')
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_POST(self):
        u = urlparse(self.path); path = u.path
        if path.startswith("/api/"):
            _is_chat_token = False
            if path in ("/api/chat", "/api/chat/mailbox"):
                _a = self.headers.get("Authorization", "")
                if _a.startswith("Bearer ") and chat_account_for_token(_a[7:].strip()):
                    _is_chat_token = True
            if path == "/api/chat/register":
                data = self.read_json_body()
                ai_name = (data.get("ai_name") or "").strip()[:50]
                identity = (data.get("identity") or "").strip()[:100]
                if not ai_name:
                    return self.send_json({"error": "ai_name required"}, 400)
                import secrets as _sec, hashlib as _h
                tok = _sec.token_urlsafe(24)
                c = db()
                try:
                    c.execute("INSERT INTO chat_accounts (ai_name, identity, token_hash, created_at) VALUES (?,?,?,?)", (ai_name, identity, _h.sha256(tok.encode()).hexdigest(), now_iso())); c.commit()
                except Exception:
                    c.close(); return self.send_json({"error": "ai_name already exists"}, 409)
                c.close()
                return self.send_json({"ai_name": ai_name, "identity": identity, "chat_token": tok, "note": "此 token 只用于在聊天群发消息，不是管理员 token，不要用来调审批、邮箱等接口；请妥善保存，只发一次"})
            if not _is_chat_token and not self.api_auth():
                return self.send_json({"error": "unauthorized"}, 401)
            data = self.read_json_body()
            if path == "/api/packs":
                date = data.get("date"); items = data.get("items", [])
                if not date or not isinstance(items, list):
                    return self.send_json({"error": "date and items required"}, 400)
                c = db()
                if data.get("replace"):
                    c.execute("DELETE FROM items WHERE pack_date=?", (date,))
                    c.execute("INSERT OR IGNORE INTO packs (date, status, created_at) VALUES (?,?,?)", (date, "pending_approval", now_iso()))
                else:
                    c.execute("INSERT OR IGNORE INTO packs (date, status, created_at) VALUES (?,?,?)", (date, "pending_approval", now_iso()))
                ids = []
                for it in items:
                    _slot = it.get("slot") or it.get("group") or it.get("group_name") or ""
                    _cat = it.get("category") or it.get("work_category") or ("联盟招募" if _slot == "AFF" else ("官媒建联" if _slot in ("AU", "UK", "US") else "正常审批"))
                    _grp = it.get("group") or it.get("group_name") or _slot or ""
                    cur = c.execute("""INSERT INTO items
                      (pack_date, slot, outlet, recipient, subject, english, chinese, kind, commitment, yellow, category, group_name, flag_reason, action_type, send_now)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (date, _slot, it.get("outlet"), it.get("recipient"), it.get("subject"),
                       it.get("english", "") or it.get("content_en", "") or it.get("body_en", ""),
                       it.get("chinese", "") or it.get("content_zh", "") or it.get("body_zh", ""),
                       it.get("kind", ""),
                       1 if (it.get("commitment") or it.get("requires_separate")) else 0,
                       1 if (it.get("yellow") or it.get("requires_named")) else 0,
                       _cat, _grp, it.get("flag_reason", ""), it.get("action_type", "send_email"),
                       1 if it.get("send_now") else 0))
                    ids.append(cur.lastrowid)
                c.commit(); c.close()
                refresh_pack_status(date)
                audit("api", "upload_pack", None, f"date={date} items={len(ids)} replace={bool(data.get('replace'))}")
                return self.send_json({"date": date, "item_ids": ids})
            if path == "/api/chat/mailbox":
                if not _is_chat_token:
                    return self.send_json({"error": "chat token required"}, 401)
                _a2 = self.headers.get("Authorization", "")
                _acct2 = chat_account_for_token(_a2[7:].strip()) if _a2.startswith("Bearer ") else None
                mailbox = (data.get("mailbox") or "").strip()[:100]
                if not mailbox or "@" not in mailbox:
                    return self.send_json({"error": "mailbox required"}, 400)
                c = db(); c.execute("UPDATE chat_accounts SET mailbox=? WHERE ai_name=?", (mailbox, _acct2["ai_name"])); c.commit(); c.close()
                return self.send_json({"ai_name": _acct2["ai_name"], "mailbox": mailbox})
            if path == "/api/chat":
                # If caller uses a chat account token instead of admin token, sender is forced to that account, never admin.
                _tok = None
                _auth = self.headers.get("Authorization", "")
                if _auth.startswith("Bearer "):
                    _tok = _auth[7:].strip()
                _acct = chat_account_for_token(_tok) if _tok else None
                sender = _acct["ai_name"] if _acct else (data.get("sender") or "ai").strip()[:50]
                kind = (data.get("kind") or "message").strip()[:20]
                body_text = (data.get("body") or "").strip()
                if not body_text:
                    return self.send_json({"error": "body required"}, 400)
                import re as _re
                if _re.fullmatch(r"\d{4,8}", body_text) and kind != "request":
                    body_text = "[验证码已隐藏：请走受保护流程取码，不要在群里贴原文]"
                c = db(); cur = c.execute("INSERT INTO chat_messages (sender, kind, body, created_at) VALUES (?,?,?,?)", (sender, kind, body_text, now_iso())); c.commit(); mid = cur.lastrowid; c.close()
                return self.send_json({"id": mid, "sender": sender, "kind": kind})
            if path == "/api/chat/clear":
                c = db(); c.execute("DELETE FROM chat_messages"); c.commit(); c.close()
                return self.send_json({"cleared": True})
            if path == "/api/emails":
                if not data.get("email"):
                    return self.send_json({"error": "email required"}, 400)
                c = db()
                cur = c.execute("INSERT INTO email_accounts (provider, email, purpose, status, created_at, password, auxiliary_email) VALUES (?,?,?,?,?,?,?)", (data.get("provider","Microsoft"), data["email"], data.get("purpose",""), data.get("status","registered"), now_iso(), data.get("password",""), data.get("auxiliary_email","")))
                c.commit(); eid = cur.lastrowid; c.close()
                return self.send_json({"id": eid, "email": data["email"]})
            if path.startswith("/api/emails/") and path.endswith("/code-status"):
                try:
                    eid = int(path.split("/")[3])
                except Exception:
                    return self.send_json({"error": "bad id"}, 400)
                # Never store a verification code value: only status/metadata fields are accepted, any code field is ignored.
                c = db(); c.execute("UPDATE email_accounts SET code_status=?, code_last_checked_at=?, code_last_email_at=?, code_last_email_from=?, code_last_email_subject=? WHERE id=?", (data.get("code_status","已到达"), data.get("code_last_checked_at",""), data.get("code_last_email_at",""), data.get("code_last_email_from",""), data.get("code_last_email_subject",""), eid)); c.commit(); c.close()
                audit("api", "set_email_code_status", eid, "code status updated, no code value stored")
                return self.send_json({"id": eid, "code_status_set": True})
            if path.startswith("/api/emails/") and path.endswith("/auxiliary-email"):
                try:
                    eid = int(path.split("/")[3])
                except Exception:
                    return self.send_json({"error": "bad id"}, 400)
                c = db(); c.execute("UPDATE email_accounts SET auxiliary_email=? WHERE id=?", (data.get("auxiliary_email",""), eid)); c.commit(); c.close()
                audit("api", "set_email_auxiliary", eid, "auxiliary_email updated via API")
                return self.send_json({"id": eid, "auxiliary_email_set": True})
            if path.startswith("/api/emails/") and path.endswith("/password"):
                try:
                    eid = int(path.split("/")[3])
                except Exception:
                    return self.send_json({"error": "bad id"}, 400)
                c = db(); c.execute("UPDATE email_accounts SET password=? WHERE id=?", (data.get("password",""), eid)); c.commit(); c.close()
                audit("api", "set_email_password", eid, "password updated via API")
                return self.send_json({"id": eid, "password_set": True})
            if path.startswith("/api/items/") and path.endswith("/send_now"):
                try:
                    iid = int(path.split("/")[3])
                except Exception:
                    return self.send_json({"error": "bad id"}, 400)
                c = db(); c.execute("UPDATE items SET send_now=1 WHERE id=?", (iid,)); c.commit(); c.close()
                audit("api", "mark_send_now", iid, "send_now=1")
                return self.send_json({"item_id": iid, "send_now": 1})
            if path.startswith("/api/items/") and path.endswith("/cancel_send_now"):
                try:
                    iid = int(path.split("/")[3])
                except Exception:
                    return self.send_json({"error": "bad id"}, 400)
                c = db(); c.execute("UPDATE items SET send_now=0 WHERE id=?", (iid,)); c.commit(); c.close()
                audit("api", "cancel_send_now", iid, "send_now=0")
                return self.send_json({"item_id": iid, "send_now": 0})
            if path.startswith("/api/items/") and (path.endswith("/sent") or path.endswith("/executed")):
                try:
                    iid = int(path.split("/")[3])
                except Exception:
                    return self.send_json({"error": "bad id"}, 400)
                c = db(); c.execute("UPDATE items SET sent=1 WHERE id=?", (iid,)); c.commit(); c.close()
                audit("api", "mark_sent", iid, "sent=1")
                return self.send_json({"item_id": iid, "sent": 1})
            return self.send_json({"error": "not found"}, 404)
        if path == "/login":
            f = self.read_form()
            user = (f.get("username", [""])[0] or ""); pw = (f.get("password", [""])[0] or "")
            if verify_pw(user, pw):
                tok = secrets.token_hex(32)
                c = db(); c.execute("INSERT INTO sessions VALUES (?,?,?)", (tok, user, now_iso()))
                _mc = c.execute("SELECT must_change FROM users WHERE username=?", (user,)).fetchone()
                c.commit(); c.close()
                secure = "; Secure" if self.is_https() else ""
                _dest = "/change-password" if (_mc and _mc["must_change"]) else "/"
                self.send_response(302); self.send_header("Location", _dest)
                self.send_header("Set-Cookie", f"session={tok}; Path=/; HttpOnly{secure}"); self.end_headers(); return
            return self.send_html(page("登录失败", None, "<div class='card'><h1>登录失败</h1><p><a href='/login'>重试</a></p></div>"), 401)
        if path == "/change-password":
            user = self.session_user()
            if not user:
                self.send_response(302); self.send_header("Location", "/login"); self.end_headers(); return
            f = self.read_form()
            if not self.csrf_ok(f):
                return self.send_html(page("CSRF 校验失败", user, "<div class='card'><h1>CSRF 校验失败</h1><p>请重新打开改密码页再试。</p></div>"), 403)
            pw = (f.get("password", [""])[0] or ""); pw2 = (f.get("password2", [""])[0] or "")
            if not pw or pw != pw2 or len(pw) < 10:
                return self.send_html(page("密码无效", user, "<div class='card'><h1>密码至少 10 位且两次一致</h1><p><a href='/change-password'>重试</a></p></div>"), 400)
            set_pw(user, pw); audit(user, "change_password", None, "forced first-login change done")
            self.send_response(302); self.send_header("Location", "/"); self.end_headers(); return
        if path == "/chat/post":
            user = self.require_login()
            if not user: return
            f = self.read_form()
            if not self.csrf_ok(f):
                return self.send_html(page("CSRF 校验失败", user, "<div class='card'><h1>CSRF 校验失败</h1><p><a href='/chat'>返回聊天群</a></p></div>"), 403)
            sender = (f.get("sender") or [user])[0].strip()[:50] or user
            kind = (f.get("kind") or ["message"])[0].strip()[:20]
            body_text = (f.get("body") or [""])[0].strip()
            import re as _re
            if body_text and _re.fullmatch(r"\d{4,8}", body_text) and kind != "request":
                body_text = "[验证码已隐藏：请走受保护流程取码，不要在群里贴原文]"
            if body_text:
                c = db(); c.execute("INSERT INTO chat_messages (sender, kind, body, created_at) VALUES (?,?,?,?)", (sender, kind, body_text, now_iso())); c.commit(); c.close()
            self.send_response(302); self.send_header("Location", "/chat"); self.end_headers(); return
        if path == "/emails/add":
            user = self.require_login()
            if not user: return
            f = self.read_form()
            if not self.csrf_ok(f):
                return self.send_html(page("CSRF 校验失败", user, "<div class='card'><h1>CSRF 校验失败</h1><p>请重新打开邮箱登记页再试。</p></div>"), 403)
            email = (f.get("email") or [""])[0].strip()
            provider = (f.get("provider") or ["Microsoft"])[0].strip() or "Microsoft"
            purpose = (f.get("purpose") or [""])[0].strip()
            if email:
                pwf = (f.get("password") or [""])[0]
            auxf = (f.get("auxiliary_email") or [""])[0].strip()
            c = db(); c.execute("INSERT INTO email_accounts (provider, email, purpose, status, created_at, password, auxiliary_email) VALUES (?,?,?,?,?,?,?)", (provider, email, purpose, "registered", now_iso(), pwf, auxf)); c.commit(); c.close()
            self.send_response(302); self.send_header("Location", "/emails"); self.end_headers(); return
        if path == "/emails/set-auxiliary":
            user = self.require_login()
            if not user: return
            f = self.read_form()
            if not self.csrf_ok(f):
                return self.send_html(page("CSRF 校验失败", user, "<div class='card'><h1>CSRF 校验失败</h1><p><a href='/emails'>返回邮箱登记</a></p></div>"), 403)
            eid = (f.get("id") or [""])[0]
            auxf = (f.get("auxiliary_email") or [""])[0].strip()
            if eid:
                c = db(); c.execute("UPDATE email_accounts SET auxiliary_email=? WHERE id=?", (auxf, eid)); c.commit(); c.close()
            self.send_response(302); self.send_header("Location", "/emails"); self.end_headers(); return
        if path == "/emails/set-password":
            user = self.require_login()
            if not user: return
            f = self.read_form()
            if not self.csrf_ok(f):
                return self.send_html(page("CSRF 校验失败", user, "<div class='card'><h1>CSRF 校验失败</h1><p><a href='/emails'>返回邮箱登记</a></p></div>"), 403)
            eid = (f.get("id") or [""])[0]
            pwf = (f.get("password") or [""])[0]
            if eid:
                c = db(); c.execute("UPDATE email_accounts SET password=? WHERE id=?", (pwf, eid)); c.commit(); c.close()
            self.send_response(302); self.send_header("Location", "/emails"); self.end_headers(); return
        if path == "/emails/update":
            user = self.require_login()
            if not user: return
            f = self.read_form()
            if not self.csrf_ok(f):
                return self.send_html(page("CSRF 校验失败", user, "<div class='card'><h1>CSRF 校验失败</h1><p><a href='/emails'>返回邮箱仪表盘</a></p></div>"), 403)
            eid = (f.get("id") or [""])[0]
            if eid:
                c = db(); c.execute("UPDATE email_accounts SET provider=?, purpose=?, status=?, auxiliary_email=?, code_status=? WHERE id=?", ((f.get("provider") or [""])[0], (f.get("purpose") or [""])[0], (f.get("status") or [""])[0], (f.get("auxiliary_email") or [""])[0].strip(), (f.get("code_status") or ["等待中"])[0], eid)); c.commit(); c.close()
            self.send_response(302); self.send_header("Location", "/emails"); self.end_headers(); return
        if path == "/emails/batch":
            user = self.require_login()
            if not user: return
            f = self.read_form()
            if not self.csrf_ok(f):
                return self.send_html(page("CSRF 校验失败", user, "<div class='card'><h1>CSRF 校验失败</h1><p><a href='/emails'>返回邮箱仪表盘</a></p></div>"), 403)
            ids = f.get("ids", [])
            act = (f.get("batch_action") or [""])[0]
            if ids and act:
                c = db()
                for eid in ids:
                    if act == "delete":
                        c.execute("DELETE FROM email_accounts WHERE id=?", (eid,))
                    elif act == "set_code_arrived":
                        c.execute("UPDATE email_accounts SET code_status='已到达' WHERE id=?", (eid,))
                    elif act == "set_code_waiting":
                        c.execute("UPDATE email_accounts SET code_status='等待中' WHERE id=?", (eid,))
                c.commit(); c.close()
            self.send_response(302); self.send_header("Location", "/emails"); self.end_headers(); return
        if path == "/emails/export":
            user = self.require_login()
            if not user: return
            import csv, io
            q = self.q1("q", "").strip().lower(); prov_f = self.q1("provider", "").strip(); cs_f = self.q1("code_status", "").strip()
            c = db(); rows = c.execute("SELECT * FROM email_accounts ORDER BY id DESC").fetchall(); c.close()
            out = io.StringIO(); w = csv.writer(out); w.writerow(["id","provider","email","purpose","status","auxiliary_email","code_status","code_last_checked_at","code_last_email_at","code_last_email_from","code_last_email_subject","created_at"])
            for r in rows:
                if prov_f and r["provider"] != prov_f: continue
                if cs_f and (r["code_status"] if "code_status" in r.keys() else "") != cs_f: continue
                if q and q not in " ".join([str(r["email"] or ""), str(r["provider"] or ""), str(r["purpose"] or ""), str(r["auxiliary_email"] or "")]).lower(): continue
                w.writerow([r["id"], r["provider"], r["email"], r["purpose"], r["status"], r["auxiliary_email"] if "auxiliary_email" in r.keys() else "", r["code_status"] if "code_status" in r.keys() else "", r["code_last_checked_at"] if "code_last_checked_at" in r.keys() else "", r["code_last_email_at"] if "code_last_email_at" in r.keys() else "", r["code_last_email_from"] if "code_last_email_from" in r.keys() else "", r["code_last_email_subject"] if "code_last_email_subject" in r.keys() else "", r["created_at"]])
            data = out.getvalue().encode()
            self.send_response(200); self.send_header("Content-Type", "text/csv; charset=utf-8"); self.send_header("Content-Disposition", "attachment; filename=emails.csv"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        if path == "/emails/delete":
            user = self.require_login()
            if not user: return
            f = self.read_form()
            if not self.csrf_ok(f):
                return self.send_html(page("CSRF 校验失败", user, "<div class='card'><h1>CSRF 校验失败</h1><p>请重新打开邮箱登记页再试。</p></div>"), 403)
            eid = (f.get("id") or [""])[0]
            if eid:
                c = db(); c.execute("DELETE FROM email_accounts WHERE id=?", (eid,)); c.commit(); c.close()
            self.send_response(302); self.send_header("Location", "/emails"); self.end_headers(); return
        if path.startswith("/packs/") and path.endswith("/decide"):
            user = self.require_login()
            if not user: return
            date = path.split("/")[2]
            f = self.read_form()
            if not self.csrf_ok(f):
                return self.send_html(page("CSRF 校验失败", user, "<div class='card'><h1>CSRF 校验失败</h1><p>请重新打开包页再试。</p></div>"), 403)
            action = (f.get("action", [""])[0] or "")
            # single-item action via button value approve:<id> / reject:<id>
            single = (f.get("single_action", [""])[0] or "")
            ids = []
            if single and ":" in single:
                act, raw = single.split(":", 1)
                action = act
                try: ids = [int(raw)]
                except Exception: ids = []
            else:
                for raw in f.get("item_ids", []):
                    try: ids.append(int(raw))
                    except Exception: pass
            c = db(); errors, done = [], []
            for iid in ids:
                row = c.execute("SELECT * FROM items WHERE id=? AND pack_date=?", (iid, date)).fetchone()
                if not row: continue
                if action == "send_now":
                    c.execute("UPDATE items SET send_now=1 WHERE id=?", (iid,)); done.append(iid)
                    c.execute("INSERT INTO audit (ts, username, action, item_id, detail) VALUES (?,?,?,?,?)", (now_iso(), user, "mark_send_now", iid, f"pack={date}"))
                elif action == "cancel_send_now":
                    c.execute("UPDATE items SET send_now=0 WHERE id=?", (iid,)); done.append(iid)
                    c.execute("INSERT INTO audit (ts, username, action, item_id, detail) VALUES (?,?,?,?,?)", (now_iso(), user, "cancel_send_now", iid, f"pack={date}"))
                elif action == "reject":
                    c.execute("UPDATE items SET status='rejected' WHERE id=?", (iid,)); done.append(iid)
                    c.execute("INSERT INTO audit (ts, username, action, item_id, detail) VALUES (?,?,?,?,?)", (now_iso(), user, "reject", iid, f"pack={date}"))
                elif action == "approve":
                    if row["yellow"] and f"yellow_confirm_{iid}" not in f:
                        errors.append(f"条目 {iid} 亮黄未点名批，已跳过"); continue
                    if row["commitment"] and f"separate_confirm_{iid}" not in f:
                        errors.append(f"条目 {iid} 含承诺未单独批，已跳过"); continue
                    c.execute("UPDATE items SET status='approved', send_now=1 WHERE id=?", (iid,)); done.append(iid)
                    c.execute("INSERT INTO audit (ts, username, action, item_id, detail) VALUES (?,?,?,?,?)", (now_iso(), user, "approve", iid, f"pack={date} yellow_named={bool(row['yellow'])} commitment_separate={bool(row['commitment'])} auto_send_now=1"))
            c.commit(); c.close(); refresh_pack_status(date)
            import urllib.parse as _up
            if action in ("send_now", "cancel_send_now"):
                msg = f"已标记 {len(done)} 条" + ("（注意：只标记立即发送，邮件尚未发出，仍需发送任务执行后才会变成已发送）" if action == "send_now" and done else "") if done else "未选任何条目，请先勾选要处理的条目再点按钮"
            elif not done and not errors:
                msg = "未选任何条目，请先勾选要处理的条目再点按钮"
            elif action == "approve" and done:
                msg = f"已批准并授权立即发送 {len(done)} 条，监控任务会在 5 分钟内发出，不用再点立即发送" + (f"，跳过 {len(errors)} 条：{errors[0]}" if errors else "")
            else:
                msg = f"已处理 {len(done)} 条" + (f"，跳过 {len(errors)} 条：{errors[0]}" if errors else "")
            self.send_response(302); self.send_header("Location", f"/packs/{date}?msg=" + _up.quote(msg)); self.end_headers(); return
        if path.startswith("/packs/") and path.endswith("/delete"):
            user = self.require_login()
            if not user: return
            date = path.split("/")[2]
            f = self.read_form()
            if not self.csrf_ok(f):
                return self.send_html(page("CSRF 校验失败", user, "<div class='card'><h1>CSRF 校验失败</h1><p>请重新打开包页再试。</p></div>"), 403)
            c = db()
            sent = c.execute("SELECT COUNT(*) n FROM items WHERE pack_date=? AND sent=1", (date,)).fetchone()["n"]
            if sent:
                c.close()
                return self.send_html(page("不能删除", user, f"<div class='card'><h1>不能删除</h1><p>该包已有 {sent} 条已发送，删除被拒绝。</p><p><a href='/packs/{esc(date)}'>返回包</a></p></div>"), 400)
            c.execute("DELETE FROM items WHERE pack_date=?", (date,)); c.execute("DELETE FROM packs WHERE date=?", (date,)); c.commit(); c.close()
            audit(user, "delete_pack", None, f"pack={date}")
            self.send_response(302); self.send_header("Location", "/packs"); self.end_headers(); return
        self.send_html(page("未找到", None, "<div class='card'><h1>未找到</h1></div>"), 404)

if __name__ == "__main__":
    init_db()
    print(f"Approval dashboard v3.6 on http://127.0.0.1:{PORT} (behind Baota Nginx + HTTPS)", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
