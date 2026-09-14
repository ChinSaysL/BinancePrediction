"""Collect Polymarket price history for rounds this bot already has quotes for.

Polymarket is a DATA SOURCE only -- execution stays on Binance. The question is
whether the gap between Polymarket's price and Binance's own quote, for the same
round on the same Chainlink stream, predicts Binance's settlement well enough to
clear the MEASURED cost of a Binance fill (+0.0977 over the displayed ask).

Writes one JSON line per (round, tau) observation.
"""
import json, sqlite3, time, urllib.request, sys

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")

def get(u, tries=3):
    for i in range(tries):
        try:
            r = urllib.request.Request(u, headers={"User-Agent": UA,
                                                   "Accept": "application/json"})
            return json.load(urllib.request.urlopen(r, timeout=25))
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(1.5 * (i + 1))

c = sqlite3.connect("data/btcpredict.db"); c.row_factory = sqlite3.Row
bn = {r["slug"]: r["winner"] for r in c.execute(
    "SELECT slug,winner FROM rounds WHERE winner IS NOT NULL "
    "AND slug LIKE '%-updown-5m-%'")}

# every closed Polymarket 5m market we can page through
mk = {}
for off in range(0, 2000, 100):
    ev = get("https://gamma-api.polymarket.com/events?closed=true&tag_slug=5m"
             f"&limit=100&offset={off}&order=endDate&ascending=false")
    if not ev:
        break
    for x in ev:
        s = x.get("slug") or ""
        if s not in bn:
            continue
        m = (x.get("markets") or [{}])[0]
        try:
            toks = json.loads(m.get("clobTokenIds") or "[]")
            outs = json.loads(m.get("outcomes") or "[]")
        except Exception:
            continue
        if len(toks) == 2 and "Up" in outs:
            mk[s] = toks[outs.index("Up")]
print(f"rounds on both venues with a known winner: {len(mk)}", flush=True)

out = open("/tmp/pmhist.jsonl", "w")
n = 0
for i, (slug, tok) in enumerate(sorted(mk.items())):
    ep = int(slug.rsplit("-", 1)[1])
    h = get(f"https://clob.polymarket.com/prices-history?market={tok}"
            f"&startTs={ep - 30}&endTs={ep + 300}")
    if not h or not h.get("history"):
        continue
    for pt in h["history"]:
        t, p = int(pt["t"]), float(pt["p"])
        tau = ep + 300 - t
        if not (20 <= tau <= 290):
            continue
        q = c.execute("""SELECT ts_ms, up_bid, up_ask, down_bid, down_ask, spot, strike
                         FROM quotes WHERE slug=? AND ts_ms BETWEEN ? AND ?
                         ORDER BY ABS(ts_ms - ?) LIMIT 1""",
                      (slug, (t - 3) * 1000, (t + 3) * 1000, t * 1000)).fetchone()
        if not q or q["up_ask"] is None or q["down_ask"] is None:
            continue
        out.write(json.dumps({
            "slug": slug, "fam": slug.rsplit("-", 1)[0], "t": t, "tau": tau,
            "pm_up": p, "bn_up_ask": q["up_ask"], "bn_up_bid": q["up_bid"],
            "bn_dn_ask": q["down_ask"], "bn_dn_bid": q["down_bid"],
            "spot": q["spot"], "strike": q["strike"],
            "won_up": int(bn[slug] == "Up")}) + "\n")
        n += 1
    if i % 25 == 0:
        print(f"  {i}/{len(mk)} rounds, {n} observations", flush=True)
        out.flush()
    time.sleep(0.15)
out.close()
print(f"DONE {n} observations from {len(mk)} rounds -> /tmp/pmhist.jsonl", flush=True)
