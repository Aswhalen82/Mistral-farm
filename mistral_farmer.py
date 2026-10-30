#!/usr/bin/env python3
"""
Mistral AI account farmer (pure HTTP, no browser) + auto-inject ke 9Router.

Flow:
  tempmail.lol inbox  ->  Ory Kratos signup  ->  email OTP verification
  ->  auto org/workspace  ->  API key (admin.mistral.ai)  ->  live test
  ->  push key ke 9Router (native provider "mistral" / node openai-compatible)

Commands:
  farm     farm N akun + push ke 9Router
  verify   tes ulang semua key di JSONL
  push     push ulang key dari JSONL ke 9Router
  token    cetak CLI token 9Router lokal (butuh sudo)

Contoh:
  python mistral_farmer.py farm -n 50 -c 8
  python mistral_farmer.py farm -n 20 -c 4 --no-push
  python mistral_farmer.py farm -n 5 -c 2 --proxy-file proxies.txt
  python mistral_farmer.py verify --out accounts.jsonl
  python mistral_farmer.py push --out accounts.jsonl --router http://127.0.0.1:20128
"""

import argparse
import json
import os
import random
import re
import string
import sys
import threading
import time

import requests

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

AUTH = "https://auth.mistral.ai"          # Ory Kratos public API
ORIGIN = "https://v2.auth.mistral.ai"     # dipakai buat Origin/Referer
ADMIN = "https://admin.mistral.ai"        # workspace + api key
API = "https://api.mistral.ai/v1"         # chat completions
TM = "https://api.tempmail.lol/v2"        # disposable inbox
TM_LEGACY = "https://api.tempmail.lol/generate"  # fallback kalau free tier 429

# Model buat tes key. JANGAN pakai mistral-small/medium: 429 walau akun sehat.
TEST_MODEL = os.environ.get("MISTRAL_TEST_MODEL", "mistral-large-4")
CODE_RE = re.compile(r"verification process:\s*(\d{6})", re.I)

PRINT_LOCK = threading.Lock()


def log(msg):
    with PRINT_LOCK:
        print(msg, flush=True)


# ----------------------------------------------------------------------------
# Proxy helpers
# ----------------------------------------------------------------------------
def load_proxies(path=None, default_port=3129):
    """File proxy: host  |  host:port  |  host:port:user:pass  |  http://user:pass@host:port
    Kredensial bisa juga dari env PROXY_USER / PROXY_PASS."""
    if not path or not os.path.exists(path):
        return []
    user = os.environ.get("PROXY_USER", "")
    pw = os.environ.get("PROXY_PASS", "")
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "://" in line:
                url = line
            else:
                parts = line.split(":")
                if len(parts) == 1:
                    host = parts[0]
                    url = (f"http://{user}:{pw}@{host}:{default_port}" if user
                           else f"http://{host}:{default_port}")
                elif len(parts) == 2:
                    host, port = parts
                    url = f"http://{user}:{pw}@{host}:{port}" if user else f"http://{host}:{port}"
                elif len(parts) == 3:
                    host, port, u = parts
                    url = f"http://{u}:{pw}@{host}:{port}"
                elif len(parts) >= 4:
                    host, port, u, p = parts[:4]
                    url = f"http://{u}:{p}@{host}:{port}"
                else:
                    continue
            out.append({"http": url, "https": url})
    return out


