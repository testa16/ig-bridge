import os
import json
import requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify

app = Flask(__name__)

# ====================
# Render / Env
# ====================
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

IG_API_KEY     = os.environ.get("IG_API_KEY", "")
IG_USERNAME    = os.environ.get("IG_USERNAME", "")
IG_PASSWORD    = os.environ.get("IG_PASSWORD", "")
IG_ACCOUNT_ID  = os.environ.get("IG_ACCOUNT_ID", "")
IG_EPIC_GER40  = os.environ.get("IG_EPIC_GER40", "")

IG_BASE = os.environ.get("IG_BASE", "https://demo-api.ig.com/gateway/deal")

LOG_DIR = os.environ.get("LOG_DIR", "/var/data")
LOG_PATH = os.path.join(LOG_DIR, "trades.jsonl")


# ====================
# Helpers
# ====================
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_line(obj: dict):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        obj = {"ts": now_iso(), **obj}
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception as e:
        print("LOG_ERROR:", str(e), flush=True)


def safe_json(resp):
    try:
        return resp.json()
    except Exception:
        return resp.text


def resolve_epic(payload: dict) -> str:
    epic = (payload.get("epic") or "").strip()
    if epic:
        return epic

    sym = (payload.get("symbol") or "").strip().upper()

    if sym in [
        "GER40",
        "DE40",
        "DAX",
        "GERMANY40",
        "GERMANY 40",
        "DAX.EUR.1.IGN",
        "DE40EUR",
        "GER40EUR"
    ]:
        return IG_EPIC_GER40

    if sym.startswith("IX.") or sym.startswith("CS.") or sym.startswith("UA."):
        return sym

    return ""


# ====================
# IG login / session
# ====================
def ig_login() -> dict:
    url = f"{IG_BASE}/session"
    headers = {
        "X-IG-API-KEY": IG_API_KEY,
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json; charset=UTF-8",
        "VERSION": "2",
    }
    payload = {
        "identifier": IG_USERNAME,
        "password": IG_PASSWORD,
        "encryptedPassword": False
    }

    r = requests.post(url, headers=headers, json=payload, timeout=20)
    log_line({"kind": "ig_login", "status": r.status_code, "body": safe_json(r)})
    r.raise_for_status()

    cst = r.headers.get("CST")
    sec = r.headers.get("X-SECURITY-TOKEN")

    if not cst or not sec:
        raise RuntimeError("IG login ok but CST / X-SECURITY-TOKEN missing")

    return {
        "X-IG-API-KEY": IG_API_KEY,
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json; charset=UTF-8",
        "CST": cst,
        "X-SECURITY-TOKEN": sec,
        "VERSION": "2",
    }


def ig_set_account(h: dict):
    try:
        url = f"{IG_BASE}/session"
        payload = {
            "accountId": IG_ACCOUNT_ID,
            "defaultAccount": True
        }
        r = requests.put(url, headers={**h, "VERSION": "1"}, json=payload, timeout=20)
        log_line({"kind": "ig_set_account", "status": r.status_code, "payload": payload, "body": safe_json(r)})
    except Exception as e:
        log_line({"kind": "ig_set_account_error", "error": str(e)})


# ====================
# IG trading helpers
# ====================
def ig_get_positions(h: dict) -> list:
    url = f"{IG_BASE}/positions"
    r = requests.get(url, headers=h, timeout=20)
    log_line({"kind": "ig_positions", "status": r.status_code, "body": safe_json(r)})
    r.raise_for_status()
    data = r.json()
    return data.get("positions", [])


def ig_open_market(h: dict, epic: str, side: str, qty: float, currency: str = "EUR", expiry: str = "-") -> dict:
    url = f"{IG_BASE}/positions/otc"
    direction = "BUY" if side.lower() == "buy" else "SELL"

    payload = {
        "epic": epic,
        "expiry": expiry,
        "direction": direction,
        "size": float(qty),
        "orderType": "MARKET",
        "currencyCode": currency,
        "forceOpen": True,
        "guaranteedStop": False,
    }

    r = requests.post(url, headers=h, json=payload, timeout=20)
    log_line({"kind": "ig_entry", "status": r.status_code, "payload": payload, "body": safe_json(r)})
    r.raise_for_status()
    return r.json()


def ig_close_deal(h: dict, deal_id: str, direction: str, size: float, currency: str, expiry: str, epic: str) -> dict:
    url = f"{IG_BASE}/positions/otc"

    payload = {
        "dealId": deal_id,
        "epic": epic,
        "expiry": expiry,
        "direction": direction,
        "size": float(size),
        "orderType": "MARKET",
        "timeInForce": "FILL_OR_KILL",
        "currencyCode": currency,
        "forceOpen": False,
        "guaranteedStop": False,
    }

    headers = {**h, "VERSION": "1", "X-HTTP-Method-Override": "DELETE"}

    r = requests.post(url, headers=headers, json=payload, timeout=20)
    log_line({"kind": "ig_exit", "status": r.status_code, "payload": payload, "body": safe_json(r)})
    r.raise_for_status()
    return r.json()


