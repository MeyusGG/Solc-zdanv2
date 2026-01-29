import os
import asyncio
import time
import threading
from typing import List, Optional, Dict, Tuple

import httpx
from telegram import Bot
from fastapi import FastAPI
import uvicorn

# =========================
# ENV
# =========================
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])

MIN_USD = float(os.getenv("MIN_USD", "100"))
POLL_SEC = int(os.getenv("POLL_SEC", "30"))

# RPC failover list (istersen ENV ile override edebilirsin)
RPC_URLS_ENV = os.getenv("RPC_URLS", "").strip()
DEFAULT_RPC_URLS = [
    "https://rpc.solana.com",
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet-beta.solana.com",
]
RPC_URLS = [u.strip() for u in RPC_URLS_ENV.split(",") if u.strip()] or DEFAULT_RPC_URLS

# Token mints
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL_MINT = "So11111111111111111111111111111111111111112"

TOP5 = [
    "GYQwwvyL4UTrjoNChC1KNWpkYWwB1uuc53nUvpQH5gN2",
    "DP7G43VPwR5Ab5rcjrCnvJ8UgvRXRHTWscMjRD1eSdGC",
    "As1W8fHawur7zrBmNkYEA4ANRy9ERzKUs71X7N59qAmn",
    "JAmx4Wsh7cWXRzQuVt3TCKAyDfRm9HA7ztJa4f7RM8h9",
    "21kMe9Ztcj3qLSN4Re2v9XQfXBrvJnJPHkw1CbaoPDnT",
]

bot = Bot(BOT_TOKEN)

# =========================
# FASTAPI (Render ping)
# =========================
app = FastAPI()

@app.get("/")
def home():
    return {
        "status": "alive",
        "min_usd": MIN_USD,
        "poll_sec": POLL_SEC,
        "rpc_urls": RPC_URLS,
    }

def start_web():
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

# =========================
# HELPERS
# =========================
def short(addr: str) -> str:
    return addr[:4] + "…" + addr[-4:]

async def send(msg: str):
    await bot.send_message(chat_id=CHAT_ID, text=msg, disable_web_page_preview=True)

def fmt_time_utc(ts: Optional[int]) -> str:
    if not ts:
        return "?"
    return time.strftime("%d %b %H:%M", time.gmtime(ts)) + " UTC"

# =========================
# RPC CLIENT (failover)
# =========================
class Rpc:
    def __init__(self, urls: List[str]):
        self.urls = urls
        self.i = 0
        self.client = httpx.AsyncClient(timeout=25)

    def _rotate(self):
        self.i = (self.i + 1) % len(self.urls)

    async def call(self, method: str, params):
        last_err = None
        for _ in range(len(self.urls) * 2):
            url = self.urls[self.i]
            try:
                r = await self.client.post(
                    url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    headers={"Content-Type": "application/json"},
                )

                if r.status_code in (403, 429) or r.status_code >= 500:
                    last_err = f"HTTP {r.status_code} @ {url}"
                    self._rotate()
                    await asyncio.sleep(1.2)
                    continue

                r.raise_for_status()
                data = r.json()

                if "error" in data:
                    last_err = f"RPC error {data['error']} @ {url}"
                    self._rotate()
                    await asyncio.sleep(1.2)
                    continue

                return data["result"]

            except Exception as e:
                last_err = f"{type(e).__name__}: {e} @ {url}"
                self._rotate()
                await asyncio.sleep(1.2)

        raise RuntimeError(last_err or "All RPC failed")

rpc = Rpc(RPC_URLS)

# =========================
# SOL PRICE (CoinGecko cache)
# =========================
_sol_cache = {"ts": 0.0, "price": 0.0}

async def sol_price_usd() -> float:
    now = time.time()
    if _sol_cache["price"] and (now - _sol_cache["ts"]) < 45:
        return _sol_cache["price"]

    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "solana", "vs_currencies": "usd"},
        )
        r.raise_for_status()
        price = float(r.json()["solana"]["usd"])
        _sol_cache["price"] = price
        _sol_cache["ts"] = now
        return price

