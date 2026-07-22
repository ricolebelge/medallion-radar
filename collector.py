#!/usr/bin/env python3
"""
TML NFT Watch — collecteur v2
Interroge l'API publique Magic Eden + CoinGecko, calcule des indicateurs,
l'ATH/ATL de chaque collection depuis sa création (via l'historique complet
des transactions on-chain), et conserve les 5 dernières transactions.
Aucune dépendance externe (stdlib uniquement).
"""
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

# --- Collections officielles TML suivies (Golden Auric retirée) -------------
COLLECTIONS = {
    "tomorrowland_winter": "Letter",
    "the_reflection_of_love": "Reflection",
    "tomorrowland_love_unity": "Symbol",
}

ME_STATS_URL = "https://api-mainnet.magiceden.dev/v2/collections/{symbol}/stats"
ME_ACTIVITIES_URL = "https://api-mainnet.magiceden.dev/v2/collections/{symbol}/activities?offset={offset}&limit={limit}"
SOL_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=eur,usd"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(SCRIPT_DIR, "docs", "data.json")
MAX_HISTORY_POINTS = 4000       # historique des snapshots floor (~90j à 15min)
RECENT_TX_COUNT = 5             # nb de tx affichées sous chaque carte
MAX_SYNC_PAGES = 100            # garde-fou pour le scan complet ATH/ATL (jusqu'à 10 000 tx)
SYNC_PAGE_LIMIT = 100

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

FLOOR_MOVE_ALERT_THRESHOLD = 0.08
LISTING_PRESSURE_THRESHOLD = 0.15

TX_TYPE_LABELS_FR = {
    "buyNow": "Vente",
    "list": "Mise en vente",
    "delist": "Retrait",
    "bid": "Offre",
    "cancelBid": "Offre annulée",
    "acceptBid": "Offre acceptée",
}


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "tml-nft-watch/2.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def get_sol_rates():
    try:
        prices = fetch_json(SOL_PRICE_URL)["solana"]
        return prices.get("eur"), prices.get("usd")
    except Exception as e:
        print(f"[warn] impossible de récupérer les taux SOL: {e}")
        return None, None


def normalize_price_to_sol(raw):
    """
    L'API Magic Eden n'est pas toujours cohérente : le endpoint stats renvoie
    des lamports, activities peut renvoyer du SOL direct selon la version.
    Heuristique fiable ici : les prix de ces collections vont de ~1 à ~100 SOL,
    donc toute valeur > 1000 est forcément des lamports.
    """
    if raw is None:
        return None
    try:
        raw = float(raw)
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None
    return raw / 1e9 if raw > 1000 else raw


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
    return {"collections": {}, "last_update": None, "sol_eur": None, "sol_usdc": None}


