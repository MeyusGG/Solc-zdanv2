import os
import asyncio
import time
import threading
from typing import Optional, List, Dict

import httpx
from telegram import Bot
from fastapi import FastAPI
import uvicorn

# =========================
# ENV
# =========================
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])

# Tek URL yerine çoklu RPC (failover)
# İstersen Render Env’e RPC_URLS ekleyebilirsin (virgülle ayrılmış).
# Yoksa default listeyi kullanır.
RPC_URLS_ENV = os.getenv("RPC_URLS", "").strip()
DEFAULT_RPC_URLS = [
    "https://solana-rpc.publicnode.com",
    "https://rpc.solana.com",
    "https://api.mainnet-beta.solana.com",
]
RPC_URLS = [u.strip() for u in RPC_URLS_ENV.split(",") if u.strip()] or DEFAULT_RPC_URLS

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

MIN_USD = float(os.getenv("MIN_USD", "100"))
POLL_SEC = int(os.getenv("POLL_SEC", "20"))

# Global rate-limit: 1 RPC çağrısı arası minimum bekleme (sn)
# 0.35sn -> ~3 req/s. 5 cüzdan polling + tx fetch ile genelde yeter.
MIN_RPC_GAP = float(os.getenv("MIN_RPC_GAP", "0.35"))

bot = Bot(BOT_TOKEN)

# =========================
# FASTAPI (Render için ping)
# =========================
app = FastAPI()

@app.get("/")
def home():
    return {"status": "alive", "rpc_urls": RPC_URLS, "min_usd": MIN_USD, "poll_sec": POLL_SEC}

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

# =========================
# RPC CLIENT (failover + backoff + global pacing)
# =========================
class RpcClient:
    def __init__(self, urls: List[str]):
        self.urls = urls
        self.idx = 0
        self.client = httpx.AsyncClient(timeout=25)
        self._last_call_ts = 0.0
        self._lock = asyncio.Lock()

    def current_url(self) -> str:
        return self.urls[self.idx % len(self.urls)]

    def rotate(self):
        self.idx = (self.idx + 1) % len(self.urls)

    async def close(self):
        await self.client.aclose()

    async def call(self, method: str, params):
        # Global pacing (her çağrı arasında min gap)
        async with self._lock:
            now = time.time()
            wait = (self._last_call_ts + MIN_RPC_GAP) - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call_ts = time.time()

            # Failover denemeleri
            last_err = None
            for attempt in range(len(self.urls) * 2):
                url = self.current_url()
                try:
                    r = await self.client.post(
                        url,
                        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                        headers={"Content-Type": "application/json"},
                    )

                    # 403/429/5xx gibi durumlarda RPC değiştir
                    if r.status_code in (403, 429) or r.status_code >= 500:
                        last_err = f"HTTP {r.status_code} @ {url}"
                        self.rotate()
                        await asyncio.sleep(1.2)  # küçük backoff
                        continue

                    r.raise_for_status()
                    data = r.json()
                    if "error" in data:
                        # Bazı RPC'ler rate-limit error'u json ile döner
                        last_err = f"RPC error {data['error']} @ {url}"
                        # rate limit benzeri ise rotate
                        self.rotate()
                        await asyncio.sleep(1.2)
                        continue

                    return data["result"]

                except Exception as e:
                    last_err = f"{type(e).__name__}: {e} @ {url}"
                    self.rotate()
                    await asyncio.sleep(1.2)

            raise RuntimeError(last_err or "RPC call failed")

rpc = RpcClient(RPC_URLS)

# =========================
# SOL PRICE (CoinGecko cache)
# =========================
_sol_price_cache: Dict[str, float] = {"ts": 0.0, "price": 0.0}

async def sol_price_usd() -> float:
    now = time.time()
    if _sol_price_cache["price"] and (now - _sol_price_cache["ts"]) < 45:
        return _sol_price_cache["price"]

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "solana", "vs_currencies": "usd"},
        )
        r.raise_for_status()
        price = float(r.json()["solana"]["usd"])
        _sol_price_cache["price"] = price
        _sol_price_cache["ts"] = now
        return price

# =========================
# TX PARSERS
# =========================
def parse_usdc_delta(tx: dict, owner: str) -> Optional[float]:
    """
    Owner USDC token account değişimini bulur.
    Negatif = USDC azaldı => genelde token ALIM
    Pozitif = USDC arttı => genelde token SATIŞ
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

def parse_sol_delta(tx: dict, owner: str) -> Optional[float]:
    """
    Owner SOL (lamports) değişimi.
    Negatif = SOL azaldı (harcadı) => çoğu swapta ALIM
    Pozitif = SOL arttı => SATIŞ / transfer
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

        lamports_delta = post[idx] - pre[idx]
        return lamports_delta / 1e9
    except:
        return None

def action_from_delta(delta: float) -> str:
    if delta < 0:
        return "ALIM"
    if delta > 0:
        return "SATIŞ"
    return "HAREKET"

# =========================
# CORE
# =========================
async def handle_signature(owner: str, sig: str):
    tx = await rpc.call(
        "getTransaction",
        [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    )
    if not tx:
        return

    ts = tx.get("blockTime")
    tstr = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)) if ts else "?"

    # 1) USDC varsa $ kesin
    usdc_delta = parse_usdc_delta(tx, owner)
    if usdc_delta is not None:
        usd = abs(usdc_delta)
        if usd >= MIN_USD:
            action = action_from_delta(usdc_delta)
            emoji = "🟢" if action == "ALIM" else "🧨"
            await send(
                f"{emoji} {action} (≥${MIN_USD:.0f})\n"
                f"Cüzdan: {short(owner)}\n"
                f"Tutar: ${usd:,.2f} (USDC)\n"
                f"TX: {sig}\n"
                f"Zaman: {tstr} UTC"
            )
        return

    # 2) USDC yoksa SOL ile $ tahmini
    sol_delta = parse_sol_delta(tx, owner)
    if sol_delta is not None:
        price = await sol_price_usd()
        usd_est = abs(sol_delta) * price
        if usd_est >= MIN_USD:
            action = action_from_delta(sol_delta)
            emoji = "🟢" if action == "ALIM" else "🧨"
            await send(
                f"{emoji} {action} (≥${MIN_USD:.0f})\n"
                f"Cüzdan: {short(owner)}\n"
                f"Tutar: ~${usd_est:,.2f} (SOL≈${price:,.2f})\n"
                f"SOL Δ: {sol_delta:+.4f}\n"
                f"TX: {sig}\n"
                f"Zaman: {tstr} UTC"
            )

async def watch_wallet(owner: str):
    seen = set()
    while True:
        try:
            res = await rpc.call("getSignaturesForAddress", [owner, {"limit": 15}])
            # eskiden yeniye
            for item in reversed(res):
                sig = item["signature"]
                if sig in seen:
                    continue
                seen.add(sig)
                await handle_signature(owner, sig)

            # set büyümesin
            if len(seen) > 2500:
                seen = set(list(seen)[-1200:])

        except Exception as e:
            # Telegram'a spam yok -> sadece log
            print(f"[WARN] {short(owner)} RPC: {e}")

        await asyncio.sleep(POLL_SEC)

async def main():
    await send(
        "✅ Top5 DM Alarm bot AKTİF (Render)\n"
        f"MIN_USD=${MIN_USD:.0f}, POLL_SEC={POLL_SEC}\n"
        f"RPC: {rpc.current_url()}"
    )
    await asyncio.gather(*(watch_wallet(w) for w in TOP5))

if __name__ == "__main__":
    threading.Thread(target=start_web, daemon=True).start()
    asyncio.run(main())
