import json
from datetime import datetime, timezone
from pathlib import Path
from collections import deque

import ccxt
import httpx
from fastapi import FastAPI, Request, HTTPException, Header, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from loguru import logger
from pydantic import BaseModel, Field, field_validator
from config import get_settings

settings = get_settings()

# Logi do dashboardu (ostatnie 100 linii)
log_buffer = deque(maxlen=100)

class InterceptHandler:
    def write(self, message):
        log_buffer.append(message.strip())

logger.add(InterceptHandler(), format="{time:HH:mm:ss} | {level} | {message}", level="INFO")
logger.add("bot_{time:YYYY-MM-DD}.log", rotation="10 MB", retention="14 days", level="INFO")

app = FastAPI(title="SMC Bot Console", version="2.1.0")
templates = Jinja2Templates(directory="templates")

# ─────────────────────────────────────────────────────────────
class Signal(BaseModel):
    side: str = Field(..., pattern="^(LONG|SHORT)$")
    symbol: str
    entry: float
    sl: float
    tp: float
    rr: float
    bias: str

    @field_validator("sl", "tp")
    @classmethod
    def positive(cls, v):
        if v <= 0:
            raise ValueError("Cena musi być > 0")
        return v

# ─────────────────────────────────────────────────────────────
def create_exchange():
    exchange = ccxt.bybit({
        "apiKey": settings.API_KEY,
        "secret": settings.API_SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": "linear", "recvWindow": 10000}
    })
    if settings.TESTNET:
        exchange.set_sandbox_mode(True)
        logger.warning("=== TRYB TESTNET ===")
    else:
        logger.warning("=== TRYB LIVE ===")
    return exchange

exchange = create_exchange()

# ─────────────────────────────────────────────────────────────
async def send_telegram(text: str):
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json={
                "chat_id": settings.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML"
            })
    except Exception as e:
        logger.error(f"Telegram error: {e}")

def normalize_symbol(tv_symbol: str) -> str:
    s = tv_symbol.upper().replace(".P", "").replace("PERP", "")
    if s.endswith("USDT") and "/" not in s:
        return f"{s[:-4]}/USDT:USDT"
    return s

def validate_sl_tp(side: str, entry: float, sl: float, tp: float):
    if side == "LONG":
        if sl >= entry or tp <= entry:
            raise ValueError("Nieprawidłowy SL/TP dla LONG")
    else:
        if sl <= entry or tp >= entry:
            raise ValueError("Nieprawidłowy SL/TP dla SHORT")

def get_position_size(symbol: str, entry: float, sl: float) -> float:
    balance = exchange.fetch_balance()
    usdt = float(balance["USDT"]["free"])
    risk_amount = usdt * (settings.RISK_PERCENT / 100)
    risk_per_unit = abs(entry - sl)
    if risk_per_unit == 0:
        raise ValueError("Risk per unit = 0")
    size = risk_amount / risk_per_unit
    size = float(exchange.amount_to_precision(symbol, size))
    market = exchange.market(symbol)
    min_cost = market.get("limits", {}).get("cost", {}).get("min") or 5
    if size * entry < min_cost:
        size = float(exchange.amount_to_precision(symbol, (min_cost * 1.1) / entry))
    return size

def has_open_position(symbol: str) -> bool:
    positions = exchange.fetch_positions([symbol])
    return any(float(p["contracts"]) > 0 for p in positions)

def get_open_positions():
    positions = exchange.fetch_positions()
    result = []
    for p in positions:
        contracts = float(p.get("contracts") or 0)
        if contracts > 0:
            result.append({
                "symbol": p["symbol"],
                "side": p["side"],
                "contracts": contracts,
                "entryPrice": p.get("entryPrice"),
                "unrealizedPnl": round(float(p.get("unrealizedPnl") or 0), 2)
            })
    return result

