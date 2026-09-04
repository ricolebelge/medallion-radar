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
import urllib.error
from datetime import datetime, timedelta, timezone

# --- Collections officielles TML suivies (Golden Auric retirée) -------------
COLLECTIONS = {
    "tomorrowland_winter": "Letter",
    "the_reflection_of_love": "Reflection",
    "tomorrowland_love_unity": "Symbol",
}

ME_STATS_URL = "https://api-mainnet.magiceden.dev/v2/collections/{symbol}/stats"
ME_HOLDER_STATS_URL = "https://api-mainnet.magiceden.dev/v2/collections/{symbol}/holder_stats"
ME_ACTIVITIES_URL = "https://api-mainnet.magiceden.dev/v2/collections/{symbol}/activities?offset={offset}&limit={limit}"
SOL_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=eur,usd"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(SCRIPT_DIR, "docs", "data.json")
MAX_HISTORY_POINTS = 5760       # ~60 jours de snapshots à 15 min (24*60/15*60)
RECENT_TX_COUNT = 5             # nb de tx affichées sous chaque carte
MAX_SYNC_PAGES = 3000            # garde-fou dur (300 000 activités) — sert de filet, pas de limite pratique
SYNC_TIME_BUDGET_SECONDS = 240   # limite réelle : 4 min max de scan par collection
SYNC_PAGE_LIMIT = 100

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

FLOOR_MOVE_ALERT_THRESHOLD = 0.08
LISTING_PRESSURE_THRESHOLD = 0.15
HEARTBEAT_INTERVAL_SECONDS = 2 * 60 * 60  # 2h

TX_TYPE_LABELS_FR = {
    "buyNow": "Vente",
    "list": "Mise en vente",
    "delist": "Retrait",
    "bid": "Offre",
    "cancelBid": "Offre annulée",
    "acceptBid": "Offre acceptée",
}


def extract_ts_epoch(activity):
    """
    Certaines activités (notamment les événements auto-générés par les pools
    de market-making, très fréquents sur ces collections) semblent ne pas
    toujours porter blockTime de façon fiable. On se rabat sur createdAt
    (toujours présent en pratique, format ISO) plutôt que de perdre la date.
    """
    bt = activity.get("blockTime")
    if bt:
        try:
            return float(bt)
        except (TypeError, ValueError):
            pass
    created = activity.get("createdAt")
    if created:
        try:
            return datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            pass
    return None


def extract_ts_iso(activity):
    epoch = extract_ts_epoch(activity)
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


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


TELEGRAM_DEBUG = {}


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[info] Telegram non configuré, alerte non envoyée:\n" + text)
        TELEGRAM_DEBUG["status"] = "not_configured"
        TELEGRAM_DEBUG["token_present"] = bool(TELEGRAM_TOKEN)
        TELEGRAM_DEBUG["chat_id_present"] = bool(TELEGRAM_CHAT_ID)
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
        with urllib.request.urlopen(req, timeout=15) as resp:
            TELEGRAM_DEBUG["status"] = "ok"
            TELEGRAM_DEBUG["http_code"] = resp.status
    except Exception as e:
        print(f"[warn] échec envoi Telegram: {e}")
        TELEGRAM_DEBUG["status"] = "error"
        TELEGRAM_DEBUG["error"] = str(e)


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