# ----------------------------------------------------------------------------
# tempmail.lol
# ----------------------------------------------------------------------------
class TempMailLol:
    name = "tm_lol"

    def __init__(self, proxy=None, api_key=None):
        self.s = requests.Session()
        self.s.proxies = proxy or {}
        self.headers = {"User-Agent": UA, "Content-Type": "application/json",
                        "Accept": "application/json"}
        if api_key:
            self.headers["Authorization"] = "Bearer " + api_key
        self.addr = None
        self.tok = None

    def new(self, domain=None, retries=3):
        """Bikin inbox. Endpoint v2 dulu, kalau 429 fallback ke endpoint legacy."""
        last = ""
        for att in range(retries):
            for path, use_get in ((f"{TM}/inbox/create", False),
                                  (TM_LEGACY, True)):
                try:
                    if use_get:
                        r = self.s.get(path, headers=self.headers, timeout=25)
                    else:
                        r = self.s.post(path, json={"domain": domain} if domain else {},
                                        headers=self.headers, timeout=25)
                    if r.status_code == 200:
                        d = r.json()
                        if d.get("address") and d.get("token"):
                            self.addr, self.tok = d["address"], d["token"]
                            return self.addr
                    last = f"inbox {r.status_code} {r.text[:80]}"
                except Exception as e:
                    last = f"{type(e).__name__}: {e}"
            time.sleep(2 + att * 3)
        raise RuntimeError(last or "inbox failed")

    def messages(self):
        if not self.tok:
            return []
        try:
            r = self.s.get(f"{TM}/inbox", params={"token": self.tok},
                           headers=self.headers, timeout=25)
            if r.status_code != 200:
                return []
            return r.json().get("emails") or []
        except Exception:
            return []

    def codes(self):
        out = []
        for m in self.messages():
            blob = " ".join(str(m.get(k) or "") for k in ("subject", "body", "html"))
            found = CODE_RE.findall(blob)
            if not found:
                # fallback: 6 digit berdiri sendiri di email yang nyebut verifikasi
                if re.search(r"verif|code|kode", blob, re.I):
                    found = re.findall(r"(?<!\d)(\d{6})(?!\d)", blob)
            out += found
        return out

    def wait_code(self, exclude=(), timeout=100, interval=2.5):
        t0 = time.time()
        while time.time() - t0 < timeout:
            for c in self.codes():
                if c not in exclude:
                    return c
            time.sleep(interval)
        return None


# ----------------------------------------------------------------------------
# Ory Kratos (auth.mistral.ai)
# ----------------------------------------------------------------------------
class Kratos:
    def __init__(self, proxy=None):
        self.s = requests.Session()
        self.s.proxies = proxy or {}
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": ORIGIN,
            "Referer": ORIGIN + "/",
        })

    # --- flow helpers -------------------------------------------------------
    def _get_flow(self, kind):
        r = self.s.get(f"{AUTH}/self-service/{kind}/browser", timeout=30)
        d = r.json()
        ui = d["ui"]
        csrf = next(n["attributes"]["value"] for n in ui["nodes"]
                    if n["attributes"].get("name") == "csrf_token")
        return ui["action"], csrf

    @staticmethod
    def _action_csrf(payload):
        ui = payload["ui"]
        csrf = next(n["attributes"]["value"] for n in ui["nodes"]
                    if n["attributes"].get("name") == "csrf_token")
        return ui["action"], csrf, ui

    @staticmethod
    def _ui_messages(payload):
        msgs = []
        try:
            for n in payload.get("ui", {}).get("nodes", []):
                for m in n.get("messages", []) or []:
                    msgs.append(m.get("text", ""))
        except Exception:
            pass
        return " | ".join(x for x in msgs if x)

    # --- steps --------------------------------------------------------------
    def signup(self, email, password, first, last):
        act, csrf = self._get_flow("registration")
        r = self.s.post(act, timeout=30, json={
            "csrf_token": csrf,
            "method": "password",
            "password": password,
            "traits": {"email": email, "name": {"first": first, "last": last}},
            "transient_payload": {},
        })
        return r

    def request_code(self, email):
        """Mulai flow verifikasi BARU dan minta kode baru. Return (action, csrf)."""
        act, csrf = self._get_flow("verification")
        r = self.s.post(act, timeout=30, json={
            "email": email, "method": "code", "csrf_token": csrf})
        d = r.json()
        act2, csrf2, _ = self._action_csrf(d)
        return act2, csrf2, r

    def submit_code(self, action, csrf, code):
        """PENTING: submit ke flow yang SAMA dengan yang minta kode."""
        return self.s.post(action, timeout=30, json={
            "code": code, "method": "code", "csrf_token": csrf})

    def verify(self, email, mail, timeout=100):
        """Snapshot kode lama -> minta kode baru -> submit kode yang belum pernah ada."""
        seen = set(mail.codes())
        act, csrf, r = self.request_code(email)
        if r.status_code not in (200, 201):
            return False, f"request_code {r.status_code}", None
        t0 = time.time()
        while time.time() - t0 < timeout:
            for code in mail.codes():
                if code in seen:
                    continue
                seen.add(code)
                r2 = self.submit_code(act, csrf, code)
                if r2.status_code == 200 and "console.mistral.ai" in r2.text:
                    return True, "ok", code
            time.sleep(2.5)
        return False, "no_code", None

    def login(self, email, password):
        act, csrf = self._get_flow("login")
        r = self.s.post(act, timeout=30, json={
            "csrf_token": csrf, "identifier": email,
            "method": "identifier_first", "transient_payload": {}})
        d = r.json()
        act2, csrf2, ui = self._action_csrf(d)
        names = [n["attributes"].get("name") for n in ui["nodes"]]
        if "password" not in names:
            return r, f"unexpected nodes {names}"
        r2 = self.s.post(act2, timeout=30, json={
            "csrf_token": csrf2, "password": password,
            "method": "password", "identifier": email})
        return r2, "ok"

    # --- post-verification --------------------------------------------------
    def workspaces(self):
        return self.s.get(f"{ADMIN}/api/workspaces", timeout=30)

    def create_key(self):
        ws_r = self.workspaces()
        ws = ws_r.json()["items"][0]["uuid"]
        org_uuid = None
        try:
            org = self.s.get(f"{ADMIN}/api/users/organizations", timeout=30).json()
            if isinstance(org, list) and org:
                org_uuid = org[0].get("uuid")
        except Exception:
            pass
        name = "k" + "".join(random.choices(string.ascii_lowercase, k=8))
        rk = self.s.post(f"{ADMIN}/api/billing/api-keys", timeout=30, json={
            "name": name, "workspace_uuid": ws,
            "primitive_access_scope": "personal_and_shared"})
        key = None
        if rk.status_code in (200, 201):
            d = rk.json()
            key = d.get("key") or d.get("plaintext")
        return ws, org_uuid, key, rk


