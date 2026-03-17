import os
import json
import requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify

app = Flask(__name__)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
IG_API_KEY     = os.environ.get("IG_API_KEY", "")
IG_USERNAME    = os.environ.get("IG_USERNAME", "")
IG_PASSWORD    = os.environ.get("IG_PASSWORD", "")
IG_ACCOUNT_ID  = os.environ.get("IG_ACCOUNT_ID", "")
IG_EPIC        = os.environ.get("IG_EPIC_GER40", "IX.D.DAX.DAILY.IP")
IG_BASE        = os.environ.get("IG_BASE", "https://demo-api.ig.com/gateway/deal")

def log(obj):
    obj["ts"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(obj, ensure_ascii=False), flush=True)

def safe_json(resp):
    try:
        return resp.json()
    except Exception:
        return resp.text

def ig_login():
    url = f"{IG_BASE}/session"
    headers = {
        "X-IG-API-KEY": IG_API_KEY,
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json; charset=UTF-8",
        "VERSION": "2",
    }
    r = requests.post(url, headers=headers, json={
        "identifier": IG_USERNAME,
        "password": IG_PASSWORD,
        "encryptedPassword": False,
    }, timeout=20)
    log({"kind": "ig_login", "status": r.status_code, "body": safe_json(r)})
    r.raise_for_status()
    cst = r.headers.get("CST")
    sec = r.headers.get("X-SECURITY-TOKEN")
    if not cst or not sec:
        raise RuntimeError("CST / X-SECURITY-TOKEN fehlen")
    return {
        "X-IG-API-KEY": IG_API_KEY,
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json; charset=UTF-8",
        "CST": cst,
        "X-SECURITY-TOKEN": sec,
        "VERSION": "2",
    }

def ig_set_account(h):
    try:
        url = f"{IG_BASE}/session"
        r = requests.put(url, headers={**h, "VERSION": "1"},
                         json={"accountId": IG_ACCOUNT_ID, "defaultAccount": True},
                         timeout=20)
        log({"kind": "ig_set_account", "status": r.status_code})
    except Exception as e:
        log({"kind": "ig_set_account_error", "error": str(e)})

def ig_open(h, epic, direction, qty):
    url = f"{IG_BASE}/positions/otc"
    payload = {
        "epic": epic,
        "expiry": "-",
        "direction": direction.upper(),
        "size": float(qty),
        "orderType": "MARKET",
        "currencyCode": "EUR",
        "forceOpen": True,
        "guaranteedStop": False,
    }
    r = requests.post(url, headers=h, json=payload, timeout=20)
    log({"kind": "ig_open", "status": r.status_code, "body": safe_json(r)})
    r.raise_for_status()
    return r.json()

def ig_get_positions(h):
    url = f"{IG_BASE}/positions"
    r = requests.get(url, headers=h, timeout=20)
    log({"kind": "ig_positions", "status": r.status_code})
    r.raise_for_status()
    return r.json().get("positions", [])

def ig_close_all(h, epic, direction):
    positions = ig_get_positions(h)
    closed = []
    for p in positions:
        m   = p.get("market", {})
        pos = p.get("position", {})
        if m.get("epic") != epic:
            continue
        if pos.get("direction", "").upper() != direction.upper():
            continue
        deal_id   = pos.get("dealId")
        size      = pos.get("size", 0)
        close_dir = "SELL" if direction == "BUY" else "BUY"
        url = f"{IG_BASE}/positions/otc"
        payload = {
            "dealId": deal_id,
            "epic": epic,
            "expiry": m.get("expiry", "-"),
            "direction": close_dir,
            "size": float(size),
            "orderType": "MARKET",
            "timeInForce": "FILL_OR_KILL",
            "currencyCode": pos.get("currency", "EUR"),
            "forceOpen": False,
            "guaranteedStop": False,
        }
        headers = {**h, "VERSION": "1", "X-HTTP-Method-Override": "DELETE"}
        r = requests.post(url, headers=headers, json=payload, timeout=20)
        log({"kind": "ig_close", "status": r.status_code, "body": safe_json(r)})
        r.raise_for_status()
        closed.append(r.json())
    if not closed:
        raise RuntimeError(f"Keine offene {direction} Position für {epic}")
    return closed

@app.get("/")
def home():
    return "OK", 200

@app.get("/health")
def health():
    return jsonify({"ok": True, "ts": datetime.now(timezone.utc).isoformat()}), 200

@app.post("/webhook")
def webhook():
    data = request.get_json(silent=True) or {}
    log({"kind": "webhook_in", "data": data})

    if not WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "WEBHOOK_SECRET fehlt"}), 500

    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "Falsches Secret"}), 401

    wtype = (data.get("type") or "").strip().lower()

    if wtype in ["test", "test_from_tv"]:
        return jsonify({"ok": True, "ignored": True}), 200

    sym = (data.get("symbol") or "").strip().upper()
    epic = IG_EPIC if sym in ["DAX", "GER40", "DE40", "GERMANY40"] else IG_EPIC

    try:
        h = ig_login()
        ig_set_account(h)

        if wtype == "entry_long":
            qty = float(data.get("qty") or 1)
            res = ig_open(h, epic, "BUY", qty)
            return jsonify({"ok": True, "result": res}), 200

        if wtype == "entry_short":
            qty = float(data.get("qty") or 1)
            res = ig_open(h, epic, "SELL", qty)
            return jsonify({"ok": True, "result": res}), 200

        if wtype == "exit_long":
            res = ig_close_all(h, epic, "BUY")
            return jsonify({"ok": True, "result": res}), 200

        if wtype == "exit_short":
            res = ig_close_all(h, epic, "SELL")
            return jsonify({"ok": True, "result": res}), 200

        return jsonify({"ok": True, "ignored": True, "reason": "unbekannter type"}), 200

    except Exception as e:
        log({"kind": "error", "error": str(e)})
        return jsonify({"ok": False, "error": str(e)}), 500