def compute_signal(history, listed_now, listed_prev, sales_24h, floor_sol, ath_sol, atl_sol, history_complete=False):
    """
    Combine tendance (moyennes mobiles), pression de l'offre, activité
    récente, et position par rapport au plus bas/plus haut historique —
    pour donner une indication explicite d'achat ou de vente.

    Tant que le scan ATH/ATL n'est pas terminé (history_complete=False),
    l'ATH/ATL connu n'est qu'une borne partielle (le vrai extrême peut être
    plus loin, pas encore scanné) : son poids dans le score est réduit de
    moitié et la raison affichée le précise, pour éviter un signal PRUDENCE/
    VENDRE prématuré basé sur un plus haut sous-estimé.
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

    # --- Position relative dans la fourchette ATH-ATL --------------------
    # (plutôt que deux seuils indépendants qui peuvent se déclencher
    # ensemble et se contredire quand l'écart ATH-ATL est faible)
    if floor_sol and ath_sol and atl_sol and ath_sol > atl_sol:
        position = (floor_sol - atl_sol) / (ath_sol - atl_sol)  # 0=ATL, 1=ATH
        weight = 1.0 if history_complete else 0.5
        partial_tag = "" if history_complete else " (ATH/ATL partiel, fiabilité réduite)"
        if position <= 0.15:
            score += weight
            reasons.append(f"proche du plus bas historique{partial_tag}")
        elif position >= 0.85:
            score -= weight
            reasons.append(f"proche du plus haut historique{partial_tag}")

    if score >= 2:
        return "ACHAT FORT", ", ".join(reasons)
    if score >= 1:
        return "SIGNAL ACHAT", ", ".join(reasons)
    if score <= -2:
        return "VENDRE", ", ".join(reasons)
    if score <= -1:
        return "PRUDENCE", ", ".join(reasons)
    return "SURVEILLER", ", ".join(reasons) if reasons else "Pas de mouvement net."


def fetch_holder_count(symbol):
    """
    Récupère le nombre de porteurs uniques d'une collection. Le nom exact
    du champ n'est pas garanti stable dans la doc publique de Magic Eden,
    donc on essaie plusieurs clés candidates et on déballe une éventuelle
    enveloppe ("results"/"data") plutôt que de planter sur un format inattendu.
    """
    try:
        stats = fetch_json(ME_HOLDER_STATS_URL.format(symbol=symbol))
    except Exception as e:
        print(f"[warn] holder_stats {symbol}: {e}")
        return None
    candidates = [stats]
    if isinstance(stats, dict):
        for wrapper_key in ("results", "data", "result"):
            if isinstance(stats.get(wrapper_key), dict):
                candidates.append(stats[wrapper_key])
    for obj in candidates:
        if not isinstance(obj, dict):
            continue
        for key in (
            "uniqueHolders", "uniqueHolderCount", "holderCount", "totalHolders",
            "holders", "totalUniqueHolders", "numHolders", "holder_count",
        ):
            if key in obj and obj[key] is not None:
                try:
                    return int(obj[key])
                except (TypeError, ValueError):
                    continue
    print(f"[warn] holder_stats {symbol}: champ porteurs non trouvé dans {stats}")
    return None


def fetch_activities_page(symbol, offset, limit=SYNC_PAGE_LIMIT):
    url = ME_ACTIVITIES_URL.format(symbol=symbol, offset=offset, limit=limit)
    return fetch_json(url)


def fetch_activities_covering(symbol, max_pages=10, page_size=50):
    """
    Pagine sur /activities jusqu'à max_pages (soit ~500 items par défaut).
    Note : l'API plafonne en pratique à ~50 items par appel quel que soit
    `limit` demandé, malgré une doc annonçant jusqu'à 1000 — d'où la
    pagination manuelle. Vu le bruit des pools de market-making (bid/
    poolUpdate très fréquents), même 500 items ne couvre pas toujours 24h
    complètes : le compteur de ventes qui en dérive est donc calculé sur la
    fenêtre réellement couverte puis extrapolé sur 24h (voir sales_24h_est).
    """
    all_activities = []
    offset = 0
    for _ in range(max_pages):
        page = fetch_activities_page(symbol, offset, limit=page_size)
        if not page:
            break
        all_activities.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return all_activities


def sync_all_time_extremes(symbol, start_offset=0, prev_atl_sol=None, prev_atl_ts=None,
                            prev_ath_sol=None, prev_ath_ts=None):
    """
    Parcourt l'historique des ventes on-chain (contrat de la collection)
    pour déterminer le plus bas et le plus haut jamais atteints. Reprenable :
    si le budget temps (SYNC_TIME_BUDGET_SECONDS) est atteint avant la fin,
    renvoie l'offset atteint pour que le prochain passage reprenne pile là
    où celui-ci s'est arrêté, au lieu de tout rescanner depuis le début.
    """
    atl_sol, atl_ts = prev_atl_sol, prev_atl_ts
    ath_sol, ath_ts = prev_ath_sol, prev_ath_ts
    oldest_ts = None
    offset = start_offset
    pages = 0
    started = time.monotonic()
    complete = False
    last_error = None
    api_limit_reached = False
    while pages < MAX_SYNC_PAGES:
        if time.monotonic() - started > SYNC_TIME_BUDGET_SECONDS:
            print(f"[info] sync {symbol}: budget temps atteint après {pages} page(s), reprise à offset={offset}")
            break
        try:
            batch = fetch_activities_page(symbol, offset)
        except urllib.error.HTTPError as e:
            if e.code == 400:
                # L'API Magic Eden refuse la pagination au-delà d'une certaine
                # profondeur (offset trop grand) : on ne pourra jamais aller plus
                # loin avec cette méthode. On arrête ici plutôt que de retenter
                # le même offset en échec à chaque run, indéfiniment.
                print(f"[info] sync {symbol}: limite de pagination API atteinte à offset={offset} (400) — arrêt du scan à ce niveau")
                api_limit_reached = True
            else:
                last_error = f"HTTP {e.code}: {e.reason}"
            break
        except Exception as e:
            last_error = str(e)
            break
        if not batch:
            complete = True
            break
        for a in batch:
            ts_iso = extract_ts_iso(a)
            if ts_iso and (oldest_ts is None or ts_iso < oldest_ts):
                oldest_ts = ts_iso
            if a.get("type") != "buyNow":
                continue
            price_sol = normalize_price_to_sol(a.get("price"))
            if price_sol is None:
                continue
            if atl_sol is None or price_sol < atl_sol:
                atl_sol, atl_ts = price_sol, ts_iso
            if ath_sol is None or price_sol > ath_sol:
                ath_sol, ath_ts = price_sol, ts_iso
        if len(batch) < SYNC_PAGE_LIMIT:
            complete = True
            break
        offset += SYNC_PAGE_LIMIT
        pages += 1
        time.sleep(0.15)
    elapsed = round(time.monotonic() - started, 1)
    print(f"[info] sync ATH/ATL {symbol}: {pages+1} page(s), {elapsed}s, "
          f"complet={complete}, offset atteint={offset}, ATL={atl_sol}, ATH={ath_sol}")
    return {
        "atl_sol": atl_sol, "atl_ts": atl_ts,
        "ath_sol": ath_sol, "ath_ts": ath_ts,
        "synced_back_to": oldest_ts,
        "history_complete": complete,
        "next_offset": offset,
        "last_error": last_error,
        "api_limit_reached": api_limit_reached,
    }


def build_recent_tx(activities):
    tx_list = []
    for a in activities[:RECENT_TX_COUNT]:
        tx_list.append({
            "type": a.get("type"),
            "type_label": TX_TYPE_LABELS_FR.get(a.get("type"), a.get("type") or "—"),
            "price_sol": normalize_price_to_sol(a.get("price")),
            "ts": extract_ts_iso(a),
        })
    return tx_list


MARKET_ALERT_ICONS = {"list": "📋", "buyNow": "💰", "acceptBid": "🤝"}
MARKET_ALERT_TITLES = {"list": "Nouvelle offre vendeur", "buyNow": "Achat réalisé", "acceptBid": "Offre acceptée (vente privée)"}


MARKET_ALERT_DEDUP_WINDOW_SECONDS = 2 * 60 * 60  # 2h : ignore les re-listings/ajustements répétés (pools MM)


def build_market_alerts(entry, activities, label, floor_sol, sol_usdc):
    """
    Alerte Telegram pour chaque nouvelle offre vendeur (list) et achat
    réalisé (buyNow), avec écart vs floor, prix en SOL/USDC, et liens
    cliquables vers le wallet (Solscan + Magic Eden) et la NFT concernée.
    Dédoublonnage :
      1) par timestamp (ne revoit pas ce qui a déjà été scanné)
      2) par (type, tokenMint) sur une fenêtre de 2h, pour ignorer les
         re-listings/ajustements de prix répétés sur la même NFT par les
         pools de market-making (ex: baisse de prix par petits paliers)
    """
    last_ts = entry.get("last_market_alert_ts")
    relevant = []
    for a in activities:
        if a.get("type") not in MARKET_ALERT_ICONS:
            continue
        price_sol = normalize_price_to_sol(a.get("price"))
        ts_iso = extract_ts_iso(a)
        if price_sol is None or ts_iso is None:
            continue
        relevant.append((ts_iso, a))
    relevant.sort(key=lambda x: x[0])  # plus ancien -> plus récent

    if last_ts is None:
        # Premier run : on fixe la référence sans spammer l'historique existant
        if relevant:
            entry["last_market_alert_ts"] = relevant[-1][0]
        return []

    recent_keys = entry.get("recent_alert_keys", [])  # [{"key": "...", "ts": iso}, ...]

    def _epoch(ts_iso_str):
        return datetime.fromisoformat(ts_iso_str).timestamp()

    new_alerts = []
    newest_ts = last_ts
    for ts_iso, a in relevant:
        if ts_iso <= last_ts:
            continue
        newest_ts = max(newest_ts, ts_iso)

        tx_type = a.get("type")
        price_sol = normalize_price_to_sol(a.get("price"))

        dedup_key = f"{tx_type}:{a.get('tokenMint') or f'price:{price_sol:.3f}'}"
        now_epoch = _epoch(ts_iso)
        is_duplicate = any(
            rk["key"] == dedup_key and (now_epoch - _epoch(rk["ts"])) < MARKET_ALERT_DEDUP_WINDOW_SECONDS
            for rk in recent_keys
        )
        recent_keys.append({"key": dedup_key, "ts": ts_iso})
        if is_duplicate:
            continue

        icon = MARKET_ALERT_ICONS[tx_type]
        title = MARKET_ALERT_TITLES[tx_type]
        spread_txt = ""
        if floor_sol:
            spread_pct = (price_sol - floor_sol) / floor_sol * 100
            sign = "+" if spread_pct >= 0 else ""
            spread_txt = f" ({sign}{spread_pct:.1f}% vs floor {floor_sol:.3f})"
        usdc_txt = f"\n≈ {price_sol * sol_usdc:,.0f} USDC".replace(",", " ") if sol_usdc else ""

        # Wallet pertinent : le vendeur pour une mise en vente, l'acheteur pour un achat
        wallet = a.get("seller") if tx_type == "list" else a.get("buyer")
        links = []
        if wallet:
            links.append(f'<a href="https://solscan.io/account/{wallet}">Wallet Solscan</a>')
            links.append(f'<a href="https://magiceden.io/u/{wallet}">Wallet ME</a>')
        token_mint = a.get("tokenMint")
        if token_mint:
            links.append(f'<a href="https://magiceden.io/item-details/{token_mint}">NFT</a>')
        links_txt = f"\n{' · '.join(links)}" if links else ""

        new_alerts.append(f"{icon} <b>{label}</b> — {title}\n{price_sol:.3f} SOL{spread_txt}{usdc_txt}{links_txt}")

    # Purge les entrées trop anciennes pour ne pas laisser grossir la liste indéfiniment
    cutoff_epoch = time.time() - MARKET_ALERT_DEDUP_WINDOW_SECONDS
    entry["recent_alert_keys"] = [rk for rk in recent_keys if _epoch(rk["ts"]) >= cutoff_epoch]

    entry["last_market_alert_ts"] = newest_ts
    return new_alerts


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
            activities = fetch_activities_covering(symbol, max_pages=10, page_size=50)
        except Exception as e:
            print(f"[warn] activities {symbol}: {e}")
            activities = []

        if activities:
            oldest_epoch = min((extract_ts_epoch(a) or time.time()) for a in activities)
            coverage_hours = max((time.time() - oldest_epoch) / 3600, 0.01)
        else:
            coverage_hours = None

        sales_in_window = sum(
            1 for a in activities if a.get("type") in ("buyNow", "acceptBid")
        )
        # Extrapolation sur 24h à partir de la fenêtre réellement couverte
        # (souvent < 24h à cause du bruit des pools MM) — indicateur de tendance,
        # pas un comptage exact.
        sales_24h = (
            round(sales_in_window / coverage_hours * 24)
            if coverage_hours else 0
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

        # --- ATH / ATL depuis le contrat (reprenable sur plusieurs runs) --
        if not entry.get("history_complete") and not entry.get("history_capped_by_api"):
            try:
                extremes = sync_all_time_extremes(
                    symbol,
                    start_offset=entry.get("sync_offset", 0),
                    prev_atl_sol=entry.get("atl_sol"), prev_atl_ts=entry.get("atl_ts"),
                    prev_ath_sol=entry.get("ath_sol"), prev_ath_ts=entry.get("ath_ts"),
                )
                entry["ath_sol"] = extremes["ath_sol"]
                entry["ath_ts"] = extremes["ath_ts"]
                entry["atl_sol"] = extremes["atl_sol"]
                entry["atl_ts"] = extremes["atl_ts"]
                entry["synced_back_to"] = extremes["synced_back_to"]
                # Si l'API refuse d'aller plus loin (limite de pagination), on
                # arrête définitivement les tentatives à ce niveau plutôt que
                # de retenter le même offset en échec à chaque run. On le
                # distingue explicitement d'un scan réellement terminé.
                entry["history_complete"] = extremes["history_complete"]
                entry["history_capped_by_api"] = extremes["api_limit_reached"]
                if not extremes["api_limit_reached"]:
                    entry["sync_offset"] = extremes["next_offset"]
                entry["history_synced"] = True  # conservé pour compatibilité avec data.json existants
                entry["synced_at"] = now_iso
            except Exception as e:
                print(f"[warn] sync ATH/ATL {symbol}: {e}")
        else:
            for a in activities:
                if a.get("type") != "buyNow":
                    continue
                price_sol = normalize_price_to_sol(a.get("price"))
                if price_sol is None:
                    continue
                ts_iso = extract_ts_iso(a)
                if entry.get("atl_sol") is None or price_sol < entry["atl_sol"]:
                    entry["atl_sol"], entry["atl_ts"] = price_sol, ts_iso
                if entry.get("ath_sol") is None or price_sol > entry["ath_sol"]:
                    entry["ath_sol"], entry["ath_ts"] = price_sol, ts_iso

        entry["recent_tx"] = build_recent_tx(activities)
        alerts.extend(build_market_alerts(entry, activities, label, floor_sol, sol_usdc))

        signal, reason = compute_signal(
            entry["history"], listed_count, listed_prev, sales_24h,
            floor_sol, entry.get("ath_sol"), entry.get("atl_sol"),
            history_complete=entry.get("history_complete", False),
        )
        pct_7d = pct_change_over(entry["history"], 7)
        pct_30d = pct_change_over(entry["history"], 30)

        holders = fetch_holder_count(symbol)

        # Ratio de liquidité : part du listing actuel qui a tourné en 24h.
        # Ex: 20 ventes / 100 listés = 20% -> marché liquide.
        # Rappel : sales_24h est approximatif (dérivé des 50 dernières activités,
        # peut sous-compter sur une collection très active où >50 tx/24h ont lieu).
        liquidity_ratio_pct = (
            round(sales_24h / listed_count * 100, 1)
            if listed_count else None
        )

        entry["current"] = {
            "floor_sol": floor_sol,
            "floor_eur": round(floor_sol * sol_eur, 2) if floor_sol and sol_eur else None,
            "floor_usdc": round(floor_sol * sol_usdc, 2) if floor_sol and sol_usdc else None,
            "listed": listed_count,
            "avg_price_24h_sol": avg_price_24h,
            "sales_24h": sales_24h,
            "liquidity_ratio_pct": liquidity_ratio_pct,
            "sales_coverage_hours": round(coverage_hours, 1) if coverage_hours else None,
            "holders": holders,
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
    holder_counts = [
        e["current"]["holders"] for e in data["collections"].values()
        if e.get("current", {}).get("holders") is not None
    ]
    data["total_holders_naive"] = sum(holder_counts) if holder_counts else None
    # ATTENTION : somme NON dédupliquée — un wallet possédant des NFT de
    # plusieurs séries est compté plusieurs fois. Magic Eden ne fournit pas
    # la liste des wallets, seulement un compte par collection, donc un vrai
    # dédoublonnage across-collections nécessiterait un scan on-chain complet.
    save_data(data)

    if alerts:
        send_telegram("\n\n".join(alerts))
    else:
        print("[info] aucune alerte à envoyer cette fois-ci.")

    # --- Heartbeat : confirme que le collecteur tourne, même sans alerte ---
    last_hb = data.get("last_heartbeat_ts")
    hb_due = last_hb is None or (
        datetime.now(timezone.utc) - datetime.fromisoformat(last_hb)
    ).total_seconds() >= HEARTBEAT_INTERVAL_SECONDS
    if hb_due:
        lines = ["🫀 <b>Heartbeat</b> — collecteur actif"]
        for symbol, label in COLLECTIONS.items():
            current = data["collections"].get(symbol, {}).get("current", {})
            floor = current.get("floor_sol")
            liq = current.get("liquidity_ratio_pct")
            floor_txt = f"{floor:.3f} SOL" if floor is not None else "N/A"
            liq_txt = f" · liquidité {liq:.1f}% (24h)" if liq is not None else ""
            lines.append(f"{label} : {floor_txt}{liq_txt}")
        send_telegram("\n".join(lines))
        data["last_heartbeat_ts"] = now_iso
        save_data(data)


if __name__ == "__main__":
    main()