# ─────────────────────────────────────────────────────────────
async def execute_trade(signal: Signal) -> dict:
    symbol = normalize_symbol(signal.symbol)
    side = "buy" if signal.side == "LONG" else "sell"

    validate_sl_tp(signal.side, signal.entry, signal.sl, signal.tp)

    if has_open_position(symbol):
        msg = f"⛔ Pominięto {signal.side} {symbol} – pozycja już otwarta"
        logger.warning(msg)
        await send_telegram(msg)
        return {"status": "skipped", "reason": "already_open"}

    try:
        exchange.set_leverage(settings.DEFAULT_LEVERAGE, symbol)
    except Exception as e:
        logger.warning(f"Leverage: {e}")

    amount = get_position_size(symbol, signal.entry, signal.sl)
    if amount <= 0:
        raise ValueError("Size <= 0")

    logger.info(f"Otwieram {signal.side} | {symbol} | Size: {amount}")

    order = exchange.create_order(
        symbol=symbol,
        type="market",
        side=side,
        amount=amount
    )
    logger.success(f"Order: {order['id']}")

    # Trading Stop (Bybit)
    try:
        clean_symbol = symbol.replace("/", "").replace(":USDT", "")
        exchange.private_post_v5_position_trading_stop({
            "category": "linear",
            "symbol": clean_symbol,
            "takeProfit": str(signal.tp),
            "stopLoss": str(signal.sl),
            "tpTriggerBy": "LastPrice",
            "slTriggerBy": "LastPrice",
            "tpslMode": "Full",
            "positionIdx": 0
        })
        logger.success(f"SL/TP ustawione | SL: {signal.sl} | TP: {signal.tp}")
    except Exception as e:
        logger.error(f"Trading Stop error: {e}")
        await send_telegram(f"⚠️ Order otwarty, ale SL/TP nie ustawiły się\n{symbol}\n{e}")

    await send_telegram(
        f"<b>{'🟢 LONG' if signal.side == 'LONG' else '🔴 SHORT'}</b>\n"
        f"<code>{symbol}</code>\n"
        f"Size: {amount}\nEntry: {signal.entry}\nSL: {signal.sl}\nTP: {signal.tp}"
    )

    return {
        "status": "opened",
        "side": signal.side,
        "symbol": symbol,
        "amount": amount,
        "order_id": order["id"]
    }

# ─────────────────────────────────────────────────────────────
# ENDPOINTY
# ─────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    try:
        balance = exchange.fetch_balance()
        usdt_free = round(float(balance["USDT"]["free"]), 2)
    except Exception:
        usdt_free = "—"

    positions = get_open_positions()
    logs = list(log_buffer)[-40:]  # ostatnie 40 linii

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "testnet": settings.TESTNET,
        "balance": usdt_free,
        "positions": positions,
        "risk_percent": settings.RISK_PERCENT,
        "logs": logs
    })

@app.post("/webhook")
async def webhook(
    request: Request,
    x_webhook_secret: str = Header(None, alias="X-Webhook-Secret")
):
    if x_webhook_secret != settings.WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        data = json.loads(await request.body())
        signal = Signal(**data)
    except Exception as e:
        logger.error(f"Parse error: {e}")
        raise HTTPException(status_code=400, detail="Invalid payload")

    logger.info(f"Sygnał: {signal.side} {signal.symbol} | {signal.entry} → SL {signal.sl} TP {signal.tp}")

    try:
        result = await execute_trade(signal)
        return {"ok": True, "result": result}
    except Exception as e:
        logger.exception(f"Trade error: {e}")
        await send_telegram(f"❌ Błąd\n{signal.side} {signal.symbol}\n{e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/close/{symbol:path}")
async def close_position(symbol: str):
    try:
        # symbol przychodzi jako BTC/USDT:USDT
        positions = exchange.fetch_positions([symbol])
        for p in positions:
            if float(p["contracts"]) > 0:
                side = "sell" if p["side"] == "long" else "buy"
                exchange.create_order(
                    symbol=symbol,
                    type="market",
                    side=side,
                    amount=float(p["contracts"]),
                    params={"reduceOnly": True}
                )
                logger.info(f"Zamknięto pozycję {symbol}")
                await send_telegram(f"🗑 Zamknięto ręcznie: {symbol}")
                break
    except Exception as e:
        logger.error(f"Close error: {e}")
    return RedirectResponse(url="/", status_code=303)

@app.get("/health")
async def health():
    return {
        "status": "running",
        "testnet": settings.TESTNET,
        "time": datetime.now(timezone.utc).isoformat()
    }

# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    logger.info("Uruchamiam SMC Bot Console v2.1...")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