def test_key(key, model=TEST_MODEL, proxy=None, timeout=60):
    try:
        r = requests.post(f"{API}/chat/completions", timeout=timeout, proxies=proxy or {},
                          headers={"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"},
                          json={"model": model, "max_tokens": 5,
                                "messages": [{"role": "user", "content": "Say OK"}]})
        return r.status_code, r.text[:200]
    except Exception as e:
        return "ERR", str(e)[:150]


def gen_password():
    return ("Mst" + "".join(random.choices(string.ascii_letters + string.digits, k=10))
            + "!" + str(random.randint(10, 99)))


FIRST = ["Marco", "Luca", "Andrea", "Paolo", "Sofia", "Giulia", "Elena",
         "Matteo", "Davide", "Chiara", "Nicolas", "Emma", "Liam", "Mia"]
LAST = ["Bellini", "Rossi", "Conti", "Romano", "Greco", "Marino", "Costa",
        "Rizzo", "Ferrari", "Galli", "Moreau", "Dubois", "Weber", "Novak"]


def new_identity():
    return random.choice(FIRST), random.choice(LAST)


# ----------------------------------------------------------------------------
# 9Router client
# ----------------------------------------------------------------------------
class Router:
    """ShinanoRouter / 9Router: POST /api/providers butuh header x-9r-cli-token."""

    NATIVE = "mistral"
    NODE_NAME = "Mistral AI"
    NODE_PREFIX = "mstrl"

    def __init__(self, url, token, node=None, prefix=None, timeout=30):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.h = {"x-9r-cli-token": token, "Content-Type": "application/json"}
        self.node = node or self.NATIVE
        self.node_explicit = bool(node)
        self.prefix = prefix or self.NODE_PREFIX
        self._lock = threading.Lock()
        self.count = 0
        self._names = None

    # -- dedupe --------------------------------------------------------------
    def existing_names(self):
        with self._lock:
            if self._names is None:
                self._names = {c.get("name") for c in self.connections()
                               if c.get("provider") == self.node}
            return self._names

    # -- low level -----------------------------------------------------------
    def _post(self, path, body):
        return requests.post(self.url + path, headers=self.h, json=body, timeout=self.timeout)

    def _get(self, path):
        return requests.get(self.url + path, headers=self.h, timeout=self.timeout)

    def connections(self):
        try:
            d = self._get("/api/providers").json()
            return d.get("connections", [])
        except Exception:
            return []

    # -- provider setup ------------------------------------------------------
    def ensure_node(self):
        """Coba native provider 'mistral'. Kalau tidak didukung, bikin node openai-compatible."""
        if self.node_explicit:
            return True
        if any(h.get("provider") == self.NATIVE for h in self.connections()):
            self.node = self.NATIVE
            return True
        # coba native dulu dengan key dummy? tidak perlu: cukup cek daftar node native
        # (native provider tidak muncul di /api/provider-nodes)
        self.node = self.NATIVE
        return True

    def use_custom_node(self):
        """Fallback: buat/ambil node openai-compatible untuk api.mistral.ai."""
        for n in (self._get("/api/provider-nodes").json().get("nodes") or []):
            if n.get("baseUrl", "").startswith("https://api.mistral.ai"):
                self.node = n["id"]
                self.prefix = n.get("prefix") or self.prefix
                return True
        r = self._post("/api/provider-nodes", {
            "name": self.NODE_NAME, "prefix": self.prefix, "apiType": "chat",
            "baseUrl": "https://api.mistral.ai/v1", "type": "openai-compatible"})
        if r.status_code in (200, 201):
            self.node = r.json().get("id") or r.json().get("node", {}).get("id")
            return bool(self.node)
        log(f"[router] gagal bikin node: {r.status_code} {r.text[:120]}")
        return False

    def add_key(self, key, name=None, priority=None):
        with self._lock:
            self.count += 1
            idx = self.count
        name = name or f"Mistral #{int(time.time()) % 10000000:07d}{idx:03d}"
        priority = priority or idx
        if name in self.existing_names():
            return "exists"  # sudah pernah di-push, skip
        body = {"provider": self.node, "apiKey": key, "name": name, "priority": priority}

        r = self._post("/api/providers", body)
        if r.status_code in (200, 201):
            self.existing_names().add(name)
            return "added"

        # nama bentrok -> 9Router nolak (409). Retry sekali dengan nama unik.
        if r.status_code == 409:
            uniq = f"{name}-{int(time.time() * 1000) % 1000000}"
            r = self._post("/api/providers", dict(body, name=uniq))
            if r.status_code in (200, 201):
                self.existing_names().add(uniq)
                return "added"

        # native provider tidak didukung router ini -> pindah ke node openai-compatible
        if (not self.node_explicit and self.node == self.NATIVE
                and r.status_code in (400, 404)
                and ("invalid provider" in r.text.lower() or "not found" in r.text.lower())):
            if self.use_custom_node():
                r = self._post("/api/providers", dict(body, provider=self.node))
                if r.status_code in (200, 201):
                    self.existing_names().add(name)
                    return "added"

        log(f"[router] add_key gagal: {r.status_code} {r.text[:140]}")
        return False