# =========================
# TOKEN META via SOLSCAN (cache)
# =========================
_token_cache: Dict[str, Dict[str, str]] = {}

async def token_meta_solscan(mint: str) -> Dict[str, str]:
    """
    Solscan public meta: çoğu yeni tokenın name/symbol bilgisi gelir.
    Bulamazsa Unknown + mint kısaltması döner.
    """
    if mint in _token_cache:
        return _token_cache[mint]

    # Solscan public api
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("https://public-api.solscan.io/token/meta", params={"tokenAddress": mint})
            if r.status_code == 200:
                js = r.json() or {}
                sym = js.get("symbol") or mint[:4]
                name = js.get("name") or "Unknown"
                meta = {"symbol": sym, "name": name}
                _token_cache[mint] = meta
                return meta
    except:
        pass

    meta = {"symbol": mint[:4], "name": "Unknown"}
    _token_cache[mint] = meta
    return meta

# =========================
# TX PARSERS (real token + SOL delta)
# =========================
def owner_sol_delta(tx: dict, owner: str) -> Optional[float]:
    """
    Owner SOL (lamports) change. Negative=spent SOL, Positive=received SOL.
    """
    try:
        meta = tx.get("meta") or {}
        pre = meta.get("preBalances") or []
        post = meta.get("postBalances") or []
        msg = (tx.get("transaction") or {}).get("message") or {}
        keys = msg.get("accountKeys") or []

        idx = None
        for i, k in enumerate(keys):
            pub = k.get("pubkey") if isinstance(k, dict) else k
            if pub == owner:
                idx = i
                break

        if idx is None or idx >= len(pre) or idx >= len(post):
            return None

        return (post[idx] - pre[idx]) / 1e9
    except:
        return None

def owner_usdc_delta(tx: dict, owner: str) -> Optional[float]:
    """
    If USDC moved in owner's token accounts.
    Negative=spent USDC (buy), Positive=received USDC (sell).
    """
    try:
        meta = tx.get("meta") or {}
        pre = meta.get("preTokenBalances") or []
        post = meta.get("postTokenBalances") or []

        def collect(arr):
            out = {}
            for x in arr:
                if x.get("mint") != USDC_MINT:
                    continue
                if x.get("owner") != owner:
                    continue
                idx = x.get("accountIndex")
                ui = x.get("uiTokenAmount", {})
                amt = ui.get("uiAmount")
                if amt is None:
                    s = ui.get("uiAmountString")
                    amt = float(s) if s else None
                out[idx] = amt
            return out

        pre_m = collect(pre)
        post_m = collect(post)

        deltas = []
        for idx, post_amt in post_m.items():
            if post_amt is None:
                continue
            pre_amt = pre_m.get(idx, 0.0) or 0.0
            deltas.append(post_amt - pre_amt)

        if not deltas:
            return None
        return sum(deltas)
    except:
        return None

def find_real_token_by_balance_increase(tx: dict, owner: str) -> Optional[Tuple[str, float]]:
    """
    Owner’ın NET token artışını bulur:
    - preTokenBalances vs postTokenBalances
    - USDC/WSOL hariç
    - En çok artan mint = en muhtemel alınan token
    Döner: (mint, delta_amount)
    """
    try:
        meta = tx.get("meta") or {}
        pre = meta.get("preTokenBalances") or []
        post = meta.get("postTokenBalances") or []

        # Pre map: (mint, accountIndex) -> amount
        pre_map = {}
        for x in pre:
            if x.get("owner") != owner:
                continue
            mint = x.get("mint")
            ui = x.get("uiTokenAmount", {})
            amt = ui.get("uiAmount")
            if amt is None:
                s = ui.get("uiAmountString")
                amt = float(s) if s else 0.0
            pre_map[(mint, x.get("accountIndex"))] = amt or 0.0

        candidates = []
        for x in post:
            if x.get("owner") != owner:
                continue
            mint = x.get("mint")
            if mint in (USDC_MINT, WSOL_MINT):
                continue
            ui = x.get("uiTokenAmount", {})
            post_amt = ui.get("uiAmount")
            if post_amt is None:
                s = ui.get("uiAmountString")
                post_amt = float(s) if s else 0.0
            post_amt = post_amt or 0.0
            pre_amt = pre_map.get((mint, x.get("accountIndex")), 0.0) or 0.0
            delta = post_amt - pre_amt
            if delta > 0:
                candidates.append((mint, delta))

        if not candidates:
            return None

        # pick max delta
        return max(candidates, key=lambda t: t[1])
    except:
        return None

