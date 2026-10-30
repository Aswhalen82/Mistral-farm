#!/usr/bin/env python3
"""Cek proxy hidup + bisa dipakai buat tempmail.lol & Mistral.

  python check_proxies.py proxies.txt
  python check_proxies.py proxies.txt --out proxies_live.txt

Proxy yang lolos dua-duanya ditulis ke --out (default proxies_live.txt).
"""
import argparse
import concurrent.futures as cf
import sys
import time

import requests

from mistral_farmer import UA, load_proxies

TM_TEST = "https://api.tempmail.lol/v2/inbox/create"
MS_TEST = "https://api.mistral.ai/v1/models"


def probe(proxy, timeout=15):
    px = {"http": proxy["http"], "https": proxy["https"]}
    out = {"proxy": proxy["http"].split("@")[-1], "tm": "-", "ms": "-", "ok": False}
    t0 = time.time()
    try:
        r = requests.post(TM_TEST, json={}, timeout=timeout, proxies=px,
                          headers={"User-Agent": UA, "Content-Type": "application/json"})
        out["tm"] = r.status_code
    except Exception as e:
        out["tm"] = type(e).__name__
    try:
        r = requests.get(MS_TEST, timeout=timeout, proxies=px, headers={"User-Agent": UA})
        out["ms"] = r.status_code
    except Exception as e:
        out["ms"] = type(e).__name__
    out["ms_latency"] = round(time.time() - t0, 2)
    out["ok"] = out["tm"] in (200, 201) and out["ms"] in (200, 401, 403)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--out", default="proxies_live.txt")
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()

    proxies = load_proxies(args.file)
    if not proxies:
        print("tidak ada proxy terbaca dari", args.file)
        sys.exit(1)
    print(f"{len(proxies)} proxy, cek paralel {args.workers}...")

    live = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for res in ex.map(probe, proxies):
            tag = "LIVE" if res["ok"] else "DEAD"
            print(f"{tag} {res['proxy']:32s} tempmail={res['tm']} mistral={res['ms']} "
                  f"{res['ms_latency']}s")
            if res["ok"]:
                live.append(res["proxy"])

    with open(args.out, "w") as fh:
        fh.write("\n".join(live) + ("\n" if live else ""))
    print(f"\n{len(live)}/{len(proxies)} hidup -> {args.out}")
    if not live:
        print("Semua mati? Kalau proxy-nya butuh auth, set dulu:\n"
              "  export PROXY_USER=xxx PROXY_PASS=yyy")


if __name__ == "__main__":
    main()