def save_data(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def moving_average(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def pct_change_over(history, days):
    """
    % de variation du floor entre maintenant et le point le plus proche
    d'il y a `days` jours. Renvoie None si l'historique ne couvre pas
    encore assez de recul pour ce délai (évite les faux résultats).
    """
    points = [h for h in history if h.get("floor_sol") is not None and h.get("ts")]
    if len(points) < 2:
        return None
    try:
        parsed = [(datetime.fromisoformat(h["ts"]), h["floor_sol"]) for h in points]
    except Exception:
        return None
    parsed.sort(key=lambda x: x[0])
    oldest_t, _ = parsed[0]
    now_t, current_floor = parsed[-1]
    if (now_t - oldest_t) < timedelta(days=days * 0.8):
        return None
    target_t = now_t - timedelta(days=days)
    best = min(parsed, key=lambda x: abs((x[0] - target_t).total_seconds()))
    base_floor = best[1]
    if not base_floor:
        return None
    return round((current_floor - base_floor) / base_floor * 100, 1)


def compute_signal(history, listed_now, listed_prev, sales_24h, floor_sol, ath_sol, atl_sol):
    """
    Combine tendance (moyennes mobiles), pression de l'offre, activité
    récente, et position par rapport au plus bas/plus haut historique —
    pour donner une indication explicite d'achat ou de vente.
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
            reasons.append("tendance courte haussière")
        elif ma7 < ma30 * 0.98:
            score -= 1
            reasons.append("tendance courte baissière")

    if sales_24h:
        score += 0.5
        reasons.append(f"{sales_24h} vente(s) sur 24h")

    if listed_prev:
        pressure = (listed_now - listed_prev) / listed_prev
        if pressure > LISTING_PRESSURE_THRESHOLD:
            score -= 1
            reasons.append("hausse des annonces en vente (pression vendeuse)")
        elif pressure < -LISTING_PRESSURE_THRESHOLD:
            score += 0.5
            reasons.append("baisse des annonces en vente (rétention)")

    if floor_sol and atl_sol:
        proximity_to_atl = floor_sol / atl_sol
        if proximity_to_atl <= 1.15:
            score += 1
            reasons.append("proche du plus bas historique")
    if floor_sol and ath_sol:
        proximity_to_ath = floor_sol / ath_sol
        if proximity_to_ath >= 0.9:
            score -= 1
            reasons.append("proche du plus haut historique")

    if score >= 2:
        return "ACHAT FORT", ", ".join(reasons)
    if score >= 1:
        return "SIGNAL ACHAT", ", ".join(reasons)
    if score <= -2:
        return "VENDRE", ", ".join(reasons)
    if score <= -1:
        return "PRUDENCE", ", ".join(reasons)
    return "SURVEILLER", ", ".join(reasons) if reasons else "Pas de mouvement net."


def fetch_activities_page(symbol, offset, limit=SYNC_PAGE_LIMIT):
    url = ME_ACTIVITIES_URL.format(symbol=symbol, offset=offset, limit=limit)
    return fetch_json(url)


def sync_all_time_extremes(symbol):
    """
    Parcourt tout l'historique des ventes on-chain (contrat de la collection)
    pour déterminer le plus bas et le plus haut jamais atteints. Coûteux en
    appels API : à faire une seule fois par collection (voir history_synced).
    """
    atl_sol = atl_ts = ath_sol = ath_ts = None
    offset = 0
    pages = 0
    while pages < MAX_SYNC_PAGES:
        try:
            batch = fetch_activities_page(symbol, offset)
        except Exception as e:
            print(f"[warn] sync ATH/ATL {symbol} offset={offset}: {e}")
            break
        if not batch:
            break
        for a in batch:
            if a.get("type") != "buyNow":
                continue
            price_sol = normalize_price_to_sol(a.get("price"))
            if price_sol is None:
                continue
            bt = a.get("blockTime")
            ts_iso = datetime.fromtimestamp(bt, tz=timezone.utc).isoformat() if bt else None
            if atl_sol is None or price_sol < atl_sol:
                atl_sol, atl_ts = price_sol, ts_iso
            if ath_sol is None or price_sol > ath_sol:
                ath_sol, ath_ts = price_sol, ts_iso
        if len(batch) < SYNC_PAGE_LIMIT:
            break
        offset += SYNC_PAGE_LIMIT
        pages += 1
        time.sleep(0.15)
    print(f"[info] sync ATH/ATL {symbol}: {pages+1} page(s) scannée(s), "
          f"ATL={atl_sol}, ATH={ath_sol}")
    return {"atl_sol": atl_sol, "atl_ts": atl_ts, "ath_sol": ath_sol, "ath_ts": ath_ts}


def build_recent_tx(activities):
    tx_list = []
    for a in activities[:RECENT_TX_COUNT]:
        bt = a.get("blockTime")
        tx_list.append({
            "type": a.get("type"),
            "type_label": TX_TYPE_LABELS_FR.get(a.get("type"), a.get("type") or "—"),
            "price_sol": normalize_price_to_sol(a.get("price")),
            "ts": datetime.fromtimestamp(bt, tz=timezone.utc).isoformat() if bt else None,
        })
    return tx_list


def main():
    data = load_data()
    sol_eur, sol_usdc = get_sol_rates()
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
        avg_price_24h = normalize_price_to_sol(stats.get("avgPrice24hr"))

        try:
            activities = fetch_activities_page(symbol, 0, limit=20)
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

        # --- ATH / ATL depuis le contrat ---------------------------------
        if not entry.get("history_synced"):
            extremes = sync_all_time_extremes(symbol)
            entry["ath_sol"] = extremes["ath_sol"]
            entry["ath_ts"] = extremes["ath_ts"]
            entry["atl_sol"] = extremes["atl_sol"]
            entry["atl_ts"] = extremes["atl_ts"]
            entry["history_synced"] = True
            entry["synced_at"] = now_iso
        else:
            for a in activities:
                if a.get("type") != "buyNow":
                    continue
                price_sol = normalize_price_to_sol(a.get("price"))
                if price_sol is None:
                    continue
                bt = a.get("blockTime")
                ts_iso = datetime.fromtimestamp(bt, tz=timezone.utc).isoformat() if bt else None
                if entry.get("atl_sol") is None or price_sol < entry["atl_sol"]:
                    entry["atl_sol"], entry["atl_ts"] = price_sol, ts_iso
                if entry.get("ath_sol") is None or price_sol > entry["ath_sol"]:
                    entry["ath_sol"], entry["ath_ts"] = price_sol, ts_iso

        entry["recent_tx"] = build_recent_tx(activities)

        signal, reason = compute_signal(
            entry["history"], listed_count, listed_prev, sales_24h,
            floor_sol, entry.get("ath_sol"), entry.get("atl_sol"),
        )
        pct_7d = pct_change_over(entry["history"], 7)
        pct_30d = pct_change_over(entry["history"], 30)

        entry["current"] = {
            "floor_sol": floor_sol,
            "floor_eur": round(floor_sol * sol_eur, 2) if floor_sol and sol_eur else None,
            "floor_usdc": round(floor_sol * sol_usdc, 2) if floor_sol and sol_usdc else None,
            "listed": listed_count,
            "avg_price_24h_sol": avg_price_24h,
            "sales_24h": sales_24h,
            "signal": signal,
            "signal_reason": reason,
            "pct_7d": pct_7d,
            "pct_30d": pct_30d,
            "updated_at": now_iso,
        }

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

        time.sleep(0.2)

    # --- Retire les collections qu'on ne suit plus (ex: Golden Auric) -------
    for symbol in list(data["collections"].keys()):
        if symbol not in COLLECTIONS:
            print(f"[info] retrait de la collection non suivie: {symbol}")
            del data["collections"][symbol]

    data["last_update"] = now_iso
    data["sol_eur"] = sol_eur
    data["sol_usdc"] = sol_usdc
    save_data(data)

    if alerts:
        send_telegram("\n\n".join(alerts))
    else:
        print("[info] aucune alerte à envoyer cette fois-ci.")


if __name__ == "__main__":
    main()