def ig_close_positions_for_epic_and_side(h: dict, epic: str, side_to_close: str) -> dict:
    """
    side_to_close:
      'long'  -> close BUY positions only
      'short' -> close SELL positions only
    """
    positions = ig_get_positions(h)
    matches = []

    for p in positions:
        m = p.get("market", {})
        pos = p.get("position", {})

        if m.get("epic") != epic:
            continue

        pos_dir = pos.get("direction", "").upper()

        if side_to_close == "long" and pos_dir == "BUY":
            matches.append((m, pos))
        elif side_to_close == "short" and pos_dir == "SELL":
            matches.append((m, pos))

    if not matches:
        raise RuntimeError(f"no open {side_to_close} position found for epic")

    closed = []

    for m, pos in matches:
        deal_id = pos.get("dealId")
        open_dir = pos.get("direction", "").upper()
        size = pos.get("size", 0)
        currency = pos.get("currency", "EUR")
        expiry = m.get("expiry", "-")
        market_epic = m.get("epic", epic)

        if not deal_id or not size:
            continue

        close_dir = "SELL" if open_dir == "BUY" else "BUY"

        res = ig_close_deal(
            h=h,
            deal_id=deal_id,
            direction=close_dir,
            size=size,
            currency=currency,
            expiry=expiry,
            epic=market_epic
        )

        closed.append({
            "dealId": deal_id,
            "closed": res
        })

    return {
        "closedCount": len(closed),
        "closed": closed
    }


# ====================
# Routes
# ====================
@app.get("/")
def home():
    return "OK", 200


@app.get("/health")
def health():
    return jsonify({"ok": True, "ts": now_iso()}), 200


@app.post("/webhook")
def webhook():
    data = request.get_json(silent=True) or {}
    log_line({"kind": "webhook_in", "data": data})

    if not WEBHOOK_SECRET:
        log_line({"kind": "config_error", "error": "WEBHOOK_SECRET missing"})
        return jsonify({"ok": False, "error": "server not configured"}), 500

    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "bad secret"}), 401

    wtype = (data.get("type") or "").strip().lower()

    if wtype in ["test", "test_from_tv"]:
        out = {"ok": True, "ignored": True}
        log_line({"kind": "webhook_out", "result": out})
        return jsonify(out), 200

    epic = resolve_epic(data)
    if not epic:
        out = {"ok": False, "error": "epic not resolved (set IG_EPIC_GER40 or send epic)"}
        log_line({"kind": "webhook_error", "result": out, "data": data})
        return jsonify(out), 400

    missing = [k for k, v in {
        "IG_API_KEY": IG_API_KEY,
        "IG_USERNAME": IG_USERNAME,
        "IG_PASSWORD": IG_PASSWORD,
        "IG_ACCOUNT_ID": IG_ACCOUNT_ID,
    }.items() if not v]

    if missing:
        out = {"ok": False, "error": f"missing env: {', '.join(missing)}"}
        log_line({"kind": "webhook_error", "result": out})
        return jsonify(out), 500

    try:
        h = ig_login()
        ig_set_account(h)

        # -------- LONG ENTRY --------
        if wtype == "entry_long":
            qty = float(data.get("qty") or 1)
            res = ig_open_market(h, epic, "buy", qty)
            out = {"ok": True, "entry_long": res}
            log_line({"kind": "webhook_out", "result": out})
            return jsonify(out), 200

        # -------- SHORT ENTRY --------
        if wtype == "entry_short":
            qty = float(data.get("qty") or 1)
            res = ig_open_market(h, epic, "sell", qty)
            out = {"ok": True, "entry_short": res}
            log_line({"kind": "webhook_out", "result": out})
            return jsonify(out), 200

        # -------- LONG EXIT --------
        if wtype == "exit_long":
            res = ig_close_positions_for_epic_and_side(h, epic, "long")
            out = {"ok": True, "exit_long": res}
            log_line({"kind": "webhook_out", "result": out})
            return jsonify(out), 200

        # -------- SHORT EXIT --------
        if wtype == "exit_short":
            res = ig_close_positions_for_epic_and_side(h, epic, "short")
            out = {"ok": True, "exit_short": res}
            log_line({"kind": "webhook_out", "result": out})
            return jsonify(out), 200

        # Optional backwards compatibility
        if wtype == "entry":
            side = (data.get("side") or "buy").lower()
            qty = float(data.get("qty") or 1)
            res = ig_open_market(h, epic, side, qty)
            out = {"ok": True, "entry": res}
            log_line({"kind": "webhook_out", "result": out})
            return jsonify(out), 200

        if wtype == "exit":
            res = ig_close_positions_for_epic_and_side(h, epic, "long")
            out = {"ok": True, "exit": res}
            log_line({"kind": "webhook_out", "result": out})
            return jsonify(out), 200

        if wtype == "positions":
            res = ig_get_positions(h)
            out = {"ok": True, "positions": res}
            log_line({"kind": "webhook_out", "result": out})
            return jsonify(out), 200

        out = {"ok": True, "ignored": True, "reason": "unknown type"}
        log_line({"kind": "webhook_out", "result": out})
        return jsonify(out), 200

    except Exception as e:
        out = {"ok": False, "error": str(e)}
        log_line({"kind": "webhook_error", "result": out, "data": data})
        return jsonify(out), 500
