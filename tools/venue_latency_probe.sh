#!/bin/bash
# WHERE SHOULD THE BOT LIVE? Run this ON a candidate host and compare.
#
# Self-contained on purpose: no repo, no venv, no python. `scp` it to a trial
# VPS, run it, read the last line. Everything it needs is curl.
#
# WHY THIS IS THE RIGHT MEASUREMENT. `book.py` records that no consumer-side
# design removes the book lag -- backlog cap 400 gives venue lag 275-580ms,
# cap 32 gives 665-871ms; the frames themselves arrive that late. So the lag is
# DELIVERY, and delivery is geography. What this prints is the part of a venue
# round trip that no code change can touch:
#
#     origin fetch = TTFB - TCP connect
#
# TCP connect is the distance to the Cloudflare EDGE. TTFB is the distance to
# the DATA. `cf-cache-status: DYNAMIC` on every venue response means nothing is
# served from the edge, so the difference is paid on every single call.
#
# MEASURED 2026-09-19, same command both sides:
#     Tokyo  (the live VPS, edge NRT)   tcp   1.5ms   ttfb 254ms   origin ~252ms
#     Tunis  (a laptop,      edge SOF)  tcp  48.0ms   ttfb 209ms   origin ~161ms
#
# A laptop on domestic broadband in Tunisia reaches the venue's origin ~90ms
# faster than the trading box does, which is what makes this worth testing
# rather than assuming. A host that prints an origin fetch well under 150ms is
# strictly better placed than what we run on now.
#
# ⛔ IT DOES NOT PREDICT P&L. `pm_two_book.py` priced a PERFECT feed at +77% on
# held-out days; that is the ceiling for removing ALL of the lag, not the value
# of removing some of it. Treat a lower number here as necessary, not
# sufficient, and re-measure the book lag itself before believing anything.
set -u
N=${1:-8}
H=clob.polymarket.com
W=ws-subscriptions-clob.polymarket.com

echo "host:  $(curl -s --max-time 8 https://ipinfo.io/json 2>/dev/null \
          | tr -d '{}" ' | grep -iE '^ *(city|country|org)' | tr '\n' ' ')"
echo "edge:  $(curl -s -D - -o /dev/null --max-time 10 "https://$H/" 2>/dev/null \
          | grep -i '^cf-ray' | tr -d '\r' | awk -F- '{print $NF}')"
echo

probe () {                       # $1 = host, $2 = label
  local tot_t=0 tot_f=0 n=0
  for _ in $(seq 1 "$N"); do
    read -r tcp ttfb <<< "$(curl -s -o /dev/null --max-time 10 \
      -w '%{time_connect} %{time_starttransfer}' "https://$1/" 2>/dev/null)"
    [ -z "${ttfb:-}" ] && continue
    tot_t=$(awk -v a="$tot_t" -v b="$tcp"  'BEGIN{print a+b}')
    tot_f=$(awk -v a="$tot_f" -v b="$ttfb" 'BEGIN{print a+b}')
    n=$((n+1))
  done
  [ "$n" -eq 0 ] && { echo "  $2: no response"; return; }
  awk -v t="$tot_t" -v f="$tot_f" -v n="$n" -v l="$2" 'BEGIN{
    tcp=t/n*1000; ttfb=f/n*1000;
    printf "  %-22s tcp %6.1fms   ttfb %7.1fms   ORIGIN FETCH %7.1fms\n",
           l, tcp, ttfb, ttfb-tcp}'
}

probe "$H" "clob (orders)"
probe "$W" "ws  (the book feed)"
echo
echo "  compare against the live box: Tokyo/NRT origin fetch ~252ms"