DATA_DIRS = [
    os.environ.get("DATA_DIR", ""),
    "/var/lib/shinanorouter",
    os.path.expanduser("~/.shinanorouter"),
    os.path.expanduser("~/.9router"),
    os.path.expanduser("~/.config/shinanorouter"),
]


def cli_token_from_local():
    """Token CLI 9Router = sha256(machine-id + '9r-cli-auth' + cli-secret)[:16]."""
    import hashlib
    for d in DATA_DIRS:
        if not d:
            continue
        mid_p = os.path.join(d, "machine-id")
        sec_p = os.path.join(d, "auth", "cli-secret")
        if os.path.exists(mid_p) and os.path.exists(sec_p):
            mid = open(mid_p).read().strip()
            sec = open(sec_p).read().strip()
            return hashlib.sha256((mid + "9r-cli-auth" + sec).encode()).hexdigest()[:16]
    raise FileNotFoundError("machine-id / auth/cli-secret 9Router tidak ketemu")


def load_router_token(path=None):
    if os.environ.get("SR_TOKEN"):
        return os.environ["SR_TOKEN"].strip()
    if path and os.path.exists(path):
        return open(path).read().strip()
    for guess in ("9r_token.txt", os.path.expanduser("~/9r_token.txt")):
        if os.path.exists(guess):
            return open(guess).read().strip()
    try:
        return cli_token_from_local()
    except Exception:
        return None


