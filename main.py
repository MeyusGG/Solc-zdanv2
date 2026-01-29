import os
import asyncio
import time
import threading
from typing import Optional, List

import httpx
from telegram import Bot
from fastapi import FastAPI
import uvicorn

# =========================
# ENV
# =========================
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])

RPC_URLS = [
    "https://rpc.solana.com",
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet-beta.solana.com",
]

MIN_USD = int(os.getenv("MIN_USD", "100"))
POLL_SEC = int(os.getenv("POLL_SEC", "30"))

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

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
_sol_cache = {"ts": 0, "price": 0}

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
# TX ANALYSIS
# =========================
def parse_usdc(tx, owner) -> Optional[float]:
    try:
        pre = tx["meta"]["preTokenBalances"]
        post = tx["meta"]["postTokenBalances"]

        def get(arr):
            out = {}
            for x in arr:
                if x["mint"] == USDC_MINT and x["owner"] == owner:
                    amt = x["uiTokenAmount"]["uiAmount"]
                    out[x["accountIndex"]] = amt or 0
            return out

        p0 = get(pre)
        p1 = get(post)

        delta = sum(p1.get(i, 0) - p0.get(i, 0) for i in p1)
        return delta
    except:
        return None

def parse_sol(tx, owner) -> Optional[float]:
    try:
        keys = tx["transaction"]["message"]["accountKeys"]
        idx = next(i for i,k in enumerate(keys) if (k["pubkey"] if isinstance(k,dict) else k)==owner)
        pre = tx["meta"]["preBalances"][idx]
        post = tx["meta"]["postBalances"][idx]
        return (post - pre) / 1e9
    except:
        return None

# =========================
# CORE LOGIC
# =========================
async def handle_tx(owner, sig):
    tx = await rpc.call(
        "getTransaction",
        [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    )
    if not tx:
        return

    ts = tx.get("blockTime")
    t = time.strftime("%d %b %H:%M", time.gmtime(ts)) if ts else "?"

    # USDC varsa direkt $
    usdc = parse_usdc(tx, owner)
    if usdc is not None and abs(usdc) >= MIN_USD:
        if usdc < 0:
            await send(
                f"🟢 ALIM (≥${MIN_USD})\n"
                f"Cüzdan: {short(owner)}\n"
                f"Aksiyon: USDC → TOKEN\n"
                f"Tutar: ${abs(usdc):,.0f}\n"
                f"Zaman: {t} UTC"
            )
        else:
            await send(
                f"🔴 SATIŞ (≥${MIN_USD})\n"
                f"Cüzdan: {short(owner)}\n"
                f"Aksiyon: TOKEN → USDC\n"
                f"Tutar: ${usdc:,.0f}\n"
                f"Zaman: {t} UTC"
            )
        return

    # SOL ile $
    sol = parse_sol(tx, owner)
    if sol:
        price = await sol_price()
        usd = abs(sol) * price
        if usd >= MIN_USD:
            if sol < 0:
                await send(
                    f"🟢 ALIM (≥${MIN_USD})\n"
                    f"Cüzdan: {short(owner)}\n"
                    f"Aksiyon: SOL → TOKEN\n"
                    f"Harcama: {abs(sol):.2f} SOL (~${usd:,.0f})\n"
                    f"Zaman: {t} UTC"
                )
            else:
                await send(
                    f"🔴 SATIŞ (≥${MIN_USD})\n"
                    f"Cüzdan: {short(owner)}\n"
                    f"Aksiyon: TOKEN → SOL\n"
                    f"Alınan: {sol:.2f} SOL (~${usd:,.0f})\n"
                    f"Zaman: {t} UTC"
                )

async def watch_wallet(owner):
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
    await send("✅ Top5 Cüzdan Alarm Sistemi AKTİF")
    await asyncio.gather(*(watch_wallet(w) for w in TOP5))

# =========================
# START
# =========================
if __name__ == "__main__":
    threading.Thread(target=start_web, daemon=True).start()
    asyncio.run(main())
