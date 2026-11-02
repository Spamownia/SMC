import json
from datetime import datetime, timezone
from collections import deque
from typing import List, Dict

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from loguru import logger
from pydantic import BaseModel, Field, field_validator
from config import get_settings

settings = get_settings()

log_buffer = deque(maxlen=100)
paper_positions: List[Dict] = []          # symulowane pozycje
paper_balance = 10000.0                   # startowe wirtualne saldo

class InterceptHandler:
    def write(self, message):
        msg = message.strip()
        if msg:
            log_buffer.append(msg)

logger.remove()
logger.add(InterceptHandler(), format="{time:HH:mm:ss} | {level} | {message}", level="INFO")
logger.add(lambda msg: print(msg, end=""), format="{time:HH:mm:ss} | {level} | {message}", level="INFO")

app = FastAPI(title="SMC Bot – Paper Mode", version="2.3.0-paper")
templates = Jinja2Templates(directory="templates")

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

def normalize_symbol(tv_symbol: str) -> str:
    s = tv_symbol.upper().replace(".P", "").replace("PERP", "")
    if s.endswith("USDT") and "/" not in s:
        return f"{s[:-4]}/USDT"
    return s

async def execute_paper_trade(signal: Signal) -> dict:
    global paper_balance

    symbol = normalize_symbol(signal.symbol)

    # Sprawdź czy już jest pozycja na tym symbolu
    for pos in paper_positions:
        if pos["symbol"] == symbol:
            msg = f"⛔ Pominięto {signal.side} {symbol} – pozycja już otwarta (paper)"
            logger.warning(msg)
            return {"status": "skipped", "reason": "already_open"}

    # Prosty sizing (ryzykujemy RISK_PERCENT kapitału)
    risk_amount = paper_balance * (settings.RISK_PERCENT / 100)
    risk_per_unit = abs(signal.entry - signal.sl)
    if risk_per_unit == 0:
        raise ValueError("Risk per unit = 0")

    amount = round(risk_amount / risk_per_unit, 4)

    position = {
        "id": len(paper_positions) + 1,
        "symbol": symbol,
        "side": signal.side.lower(),
        "amount": amount,
        "entry": signal.entry,
        "sl": signal.sl,
        "tp": signal.tp,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "status": "open"
    }

    paper_positions.append(position)

    logger.info(f"[PAPER] Otwarto {signal.side} | {symbol} | Size: {amount} | Entry: {signal.entry}")
    logger.info(f"[PAPER] SL: {signal.sl} | TP: {signal.tp}")

    return {
        "status": "opened_paper",
        "side": signal.side,
        "symbol": symbol,
        "amount": amount,
        "entry": signal.entry,
        "sl": signal.sl,
        "tp": signal.tp
    }

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    open_positions = [p for p in paper_positions if p["status"] == "open"]

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "testnet": True,
        "balance": round(paper_balance, 2),
        "positions": open_positions,
        "risk_percent": settings.RISK_PERCENT,
        "logs": list(log_buffer)[-50:]
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

    logger.info(f"Sygnał: {signal.side} {signal.symbol} | Entry: {signal.entry} | SL: {signal.sl} | TP: {signal.tp}")

    try:
        result = await execute_paper_trade(signal)
        return {"ok": True, "mode": "paper", "result": result}
    except Exception as e:
        logger.exception(f"Paper trade error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/close/{symbol:path}")
async def close_position(symbol: str):
    for pos in paper_positions:
        if pos["symbol"] == symbol and pos["status"] == "open":
            pos["status"] = "closed"
            logger.info(f"[PAPER] Zamknięto pozycję {symbol}")
            break
    return RedirectResponse(url="/", status_code=303)

@app.get("/health")
async def health():
    return {
        "status": "running",
        "mode": "paper",
        "open_positions": len([p for p in paper_positions if p["status"] == "open"]),
        "time": datetime.now(timezone.utc).isoformat()
    }

@app.get("/reset")
async def reset_paper():
    """Szybkie czyszczenie wszystkich paper pozycji"""
    global paper_positions, paper_balance
    paper_positions = []
    paper_balance = 10000.0
    logger.info("[PAPER] Reset wykonany")
    return {"status": "reset_done"}
