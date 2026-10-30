# Mistral AI Farmer → 9Router

Farmer akun Mistral AI **pure HTTP, tanpa browser**, pakai inbox `tempmail.lol`,
lalu otomatis bikin API key dan inject ke **9Router / ShinanoRouter**.

1 akun ≈ 40–90 detik (mayoritas nunggu OTP). Sukses rate 95–100% kalau proxy sehat.

```
tempmail.lol inbox → Ory Kratos signup → OTP verify → org+workspace auto
→ API key (admin.mistral.ai) → live test → push ke 9Router
```

---

## 1. Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Butuh Python 3.8+. Dependency cuma `requests`.

---

## 2. Proxy (disarankan)

Tanpa proxy bisa jalan, tapi `tempmail.lol` free tier **rate limit per IP**
(`429 Rate limited (free)`) dan Mistral juga throttle per IP.

```bash
cp proxies.txt.example proxies.txt
# isi proxy lo, 1 per baris
python check_proxies.py proxies.txt     # hasil yang hidup -> proxies_live.txt
```

Format proxy yang didukung: `ip`, `ip:port`, `ip:port:user:pass`, `http://user:pass@ip:port`.
Untuk format tanpa user/pass, isi kredensial lewat env:

```bash
export PROXY_USER=xxx
export PROXY_PASS=yyy
```

Proxy **datacenter** sudah cukup — Mistral nerima. Nggak perlu residential.

---

## 3. Token 9Router

Push butuh header `x-9r-cli-token`. Tiga cara, urut prioritas:

```bash
# a) langsung
--sr-token 0123456789abcdef

# b) env
export SR_TOKEN=0123456789abcdef

# c) file (default: ./9r_token.txt, lalu ~/9r_token.txt)
echo 0123456789abcdef > 9r_token.txt
```

Ambil token-nya (di host yang jalanin 9Router):

```bash
python mistral_farmer.py token          # butuh sudo buat baca file router
```

Manual:

```bash
python3 -c "import hashlib;print(hashlib.sha256((open('/var/lib/shinanorouter/machine-id').read().strip()+'9r-cli-auth'+open('/var/lib/shinanorouter/auth/cli-secret').read().strip()).encode()).hexdigest()[:16])"
```

Kalau router lo bukan ShinanoRouter, path data-dir-nya beda
(`~/.9router`, `~/.shinanorouter`, atau `$DATA_DIR`) — script nyoba semua otomatis.

---

## 4. Pakai

```bash
# farm 50 akun, 8 paralel, auto-push ke 9Router
.venv/bin/python mistral_farmer.py farm -n 50 -c 8 --proxy-file proxies_live.txt

# tanpa push (cuma simpan key)
.venv/bin/python mistral_farmer.py farm -n 10 -c 4 --no-push

# tes ulang semua key di JSONL (pakai mistral-large-4)
.venv/bin/python mistral_farmer.py verify --out accounts.jsonl

# push ulang JSONL ke 9Router (yang udah ada di-skip, nggak dobel)
.venv/bin/python mistral_farmer.py push --out accounts.jsonl

# router remote / node custom
.venv/bin/python mistral_farmer.py farm -n 5 -c 2 \
  --router http://10.0.0.5:20128 --sr-token $SR_TOKEN
```

Opsi penting:

| Flag | Arti |
|---|---|
| `-n` | jumlah akun |
| `-c` | concurrency (8 aman buat 32 proxy) |
| `--proxy-file` | file proxy |
| `--no-push` | nggak push ke router |
| `--node` | id provider di router (default: native `mistral`) |
| `--code-timeout` | batas tunggu OTP per akun (default 100s) |
| `--mail-api-key` | API key tempmail.lol kalau punya (limit lebih longgar) |

Output:

| File | Isi |
|---|---|
| `accounts.jsonl` | record lengkap: email, password, api_key, workspace, org, status test, `pushed` |
| `accounts_keys.txt` | `email:password:api_key:provider` — enak buat import massal |

---

## 5. Model free tier (tested Oct 2026)

| Tersedia ✅ | Kena 429 / butuh plan ❌ |
|---|---|
| `mistral-large-4` | `mistral-medium-*` |
| `ministral-3b / 8b / 14b` | `mistral-small-*` |
| `codestral-2508`, `mistral-code-*` | `magistral-*`, `mistral-vibe-*` |
| `voxtral-small-2507` (audio) | |

**Selalu tes pakai `mistral-large-4`.** Kalau tes pakai `mistral-small-latest`,
semua akun bakal kelihatan gagal padahal sehat (429 = limit plan, bukan akun mati).

Set model tes lain:

```bash
export MISTRAL_TEST_MODEL=ministral-8b-latest
```

---

