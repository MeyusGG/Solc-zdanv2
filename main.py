import os
import asyncio
import time
import threading
import httpx
from telegram import Bot
from fastapi import FastAPI
import uvicorn

# =========================
# ENV
# =========================
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
SOL_RPC_HTTP = os.getenv("SOL_RPC_HTTP", "https://api.mainnet-beta.solana.com")

# =========================
# CONFIG
# =========================
TOP5 = [
    "GYQwwvyL4UTrjoNChC1KNWpkYWwB1uuc53nUvpQH5gN2",
    "DP7G43VPwR5Ab5rcjrCnvJ8UgvRXRHTWscMjRD1eSdGC",
    "As1W8fHawur7zrBmNkYEA4ANRy9ERzKUs71X7N59qAmn",
    "JAmx4Wsh7cWXRzQuVt3TCKAyDfRm9HA7ztJa4f7RM8h9",
    "21kMe9Ztcj3qLSN4Re2v9XQfXBrvJnJPHkw1CbaoPDnT",
]

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MIN_USD = 100.0
POLL_SEC = 7

bot = Bot(BOT_TOKEN)

# =========================
# FASTAPI (Render için)
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
def short(addr):
    return addr[:4] + "…" + addr[-4:]

async def send(msg):
    await bot.send_message(
        chat_id=CHAT_ID,
        text=msg,
        disable_web_page_preview=True
    )

async def rpc_call(method, params):
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.post(
            SOL_RPC_HTTP,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(data["error"])
        return data["result"]

# =========================
# SOL PRICE (cache)
# =========================
_sol_price = {"ts": 0, "price": None}

async def sol_price_usd():
    now = time.time()
    if _sol_price["price"] and now - _sol_price["ts"] < 30:
        return _sol_price["price"]

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "solana", "vs_currencies": "usd"},
        )
        price = float(r.json()["solana"]["usd"])
        _sol_price.update({"price": price, "ts": now})
        return price

# =========================
# TX PARSERS
# =========================
def parse_usdc_delta(tx, owner):
    try:
        meta = tx["meta"]
        pre = meta.get("preTokenBalances", [])
        post = meta.get("postTokenBalances", [])

        def collect(arr):
            out = {}
            for x in arr:
                if x.get("mint") == USDC_MINT and x.get("owner") == owner:
                    idx = x["accountIndex"]
                    ui = x["uiTokenAmount"]
                    amt = ui.get("uiAmount")
                    if amt is None:
                        amt = float(ui.get("uiAmountString", 0))
                    out[idx] = amt
            return out

        pre_m = collect(pre)
        post_m = collect(post)

        delta = 0
        found = False
        for idx, post_amt in post_m.items():
            pre_amt = pre_m.get(idx, 0)
            delta += post_amt - pre_amt
            found = True

        return delta if found else None
    except:
        return None

def parse_sol_delta(tx, owner):
    try:
        meta = tx["meta"]
        pre = meta["preBalances"]
        post = meta["postBalances"]
        keys = tx["transaction"]["message"]["accountKeys"]

        for i, k in enumerate(keys):
            pub = k["pubkey"] if isinstance(k, dict) else k
            if pub == owner:
                return (post[i] - pre[i]) / 1e9
        return None
    except:
        return None

# =========================
# CORE LOGIC
# =========================
async def handle_tx(owner, sig):
    tx = await rpc_call(
        "getTransaction",
        [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    )
    if not tx:
        return

    ts = tx.get("blockTime")
    tstr = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)) if ts else "?"

    usdc = parse_usdc_delta(tx, owner)
    if usdc and abs(usdc) >= MIN_USD:
        action = "ALIM" if usdc < 0 else "SATIŞ"
        emoji = "🟢" if action == "ALIM" else "🧨"
        await send(
            f"{emoji} {action} (≥$100)\n"
            f"Cüzdan: {short(owner)}\n"
            f"Tutar: ${abs(usdc):,.2f} USDC\n"
            f"TX: {sig}\n"
            f"Zaman: {tstr} UTC"
        )
        return

    sol = parse_sol_delta(tx, owner)
    if sol:
        price = await sol_price_usd()
        usd = abs(sol) * price
        if usd >= MIN_USD:
            action = "ALIM" if sol < 0 else "SATIŞ"
            emoji = "🟢" if action == "ALIM" else "🧨"
            await send(
                f"{emoji} {action} (≥$100)\n"
                f"Cüzdan: {short(owner)}\n"
                f"Tutar: ~${usd:,.2f} (SOL)\n"
                f"SOL Δ: {sol:+.4f}\n"
                f"TX: {sig}\n"
                f"Zaman: {tstr} UTC"
            )

async def watch_wallet(owner):
    seen = set()
    while True:
        try:
            sigs = await rpc_call("getSignaturesForAddress", [owner, {"limit": 20}])
            for s in reversed(sigs):
                sig = s["signature"]
                if sig not in seen:
                    seen.add(sig)
                    await handle_tx(owner, sig)
            if len(seen) > 3000:
                seen = set(list(seen)[-1500:])
        except Exception as e:
            await send(f"⚠️ RPC hata ({short(owner)}): {e}")
            await asyncio.sleep(10)

        await asyncio.sleep(POLL_SEC)

async def bot_main():
    await send("✅ Top5 DM Alarm bot AKTİF (Render)")
    await asyncio.gather(*(watch_wallet(w) for w in TOP5))

# =========================
# START
# =========================
if __name__ == "__main__":
    threading.Thread(target=start_web).start()
    asyncio.run(bot_main())
