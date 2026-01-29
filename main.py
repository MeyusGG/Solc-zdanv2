import os
import asyncio
import time
import threading
from typing import List, Optional, Dict

import httpx
from telegram import Bot
from fastapi import FastAPI
import uvicorn

# =========================
# ENV
# =========================
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])

MIN_USD = int(os.getenv("MIN_USD", "100"))
POLL_SEC = int(os.getenv("POLL_SEC", "30"))

RPC_URLS = [
    "https://rpc.solana.com",
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet-beta.solana.com",
]

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
    return {"status": "alive"}

def start_web():
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

# =========================
# HELPERS
# =========================
def short(addr: str):
    return addr[:4] + "…" + addr[-4:]

async def send(msg: str):
    await bot.send_message(chat_id=CHAT_ID, text=msg)

# =========================
# RPC CLIENT (failover)
# =========================
class Rpc:
    def __init__(self, urls: List[str]):
        self.urls = urls
        self.i = 0
        self.client = httpx.AsyncClient(timeout=20)

    async def call(self, method, params):
        for _ in range(len(self.urls)):
            url = self.urls[self.i]
            try:
                r = await self.client.post(
                    url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                )
                if r.status_code in (403, 429) or r.status_code >= 500:
                    self.i = (self.i + 1) % len(self.urls)
                    await asyncio.sleep(1)
                    continue
                r.raise_for_status()
                data = r.json()
                if "error" in data:
                    self.i = (self.i + 1) % len(self.urls)
                    continue
                return data["result"]
            except:
                self.i = (self.i + 1) % len(self.urls)
        raise RuntimeError("All RPC failed")

rpc = Rpc(RPC_URLS)

# =========================
# SOL PRICE
# =========================
_sol_cache = {"ts": 0, "price": 0.0}

async def sol_price():
    if time.time() - _sol_cache["ts"] < 60:
        return _sol_cache["price"]
    async with httpx.AsyncClient() as c:
        r = await c.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "solana", "vs_currencies": "usd"},
        )
        price = float(r.json()["solana"]["usd"])
        _sol_cache.update({"price": price, "ts": time.time()})
        return price

# =========================
# TOKEN METADATA
# =========================
_token_cache: Dict[str, Dict] = {}

async def token_meta(mint: str) -> Dict[str, str]:
    if mint in _token_cache:
        return _token_cache[mint]

    # Solana token list (public, hızlı)
    url = "https://raw.githubusercontent.com/solana-labs/token-list/main/src/tokens/solana.tokenlist.json"
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(url)
        tokens = r.json().get("tokens", [])
        for t in tokens:
            if t.get("address") == mint:
                meta = {"symbol": t.get("symbol"), "name": t.get("name")}
                _token_cache[mint] = meta
                return meta

    meta = {"symbol": mint[:4], "name": "Unknown"}
    _token_cache[mint] = meta
    return meta

# =========================
# TX ANALYSIS – REAL TOKEN
# =========================
def find_real_token(tx: dict, owner: str) -> Optional[Dict]:
    """
    Owner'ın NET token artışını bulur.
    USDC / WSOL hariç tutulur.
    """
    try:
        pre = tx["meta"]["preTokenBalances"]
        post = tx["meta"]["postTokenBalances"]

        pre_map = {}
        for x in pre:
            if x["owner"] == owner:
                pre_map[(x["mint"], x["accountIndex"])] = x["uiTokenAmount"]["uiAmount"] or 0

        deltas = []
        for x in post:
            if x["owner"] != owner:
                continue
            mint = x["mint"]
            if mint in (USDC_MINT, WSOL_MINT):
                continue
            post_amt = x["uiTokenAmount"]["uiAmount"] or 0
            pre_amt = pre_map.get((mint, x["accountIndex"]), 0)
            delta = post_amt - pre_amt
            if delta > 0:
                deltas.append((mint, delta))

        if not deltas:
            return None

        # En çok artan token = gerçek alınan
        mint, amount = max(deltas, key=lambda x: x[1])
        return {"mint": mint, "amount": amount}

    except:
        return None

# =========================
# CORE
# =========================
async def handle_tx(owner: str, sig: str):
    tx = await rpc.call(
        "getTransaction",
        [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    )
    if not tx:
        return

    ts = tx.get("blockTime")
    t = time.strftime("%d %b %H:%M", time.gmtime(ts)) if ts else "?"

    token = find_real_token(tx, owner)
    if not token:
        return

    sol_delta = None
    try:
        keys = tx["transaction"]["message"]["accountKeys"]
        idx = next(i for i,k in enumerate(keys) if (k["pubkey"] if isinstance(k,dict) else k)==owner)
        pre = tx["meta"]["preBalances"][idx]
        post = tx["meta"]["postBalances"][idx]
        sol_delta = (post - pre) / 1e9
    except:
        pass

    price = await sol_price()
    usd = abs(sol_delta) * price if sol_delta else 0

    if usd < MIN_USD:
        return

    meta = await token_meta(token["mint"])

    if sol_delta < 0:
        await send(
            f"🟢 ALIM (≥${MIN_USD})\n"
            f"Cüzdan: {short(owner)}\n"
            f"Token: {meta['symbol']} ({meta['name']})\n"
            f"Aksiyon: SOL → {meta['symbol']}\n"
            f"Harcama: {abs(sol_delta):.2f} SOL (~${usd:,.0f})\n"
            f"Zaman: {t} UTC"
        )
    else:
        await send(
            f"🔴 SATIŞ (≥${MIN_USD})\n"
            f"Cüzdan: {short(owner)}\n"
            f"Token: {meta['symbol']} ({meta['name']})\n"
            f"Aksiyon: {meta['symbol']} → SOL\n"
            f"Alınan: {sol_delta:.2f} SOL (~${usd:,.0f})\n"
            f"Zaman: {t} UTC"
        )

async def watch_wallet(owner: str):
    seen = set()
    while True:
        try:
            sigs = await rpc.call("getSignaturesForAddress", [owner, {"limit": 10}])
            for s in reversed(sigs):
                sig = s["signature"]
                if sig not in seen:
                    seen.add(sig)
                    await handle_tx(owner, sig)
            if len(seen) > 2000:
                seen = set(list(seen)[-1000:])
        except:
            pass
        await asyncio.sleep(POLL_SEC)

async def main():
    await send("✅ Top5 Cüzdan Alarm Sistemi AKTİF (Token isimli)")
    await asyncio.gather(*(watch_wallet(w) for w in TOP5))

# =========================
# START
# =========================
if __name__ == "__main__":
    threading.Thread(target=start_web, daemon=True).start()
    asyncio.run(main())
