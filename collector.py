#!/usr/bin/env python3
"""
TML NFT Watch — collecteur
Interroge l'API publique Magic Eden pour les collections NFT officielles
de Tomorrowland, calcule des indicateurs simples (moyennes mobiles,
volume, pression de vente) et met à jour docs/data.json.
Envoie une alerte Telegram si un mouvement notable est détecté.

Aucune dépendance externe (stdlib uniquement) pour rester 100% gratuit
et exécutable tel quel dans GitHub Actions.
"""
import json
import os
import time
import urllib.request
from datetime import datetime, timezone

# --- Collections officielles TML sur Magic Eden -----------------------------
COLLECTIONS = {
    "tomorrowland_winter": "A Letter from the Universe (Winter)",
    "the_reflection_of_love": "The Reflection of Love",
    "tomorrowland_love_unity": "The Symbol of Love and Unity",
    "the_golden_auric": "The Golden Auric",
}

ME_STATS_URL = "https://api-mainnet.magiceden.dev/v2/collections/{symbol}/stats"
ME_ACTIVITIES_URL = "https://api-mainnet.magiceden.dev/v2/collections/{symbol}/activities?offset=0&limit=100"
SOL_EUR_URL = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=eur"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(SCRIPT_DIR, "docs", "data.json")
MAX_HISTORY_POINTS = 4000  # large marge, ~90j même à 1 point/15min

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

FLOOR_MOVE_ALERT_THRESHOLD = 0.08   # 8% de variation du floor -> alerte
LISTING_PRESSURE_THRESHOLD = 0.15   # 15% de variation du nb d'annonces


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "tml-nft-watch/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def get_sol_eur():
    try:
        return fetch_json(SOL_EUR_URL)["solana"]["eur"]
    except Exception as e:
        print(f"[warn] impossible de récupérer SOL/EUR: {e}")
        return None


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[info] Telegram non configuré, alerte non envoyée:\n" + text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"[warn] échec envoi Telegram: {e}")


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"collections": {}, "last_update": None, "sol_eur": None}


def save_data(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def moving_average(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def compute_signal(history, listed_now, listed_prev, sales_24h):
    """
    Heuristique volontairement simple : croisement de moyennes mobiles du
    floor + pression de l'offre (annonces en vente) + activité récente.
    C'est un indicateur informatif, pas un conseil financier — le marché
    NFT reste peu liquide et peut bouger sur très peu de transactions.
    """
    floors = [h["floor_sol"] for h in history if h.get("floor_sol")]
    if len(floors) < 5:
        return "COLLECTE EN COURS", "Pas encore assez d'historique pour un signal fiable."

    ma7 = moving_average(floors[-7:])
    ma30 = moving_average(floors[-30:])

    score = 0.0
    reasons = []

    if ma7 is not None and ma30 is not None:
        if ma7 > ma30 * 1.02:
            score += 1
            reasons.append("moyenne courte au-dessus de la moyenne longue (tendance haussière)")
        elif ma7 < ma30 * 0.98:
            score -= 1
            reasons.append("moyenne courte sous la moyenne longue (tendance baissière)")

    if sales_24h:
        score += 0.5
        reasons.append(f"{sales_24h} vente(s) confirmée(s) sur 24h")

    if listed_prev:
        pressure = (listed_now - listed_prev) / listed_prev
        if pressure > LISTING_PRESSURE_THRESHOLD:
            score -= 1
            reasons.append("forte hausse des annonces en vente (pression vendeuse)")
        elif pressure < -LISTING_PRESSURE_THRESHOLD:
            score += 0.5
            reasons.append("baisse des annonces en vente (rétention par les détenteurs)")

    if score >= 1.5:
        return "SIGNAL ACHAT", ", ".join(reasons)
    if score <= -1.5:
        return "PRUDENCE", ", ".join(reasons)
    return "SURVEILLER", ", ".join(reasons) if reasons else "Pas de mouvement net."


def main():
    data = load_data()
    sol_eur = get_sol_eur()
    now_iso = datetime.now(timezone.utc).isoformat()
    alerts = []

    for symbol, label in COLLECTIONS.items():
        try:
            stats = fetch_json(ME_STATS_URL.format(symbol=symbol))
        except Exception as e:
            print(f"[warn] stats {symbol}: {e}")
            continue

        floor_lamports = stats.get("floorPrice")
        floor_sol = round(floor_lamports / 1e9, 4) if floor_lamports else None
        listed_count = stats.get("listedCount")
        avg_price_24h = stats.get("avgPrice24hr")

        try:
            activities = fetch_json(ME_ACTIVITIES_URL.format(symbol=symbol))
        except Exception as e:
            print(f"[warn] activities {symbol}: {e}")
            activities = []

        cutoff = time.time() - 86400
        sales_24h = sum(
            1 for a in activities
            if a.get("type") == "buyNow" and a.get("blockTime", 0) >= cutoff
        )

        entry = data["collections"].setdefault(symbol, {"label": label, "history": [], "last_signal": None})
        entry["label"] = label
        history = entry["history"]
        prev_snapshot = history[-1] if history else None
        prev_floor = prev_snapshot.get("floor_sol") if prev_snapshot else None
        listed_prev = prev_snapshot.get("listed") if prev_snapshot else None

        history.append({
            "ts": now_iso,
            "floor_sol": floor_sol,
            "listed": listed_count,
            "sales_24h": sales_24h,
        })
        entry["history"] = history[-MAX_HISTORY_POINTS:]

        signal, reason = compute_signal(entry["history"], listed_count, listed_prev, sales_24h)

        entry["current"] = {
            "floor_sol": floor_sol,
            "floor_eur": round(floor_sol * sol_eur, 2) if floor_sol and sol_eur else None,
            "listed": listed_count,
            "avg_price_24h_sol": avg_price_24h,
            "sales_24h": sales_24h,
            "signal": signal,
            "signal_reason": reason,
            "updated_at": now_iso,
        }

        # --- Alertes -----------------------------------------------------
        if prev_floor and floor_sol and abs(floor_sol - prev_floor) / prev_floor >= FLOOR_MOVE_ALERT_THRESHOLD:
            direction = "monté 📈" if floor_sol > prev_floor else "chuté 📉"
            pct = abs(floor_sol - prev_floor) / prev_floor * 100
            alerts.append(
                f"🔔 <b>{label}</b>\nFloor {direction} de {pct:.1f}% : "
                f"{prev_floor:.3f} → {floor_sol:.3f} SOL"
            )

        if entry.get("last_signal") not in (None, signal):
            alerts.append(f"⚡ <b>{label}</b>\nNouveau signal : <b>{signal}</b>\n{reason}")
        entry["last_signal"] = signal

    data["last_update"] = now_iso
    data["sol_eur"] = sol_eur
    save_data(data)

    if alerts:
        send_telegram("\n\n".join(alerts))
    else:
        print("[info] aucune alerte à envoyer cette fois-ci.")


if __name__ == "__main__":
    main()