# ----------------------------------------------------------------------------
# One account
# ----------------------------------------------------------------------------
def make_inbox(proxies=None, api_key=None, tries=4):
    """Bikin inbox dengan rotasi proxy (tempmail.lol free = rate limit per IP)."""
    cand = list(proxies) if proxies else [None]
    last = ""
    for i in range(min(tries, len(cand)) if proxies else 1):
        mail = TempMailLol(proxy=cand[i], api_key=api_key)
        try:
            addr = mail.new()
            return mail, addr
        except Exception as e:
            last = str(e)[:160]
            time.sleep(1.2)
    return None, last


def register_one(proxy=None, mail_api_key=None, send_timeout=100, proxies=None):
    mail, addr = make_inbox(proxies or ([proxy] if proxy else None), mail_api_key)
    if mail is None:
        return {"ok": False, "stage": "inbox", "resp": addr}

    pw = gen_password()
    first, last = new_identity()
    k = Kratos(proxy)

    r = k.signup(addr, pw, first, last)
    if r.status_code != 200 or '"session"' not in r.text:
        msg = k._ui_messages(r.json()) if r.text.startswith("{") else r.text[:150]
        return {"ok": False, "stage": "signup", "email": addr, "status": r.status_code,
                "resp": (msg or r.text)[:200]}

    ok, why, code = k.verify(addr, mail, timeout=send_timeout)
    if not ok:
        return {"ok": False, "stage": "verify", "email": addr, "resp": why}

    ws, org, key, rk = k.create_key()
    if not key:
        body = rk.text[:200]
        stage = "key"
        if "items" in body or rk.status_code != 200:
            stage = "workspace"
        return {"ok": False, "stage": stage, "email": addr,
                "status": rk.status_code, "resp": body}

    status, body = test_key(key, proxy=None)
    return {"ok": status == 200, "stage": "test" if status != 200 else "done",
            "email": addr, "password": pw, "api_key": key, "provider": "tm_lol",
            "workspace": ws, "org": org, "model": TEST_MODEL, "test": status,
            "test_body": body if status != 200 else None,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------
def cmd_farm(args):
    proxies = load_proxies(args.proxy_file)
    if proxies:
        log(f"[farm] {len(proxies)} proxy dimuat")
    else:
        log("[farm] tanpa proxy (direct IP) - aman 1 signup/IP, tapi kalau kena 429 pakai proxy")

    router = None
    if not args.no_push:
        token = load_router_token(args.sr_token_file or args.token)
        if not token:
            log("[farm] CLI token 9Router tidak ketemu -> push dimatikan (pakai --sr-token / SR_TOKEN)")
        else:
            router = Router(args.router, token, node=args.node, prefix=args.prefix)
            router.ensure_node()

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    stats = {"ok": 0, "fail": 0}
    lock = threading.Lock()
    keyfile = os.path.splitext(args.out)[0] + "_keys.txt"

    def work(i):
        proxy = proxies[i % len(proxies)] if proxies else None
        rotated = ((proxies[i % len(proxies):] + proxies[:i % len(proxies)])
                   if proxies else [])
        try:
            rec = register_one(proxy, args.mail_api_key, args.code_timeout, proxies=rotated)
        except Exception as e:
            rec = {"ok": False, "stage": "exception", "err": f"{type(e).__name__}: {e}"[:160]}

        with lock:
            if rec.get("ok"):
                stats["ok"] += 1
                if router:
                    rec["pushed"] = router.add_key(
                        rec["api_key"], name=f"Mistral {rec['email']}")
            else:
                stats["fail"] += 1
            with open(args.out, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
            if rec.get("ok") and rec.get("api_key"):
                with open(keyfile, "a") as fh:
                    fh.write(f"{rec['email']}:{rec['password']}:{rec['api_key']}:tm_lol\n")
            tag = "OK  " if rec.get("ok") else "FAIL"
            log(f"{tag} {rec.get('email', '-'):38s} {rec.get('stage', ''):10s} "
                f"{str(rec.get('resp') or rec.get('test_body') or '')[:60]} "
                f"[{stats['ok']}ok/{stats['fail']}fail]")

    threads = []
    for i in range(args.count):
        while sum(1 for t in threads if t.is_alive()) >= args.concurrency:
            time.sleep(0.4)
        t = threading.Thread(target=work, args=(i,), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    log(f"\nDONE ok={stats['ok']} fail={stats['fail']} -> {args.out}")
    log(f"keys: {keyfile}")


def iter_jsonl(path):
    if not os.path.exists(path):
        log(f"[!] file tidak ada: {path}")
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def cmd_verify(args):
    router = None
    if not args.no_push:
        token = load_router_token(args.sr_token_file or args.token)
        if token:
            router = Router(args.router, token, node=args.node, prefix=args.prefix)

    seen, ok, bad = set(), 0, 0
    for rec in iter_jsonl(args.out):
        key = rec.get("api_key")
        if not key or key in seen:
            continue
        seen.add(key)
        status, body = test_key(key, proxy=None)
        good = status == 200
        ok += good
        bad += not good
        if good and router and args.repush:
            router.add_key(key, name=f"Mistral {rec.get('email') or len(seen)}")
        log(f"{'OK  ' if good else 'BAD '} {rec.get('email','-')[:38]:38s} {status}")
    log(f"\nDONE ok={ok} bad={bad} total={len(seen)}")


def cmd_push(args):
    token = load_router_token(args.sr_token_file or args.token)
    if not token:
        log("[!] token 9Router tidak ada (--sr-token / SR_TOKEN / 9r_token.txt)")
        return
    router = Router(args.router, token, node=args.node, prefix=args.prefix)
    if not router.ensure_node():
        log("[!] provider node tidak siap")
        return
    seen, pushed = set(), 0
    for rec in iter_jsonl(args.out):
        key = rec.get("api_key")
        if not key or key in seen:
            continue
        seen.add(key)
        res = router.add_key(key, name=f"Mistral {rec.get('email') or len(seen)}")
        if res == "added":
            pushed += 1
        log(f"{'PUSH' if res == 'added' else 'SKIP'} {len(seen)} {rec.get('email','-')}")
    log(f"\nDONE pushed={pushed} total={len(seen)} provider={router.node}")


def cmd_token(args):
    try:
        print(cli_token_from_local())
    except Exception as e:
        print(f"gagal baca file router (butuh sudo?): {e}", file=sys.stderr)
        sys.exit(1)


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Mistral AI farmer + 9Router auto-inject")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--out", default="accounts.jsonl", help="output JSONL")
        p.add_argument("--router", default=os.environ.get("SR_URL", "http://127.0.0.1:20128"))
        p.add_argument("--sr-token", dest="sr_token", default=None, help="9Router CLI token")
        p.add_argument("--sr-token-file", dest="sr_token_file", default=None)
        p.add_argument("--token", default=None, help="alias --sr-token")
        p.add_argument("--node", default=None, help="provider id 9Router (default: native 'mistral')")
        p.add_argument("--prefix", default=None, help="prefix kalau bikin node custom (default mstrl)")
        p.add_argument("--no-push", action="store_true", help="jangan push ke 9Router")

    f = sub.add_parser("farm", help="farm N akun")
    f.add_argument("-n", "--count", type=int, default=10)
    f.add_argument("-c", "--concurrency", type=int, default=6)
    f.add_argument("--proxy-file", default=None)
    f.add_argument("--mail-api-key", default=os.environ.get("TEMPMail_API_KEY"),
                   help="opsional: API key tempmail.lol")
    f.add_argument("--code-timeout", type=int, default=100)
    common(f)
    f.set_defaults(func=cmd_farm)

    v = sub.add_parser("verify", help="tes ulang semua key di JSONL")
    v.add_argument("--repush", action="store_true", help="push key yang valid ke 9Router")
    common(v)
    v.set_defaults(func=cmd_verify)

    p = sub.add_parser("push", help="push key dari JSONL ke 9Router")
    common(p)
    p.set_defaults(func=cmd_push)

    t = sub.add_parser("token", help="cetak CLI token 9Router lokal")
    t.set_defaults(func=cmd_token)

    args = ap.parse_args()
    if not getattr(args, "cmd", None):
        ap.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