# =========================
# CORE LOGIC
# =========================
async def handle_signature(owner: str, sig: str):
    tx = await rpc.call(
        "getTransaction",
        [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    )
    if not tx:
        return

    ts = tx.get("blockTime")
    when = fmt_time_utc(ts)

    # 1) Find real token by owner's net increase
    token_info = find_real_token_by_balance_increase(tx, owner)
    if not token_info:
        return

    mint, token_delta = token_info
    meta = await token_meta_solscan(mint)

    # 2) Determine buy/sell using USDC delta first, else SOL delta
    usdc = owner_usdc_delta(tx, owner)
    if usdc is not None and abs(usdc) >= MIN_USD:
        # If spent USDC -> buy; received USDC -> sell
        if usdc < 0:
            action = "🟢 ALIM"
            flow = f"USDC → {meta['symbol']}"
            usd_val = abs(usdc)
            detail = f"Tutar: ${usd_val:,.0f} (USDC)"
        else:
            action = "🔴 SATIŞ"
            flow = f"{meta['symbol']} → USDC"
            usd_val = abs(usdc)
            detail = f"Tutar: ${usd_val:,.0f} (USDC)"

        await send(
            f"{action} (≥${MIN_USD:.0f})\n"
            f"Cüzdan: {short(owner)}\n"
            f"Token: {meta['symbol']} ({meta['name']})\n"
            f"Mint: {mint}\n"
            f"Aksiyon: {flow}\n"
            f"{detail}\n"
            f"Zaman: {when}"
        )
        return

    # fallback to SOL delta estimate
    sol = owner_sol_delta(tx, owner)
    if sol is None:
        return

    price = await sol_price_usd()
    usd_est = abs(sol) * price
    if usd_est < MIN_USD:
        return

    if sol < 0:
        action = "🟢 ALIM"
        flow = f"SOL → {meta['symbol']}"
        detail = f"Harcama: {abs(sol):.2f} SOL (~${usd_est:,.0f})"
    else:
        action = "🔴 SATIŞ"
        flow = f"{meta['symbol']} → SOL"
        detail = f"Alınan: {sol:.2f} SOL (~${usd_est:,.0f})"

    await send(
        f"{action} (≥${MIN_USD:.0f})\n"
        f"Cüzdan: {short(owner)}\n"
        f"Token: {meta['symbol']} ({meta['name']})\n"
        f"Mint: {mint}\n"
        f"Aksiyon: {flow}\n"
        f"{detail}\n"
        f"Zaman: {when}"
    )

async def watch_wallet(owner: str):
    seen = set()
    while True:
        try:
            res = await rpc.call("getSignaturesForAddress", [owner, {"limit": 12}])
            for item in reversed(res):
                sig = item["signature"]
                if sig in seen:
                    continue
                seen.add(sig)
                await handle_signature(owner, sig)

            if len(seen) > 3000:
                seen = set(list(seen)[-1500:])

        except Exception as e:
            # Telegram spam yok, sadece log
            print(f"[WARN] {short(owner)}: {e}")

        await asyncio.sleep(POLL_SEC)

async def main():
    await send(
        "✅ Top5 Cüzdan Alarm Sistemi AKTİF (Token isimli + Mint)\n"
        f"MIN_USD=${MIN_USD:.0f} | POLL_SEC={POLL_SEC}\n"
        f"RPC1: {RPC_URLS[0]}"
    )
    await asyncio.gather(*(watch_wallet(w) for w in TOP5))

# =========================
# START
# =========================
if __name__ == "__main__":
    threading.Thread(target=start_web, daemon=True).start()
    asyncio.run(main())
