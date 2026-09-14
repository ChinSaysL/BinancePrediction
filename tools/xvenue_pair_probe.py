"""Cross-venue pair: take whichever direction is under $1.00. Read-only.

Both directions sum to roughly -(spread_BN + spread_PM + fee_BN), so when one
is profitable the other is not. The arm would evaluate both each tick and take
the better one, or neither.

Logs every observation to /tmp/xv7.jsonl for analysis.
"""
import json, sqlite3, time, urllib.request
UA=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36")
FEE_BN=0.0169
def get(u):
    r=urllib.request.Request(u,headers={"User-Agent":UA,"Accept":"application/json"})
    return json.load(urllib.request.urlopen(r,timeout=15))
c=sqlite3.connect("data/btcpredict.db"); c.row_factory=sqlite3.Row
out=open("/tmp/xv7.jsonl","w")
deadline=time.time()+520; n=0
while time.time()<deadline:
    try: ev=get("https://gamma-api.polymarket.com/events?closed=false&tag_slug=5m&limit=100")
    except Exception: time.sleep(4); continue
    for x in ev:
        s=x.get("slug") or ""
        if not s.startswith(("btc-updown-5m","eth-updown-5m","bnb-updown-5m")): continue
        try: ep=int(s.rsplit("-",1)[1])
        except Exception: continue
        tau=ep+300-time.time()
        if not (25<=tau<=275): continue
        r=c.execute("SELECT ts_ms,up_bid,up_ask,down_bid,down_ask FROM quotes "
                    "WHERE slug=? ORDER BY ts_ms DESC LIMIT 1",(s,)).fetchone()
        if not r or r["up_ask"] is None or r["down_ask"] is None: continue
        if (time.time()*1000-r["ts_ms"])/1000>3: continue
        m=(x.get("markets") or [{}])[0]
        try:
            toks=json.loads(m.get("clobTokenIds") or "[]")
            outs=json.loads(m.get("outcomes") or "[]")
        except Exception: continue
        if len(toks)!=2: continue
        bk={}
        for o,t in zip(outs,toks):
            try: b=get(f"https://clob.polymarket.com/book?token_id={t}")
            except Exception: bk={}; break
            a=[(float(z["price"]),float(z["size"])) for z in (b.get("asks") or [])]
            if not a: bk={}; break
            p=min(q for q,_ in a); sz=sum(q2 for q1,q2 in a if abs(q1-p)<1e-9)
            bk[o]=(p,sz)
        if len(bk)!=2: continue
        A=bk["Up"][0]+r["down_ask"]*(1+FEE_BN)          # PM Up + BN Down
        B=r["up_ask"]*(1+FEE_BN)+bk["Down"][0]          # BN Up + PM Down
        rec={"t":int(time.time()),"slug":s,"tau":round(tau),
             "bn_up_ask":r["up_ask"],"bn_dn_ask":r["down_ask"],
             "pm_up_ask":bk["Up"][0],"pm_dn_ask":bk["Down"][0],
             "pm_up_sz":bk["Up"][1],"pm_dn_sz":bk["Down"][1],
             "netA":round(1-A,4),"netB":round(1-B,4)}
        out.write(json.dumps(rec)+"\n"); out.flush(); n+=1
    time.sleep(5)
out.close()
print(f"logged {n} observations to /tmp/xv7.jsonl")