## 6. Cara kerjanya (kalau mau modif)

`v2.auth.mistral.ai` cuma shell Next.js di atas **Ory Kratos** (`auth.mistral.ai`).
Semua endpoint bisa dipanggil langsung asal kirim `Accept: application/json`:

```
GET  /self-service/registration/browser        → {id, ui:{action, nodes}}
POST /self-service/registration?flow=<id>      → 200 {session:...}   (langsung login)
GET  /self-service/verification/browser        → flow
POST /self-service/verification?flow=<id>      {email, method:"code"}  → kirim OTP baru
POST /self-service/verification?flow=<id>      {code, method:"code"}   → sukses = redirect console
```

Setelah verified, org + Default Workspace **auto-dibuat**:

```
GET  https://admin.mistral.ai/api/workspaces                → items[0].uuid
POST https://admin.mistral.ai/api/billing/api-keys           {"name","workspace_uuid",
                                                              "primitive_access_scope":"personal_and_shared"}
```

### ⚠️ Pitfall paling mahal: OTP itu flow-bound

Kode OTP terikat ke flow verifikasi yang minta dia. Kalau kode dari flow lama
di-submit ke flow baru, Kratos balas **HTTP 200 tapi gagal diam-diam**
(flow di-render ulang, bukan redirect ke console) → `/api/workspaces` balik JSON flow
→ `KeyError: 'items'`.

Urutan benar (implementasi di `Kratos.verify`):

1. `seen = set(mail.codes())` **sebelum** minta kode baru
2. `POST verification {email, method:"code"}` → Kratos kirim kode baru
3. submit **hanya** kode yang belum ada di `seen`, ke `action` + `csrf` dari flow itu
4. anggap sukses kalau `status == 200 and "console.mistral.ai" in response.text`

### Pitfall lain

- **Nama konflik di 9Router** → `409 PROVIDER_NAME_CONFLICT`. Script pakai nama unik
  (`Mistral <email>`) dan punya fallback retry.
- **Provider `mistral` native** dipakai duluan. Kalau router lo nggak punya provider itu
  (400 `Invalid provider`), script bikin node `openai-compatible` ke `https://api.mistral.ai/v1`
  dengan prefix `mstrl` → model jadi `mstrl/mistral-large-4`.
- **429 dari Mistral saat signup** = throttle per IP → ganti proxy, script retry otomatis
  lewat `make_inbox`/rotasi.
- Jangan pakai prefix `mistral` buat node custom — bentrok sama provider built-in.

---

## 7. Deliverability tempmail.lol

Domain rotate otomatis (`paxeluni.com`, `xzcoot.com`, `lucktaro.com`, `kashre.com`, dll).
OTP biasanya masuk 3–8 detik. Kalau inbox creation balik 429, itu rate limit IP —
tambah proxy atau kasih jeda. Endpoint legacy (`/generate`) dipakai sebagai fallback.

Kalau butuh kapasitas besar dan stabil, opsi lain yang sudah dites: catch-all domain sendiri
(paling reliable tapi domain cepat kebakar) — taruh paling akhir.

---

## 8. Troubleshooting

| Gejala | Penyebab | Fix |
|---|---|---|
| `FAIL inbox 429` | rate limit IP tempmail.lol | pakai proxy / `--mail-api-key` |
| `FAIL signup 400 invalid` | email domain diblok | ganti domain / proxy |
| `FAIL verify no_code` | OTP telat / flow ke-reset | naikkan `--code-timeout`, ganti proxy |
| `FAIL workspace` + `items` | verifikasi gagal senyap | cek pitfall flow-bound di atas |
| `test 429` | model kena limit plan | tes pakai `mistral-large-4` |
| `[router] add_key gagal 409` | nama sudah ada | script retry nama unik otomatis |
| `[router] add_key gagal 401` | token salah | `mistral_farmer.py token` |
| `tanpa proxy` padahal ada file | file nggak kebaca | cek path / format baris |

---

## 9. File

| File | Fungsi |
|---|---|
| `mistral_farmer.py` | semua: mail, Kratos, key creation, test, 9Router push, CLI |
| `check_proxies.py` | cek proxy hidup buat tempmail.lol + Mistral |
| `proxies.txt.example` | contoh format proxy |
| `requirements.txt` | `requests` |

---

## Catatan operasional

- 1 signup per IP aman; ~3 akun per IP juga lolos. Concurrency 8 dengan 30+ proxy = stabil.
- Cloudflare di `auth.mistral.ai` nggak ganggu selama header `Accept`/`Origin`/`Referer` benar.
- Akun yang kena 429 belum mati — cuma kena limit harian. Key-nya tetap valid besok.
- Simpan `accounts.jsonl` di tempat aman: isinya kredensial akun + API key aktif.
