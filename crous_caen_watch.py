# -*- coding: utf-8 -*-
"""
crous_caen_watch.py — Alerte instantanée dès qu'un logement CROUS se libère à Caen
=================================================================================

Ce que fait le script, toutes les N minutes :
  1. Lit la liste PUBLIQUE des phases ouvertes :
       https://trouverunlogement.lescrous.fr/api/fr/tools
     -> il choisit tout seul la/les phase(s) active(s). Quand la
        « Phase complémentaire 2026-2027 » ferme (02/11/2026 10h00) et qu'une
        nouvelle phase (ex. « Fil de l'eau 2026-27 ») ouvre, il bascule SANS
        que tu touches au code, et il t'envoie une notification.
  2. Interroge l'API de recherche publique sur la zone Caen + Hérouville
     (Campus 1, Lébisey/Satie, Côte de Nacre/Campus 2, Claude Bloch, Hérouville).
  3. Envoie une notification push sur ton téléphone (app gratuite ntfy)
     pour chaque NOUVEAU logement, avec un lien direct vers l'annonce.

Il ne se connecte PAS à ton compte et ne réserve rien : la réservation,
c'est toi qui la fais en 1 clic depuis la notification (sois déjà connecté
à MesServices sur ton téléphone).

Installation (5 minutes)
------------------------
  1. Installe Python 3.10+ puis :   pip install requests
  2. Installe l'app « ntfy » (Android / iOS), touche « + » et abonne-toi
     à un topic secret, ex. : crous-caen-mad-7Q4x9  (il sert de mot de passe)
  3. Mets ce même topic dans NTFY_TOPIC ci-dessous (ou dans un fichier .env)
  4. Lance :   python crous_caen_watch.py
     -> tu reçois « Surveillance CROUS Caen active » sur ton téléphone.
  Test sans réseau ntfy :  DRY_RUN=1 python crous_caen_watch.py

Toutes les valeurs de CONFIG peuvent être surchargées par une variable
d'environnement du même nom ou par un fichier .env (KEY=VALUE) à côté du script.
Basé sur la méthode du projet public github.com/sofiane200206/crous-script (Rouen).
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests


# ============================== CONFIG =======================================
def _load_dotenv() -> None:
    p = Path(__file__).with_name(".env")
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


BASE = "https://trouverunlogement.lescrous.fr"
TOOLS_URL = f"{BASE}/api/fr/tools"
SEARCH_URL = BASE + "/api/fr/search/{tool_id}"
LISTING_URL = BASE + "/tools/{tool_id}/accommodations/{item_id}"

# Zone Caen + Hérouville-Saint-Clair (coin Nord-Ouest, coin Sud-Est)
BBOX_LON_WEST = float(_env("BBOX_LON_WEST", "-0.46"))
BBOX_LAT_NORTH = float(_env("BBOX_LAT_NORTH", "49.245"))
BBOX_LON_EAST = float(_env("BBOX_LON_EAST", "-0.29"))
BBOX_LAT_SOUTH = float(_env("BBOX_LAT_SOUTH", "49.145"))

# Filtre facultatif sur le nom de résidence (vide = tout Caen).
# Ex. : "Lebisey,Satie,Cote de Nacre,Claude Bloch,Campus 1,Saint-Ouen"
RESIDENCE_FILTER = _env("RESIDENCE_FILTER", "")
# Loyer max en euros (0 = pas de limite)
MAX_RENT_EUR = float(_env("MAX_RENT_EUR", "0"))
# Lieu d'études pour le classement (défaut : Campus 2, bd Maréchal Juin)
STUDY_LAT = float(_env("STUDY_LAT", "49.2135"))
STUDY_LON = float(_env("STUDY_LON", "-0.3683"))
STUDY_NAME = _env("STUDY_NAME", "Campus 2")
EXCLUDE_PMR = _env("EXCLUDE_PMR", "1") == "1"        # logements adaptés PMR
EXCLUDE_COUPLE_ONLY = _env("EXCLUDE_COUPLE_ONLY", "1") == "1"
# Your pending request (0 = none). Alerts then say whether switching is worth it.
CURRENT_RENT_EUR = float(_env("CURRENT_RENT_EUR", "0") or 0)
CURRENT_DIST_KM = float(_env("CURRENT_DIST_KM", "0") or 0)
CURRENT_LABEL = _env("CURRENT_LABEL", "ta demande actuelle")
SWITCH_MIN_SAVING_EUR = float(_env("SWITCH_MIN_SAVING_EUR", "50"))
# Only notify rooms scoring at least this (75 = ⭐⭐⭐ TOP, 55 = ⭐⭐, 0 = all)
MIN_ALERT_SCORE = int(_env("MIN_ALERT_SCORE", "0") or 0)


def switch_advice(rent: float | None, dist: float | None,
                  shared: bool = False) -> tuple[str, bool]:
    """(texte, vaut_le_coup) en comparant avec la demande en cours."""
    if CURRENT_RENT_EUR <= 0 or rent is None:
        return "", True
    if shared:
        return f"✋ Colocation (partagée) : pas mieux que {CURRENT_LABEL}, garde ta demande.", False
    saving = CURRENT_RENT_EUR - rent
    closer = dist is not None and CURRENT_DIST_KM > 0 and dist < CURRENT_DIST_KM - 1.0
    if saving >= SWITCH_MIN_SAVING_EUR or (saving >= 0 and closer):
        why = f"{saving:.0f} €/mois de moins" if saving >= 1 else "même prix"
        if closer:
            why += ", bien plus près"
        return (f"💡 CHANGE : {why} que {CURRENT_LABEL} ({CURRENT_RENT_EUR:.0f} €). "
                f"Annule ta demande dans « Mon dossier » puis réserve ici."), True
    if saving > 0:
        return f"≈ {saving:.0f} €/mois de moins que {CURRENT_LABEL} : pas assez pour changer.", False
    return f"✋ Pas mieux que {CURRENT_LABEL} ({CURRENT_RENT_EUR:.0f} €) : garde ta demande.", False

# --- Request budget (be gentle with the Crous servers) -----------------------
# Every run does 1 cheap search: new listings are seen within one cron tick.
# The per-room availability sweep (catches cancellations) is spaced out:
# every FAST_SWEEP_MIN during office hours, every SLOW_SWEEP_MIN otherwise.
FAST_SWEEP_MIN = int(_env("FAST_SWEEP_MIN", "5"))
SLOW_SWEEP_MIN = int(_env("SLOW_SWEEP_MIN", "20"))
TOOLS_CACHE_MIN = int(_env("TOOLS_CACHE_MIN", "5"))   # phase list rarely changes
FAST_HOURS = (9, 19)          # Paris time, [start, end)
FAST_WEEKDAYS = range(0, 5)   # Monday..Friday

NTFY_TOPIC = _env("NTFY_TOPIC", "crous-caen-CHANGE-ME")
NTFY_URL = _env("NTFY_URL", "https://ntfy.sh")

INTERVAL_SECONDS = int(_env("INTERVAL_SECONDS", "180"))  # 3 min
RUN_ONCE = _env("RUN_ONCE", "0") == "1"   # 1 cycle puis stop (GitHub Actions)
DRY_RUN = _env("DRY_RUN", "0") == "1"     # affiche au lieu de notifier
STATE_FILE = Path(__file__).with_name(_env("STATE_FILE", "crous_caen_state.json"))
TIMEOUT = 30

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Origin": BASE,
    "Referer": BASE + "/",
}


# ============================== UTILS ========================================
def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def _norm(s: str) -> str:
    """minuscules sans accents ni tirets, pour comparer les noms de résidence"""
    table = str.maketrans("éèêëàâäîïôöûüùç-'’", "eeeeaaaiioouuuc   ")
    return " ".join(s.lower().translate(table).split())


def notify(title: str, message: str, link: str | None = None,
           priority: int = 3, tags: list[str] | None = None) -> None:
    if DRY_RUN:
        log(f"[NOTIF] {title} | {message} | {link or ''}")
        return
    body = {"topic": NTFY_TOPIC, "title": title, "message": message,
            "priority": priority, "tags": tags or []}
    if link:
        body["click"] = link
        body["actions"] = [{"action": "view", "label": "Ouvrir l'annonce", "url": link}]
    try:
        r = requests.post(NTFY_URL, json=body, timeout=TIMEOUT)
        if r.status_code >= 400:
            log(f"ntfy HTTP {r.status_code}: {r.text[:200]}")
    except requests.RequestException as e:
        log(f"ntfy error: {e}")


def load_state() -> dict:
    try:
        if STATE_FILE.is_file():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log("State file unreadable, starting fresh.")
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# ============================== API ==========================================
def active_tools(now: datetime | None = None) -> list[dict]:
    """Phases publiées, activées et dont la période contient maintenant."""
    now = now or datetime.now(timezone.utc)
    r = requests.get(TOOLS_URL, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    tools = r.json()
    out = []
    for t in tools if isinstance(tools, list) else []:
        if not t.get("enabled", True) or not t.get("published", True) or t.get("locked"):
            continue
        start, end = _parse_dt(t.get("startDate")), _parse_dt(t.get("endDate"))
        if start and now < start:
            continue
        if end and now >= end:
            continue
        out.append(t)
    return out


def search_payload(tool: dict, page: int) -> dict:
    return {
        "idTool": tool["id"],
        "need_aggregation": True,
        "page": page,
        "pageSize": 200,
        "sector": None,
        "occupationModes": [],
        "location": [
            {"lon": BBOX_LON_WEST, "lat": BBOX_LAT_NORTH},
            {"lon": BBOX_LON_EAST, "lat": BBOX_LAT_SOUTH},
        ],
        "residence": None,
        "precision": 7,
        "equipment": [],
        "price": {"min": 0, "max": 10000000},
        "toolMechanism": tool.get("mechanism", "residual"),
    }


def fetch_listings(tool: dict) -> list[dict]:
    items, page = [], 1
    url = SEARCH_URL.format(tool_id=tool["id"])
    while True:
        r = requests.post(url, json=search_payload(tool, page), headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        res = r.json().get("results")
        if not isinstance(res, dict) or "items" not in res:
            raise ValueError("Unexpected JSON structure from search API")
        batch = res.get("items") or []
        items.extend(batch)
        total = res.get("total", 0)
        if isinstance(total, dict):
            total = total.get("value", 0)
        if not batch or len(items) >= int(total or 0) or page >= 20:
            return items
        page += 1


AVAIL_URL = BASE + "/api/fr/tools/{tool_id}/accommodations/{item_id}/availabilities"
DEPARTURE_DATE = _env("DEPARTURE_DATE", "2027-06-30")


def free_periods(tool_id, item: dict) -> list | None:
    """Périodes réellement réservables d'un logement (None si la vérif échoue).
    Une annonce peut rester affichée alors qu'elle est déjà prise : seul ce
    contrôle dit si on peut vraiment réserver."""
    from datetime import date, timedelta
    modes = [m.get("type") for m in (item.get("occupationModes") or []) if isinstance(m, dict)]
    mode = "alone" if "alone" in modes else (modes[0] if modes else "alone")
    arrival = max(date.today() + timedelta(days=2), date(2026, 11, 5))
    departure = date.fromisoformat(DEPARTURE_DATE)
    if departure < arrival + timedelta(days=31):
        departure = arrival + timedelta(days=60)
    try:
        r = requests.get(AVAIL_URL.format(tool_id=tool_id, item_id=item.get("id")),
                         params={"occupationMode": mode, "arrivalDate": arrival.isoformat(),
                                 "departureDate": departure.isoformat()},
                         headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        periods = r.json().get("periodsAvailable")
        return periods if isinstance(periods, list) else None
    except (requests.RequestException, ValueError):
        return None


SNAPSHOT_FILE = Path(__file__).with_name("caen_listings.json")


def snapshot_row(item: dict, periods) -> dict:
    """Compact public description of a listing, saved for analysis."""
    res = item.get("residence") if isinstance(item.get("residence"), dict) else {}
    res_small = {k: v for k, v in res.items()
                 if k not in ("description", "medias", "parkings", "services")}
    for t in res_small.get("publicTransports") or []:
        if isinstance(t, dict):
            t.pop("id", None)
    return {
        "id": item.get("id"), "label": item.get("label"), "area": item.get("area"),
        "roomCount": item.get("roomCount"), "bedCount": item.get("bedCount"),
        "adaptedPmr": item.get("adaptedPmr"),
        "occupationModes": item.get("occupationModes"),
        "equipments": [e.get("label") if isinstance(e, dict) else e
                       for e in (item.get("equipments") or [])],
        "bookable": bool(periods), "periods": periods,
        "residence": res_small,
    }


def write_snapshot(tool_id, rows: list) -> None:
    data = {"tool": tool_id, "listings": sorted(rows, key=lambda r: r["id"] or 0)}
    text = json.dumps(data, ensure_ascii=False, indent=1, default=str)
    try:
        if SNAPSHOT_FILE.is_file() and SNAPSHOT_FILE.read_text(encoding="utf-8") == text:
            return
        SNAPSHOT_FILE.write_text(text, encoding="utf-8")
    except OSError as e:
        log(f"Snapshot write failed: {e}")


def france_total(tool: dict) -> int:
    """Nombre total d'annonces en France métropolitaine pour cette phase
    (sert à vérifier que la recherche fonctionne vraiment)."""
    body = search_payload(tool, 1)
    body["pageSize"] = 1
    body["location"] = [{"lon": -5.5, "lat": 51.2}, {"lon": 9.8, "lat": 41.2}]
    r = requests.post(SEARCH_URL.format(tool_id=tool["id"]), json=body,
                      headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    total = (r.json().get("results") or {}).get("total", 0)
    if isinstance(total, dict):
        total = total.get("value", 0)
    return int(total or 0)


def describe(item: dict) -> tuple[str, str, float | None, str]:
    """(nom, résidence, loyer_min_euros, loyer_texte)"""
    name = item.get("label") or "Logement"
    res = item.get("residence")
    residence = (res.get("label") if isinstance(res, dict) else None) or "?"
    rent_min, rent_txt = None, "?"
    try:
        modes = item.get("occupationModes") or []
        rent = (modes[0].get("rent") or {}) if modes and isinstance(modes[0], dict) else {}
        if rent.get("min") is not None:
            lo = rent["min"] / 100
            hi = (rent.get("max") if rent.get("max") is not None else rent["min"]) / 100
            rent_min = lo
            rent_txt = f"{lo:.0f} €" if lo == hi else f"{lo:.0f}–{hi:.0f} €"
        elif item.get("rentRange"):
            rr = item["rentRange"]
            rent_min = float(rr[0])
            rent_txt = f"{rr[0]:.0f} €" if rr[0] == rr[-1] else f"{rr[0]:.0f}–{rr[-1]:.0f} €"
    except (TypeError, ValueError, IndexError, KeyError):
        pass
    return str(name), str(residence), rent_min, rent_txt


def _modes(item: dict) -> list[str]:
    return [m.get("type") for m in (item.get("occupationModes") or []) if isinstance(m, dict)]


def distance_km(item: dict) -> float | None:
    import math
    loc = (item.get("residence") or {}).get("location") or {}
    try:
        lat, lon = float(loc["lat"]), float(loc["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    dy = (lat - STUDY_LAT) * 111.2
    dx = (lon - STUDY_LON) * 111.2 * math.cos(math.radians(STUDY_LAT))
    return math.hypot(dx, dy)


def room_kind(item: dict) -> str:
    """Type lisible : Studio, Chambre confort, Chambre simple, T1 bis, T2…"""
    import re
    label = (item.get("label") or "").upper()
    equip = {(e.get("label") if isinstance(e, dict) else str(e)) or ""
             for e in (item.get("equipments") or [])}
    if re.search(r"\bT2\b", label):
        return "T2 (2 pièces)"
    if re.search(r"\bT1 ?BIS\b", label):
        return "T1 bis (grand studio)"
    if "STUDIO" in label or re.search(r"\bT1\b", label) or "Evier + plaque" in equip:
        return "Studio (kitchenette, WC/douche)"
    if "CONFORT" in label or {"WC", "Douche"} <= equip:
        return "Chambre confort (WC/douche privés, cuisine commune)"
    return "Chambre simple (sanitaires + cuisine communs)"


def score(item: dict) -> tuple[int, str, str]:
    """(score 0-100, étoiles, ligne trajet). Plus c'est haut, mieux c'est."""
    d = distance_km(item)
    _, _, rent, _ = describe(item)
    kind = room_kind(item)
    s = 0
    if d is None:
        s += 20; trip = "distance inconnue"
    else:
        walk = round(d * 13)  # ~4,6 km/h
        trip = f"{d:.1f} km du {STUDY_NAME} (~{walk} min à pied)".replace(".", ",", 1)
        s += 50 if d <= 0.8 else 32 if d <= 2.2 else 22 if d <= 3.2 else 10
    if rent is not None:
        s += 30 if rent <= 300 else 22 if rent <= 400 else 12 if rent <= 480 else 5
    s += {"Studio": 20, "T1 bis": 15, "Chambre confort": 12, "T2": 8}.get(kind.split(" (")[0], 6)
    if "alone" not in _modes(item):
        s -= 30  # colocation : on partage avec quelqu'un
    stars = "⭐⭐⭐ TOP" if s >= 75 else "⭐⭐ BON" if s >= 55 else "⭐ OK"
    return s, stars, trip


def wanted(item: dict) -> bool:
    modes = _modes(item)
    if EXCLUDE_PMR and item.get("adaptedPmr"):
        return False
    if EXCLUDE_COUPLE_ONLY and modes and set(modes) <= {"couple"}:
        return False
    _, residence, rent_min, _ = describe(item)
    if RESIDENCE_FILTER.strip():
        keys = [_norm(k) for k in RESIDENCE_FILTER.split(",") if k.strip()]
        if not any(k in _norm(residence) for k in keys):
            return False
    if MAX_RENT_EUR > 0 and rent_min is not None and rent_min > MAX_RENT_EUR:
        return False
    return True


# ============================== CYCLE ========================================
def paris_now() -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Europe/Paris"))


def in_fast_window(t: datetime) -> bool:
    return t.weekday() in FAST_WEEKDAYS and FAST_HOURS[0] <= t.hour < FAST_HOURS[1]


def minutes_since(iso: str | None) -> float:
    t = _parse_dt(iso)
    if t is None:
        return float("inf")
    return (datetime.now(timezone.utc) - t).total_seconds() / 60


def current_tools(state: dict) -> list[dict]:
    """Active phases, re-read from the site at most every TOOLS_CACHE_MIN."""
    cache = state.get("tools_cache") or {}
    if cache.get("tools") is not None and minutes_since(cache.get("t")) < TOOLS_CACHE_MIN:
        now = datetime.now(timezone.utc)
        return [t for t in cache["tools"]
                if not (_parse_dt(t.get("endDate")) and now >= _parse_dt(t.get("endDate")))]
    tools = active_tools()
    state["tools_cache"] = {
        "t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tools": [{k: t.get(k) for k in ("id", "name", "mechanism", "startDate", "endDate")}
                  for t in tools],
    }
    return tools


def run_cycle(state: dict) -> dict:
    try:
        tools = current_tools(state)
    except (requests.RequestException, ValueError) as e:
        log(f"Cannot read phases ({e}); retry next cycle.")
        return state

    ids_now = sorted(t["id"] for t in tools)
    if ids_now != state.get("tool_ids"):
        if state.get("initialised"):  # do not notify on the very first run
            if tools:
                names = ", ".join(t.get("name", str(t["id"])) for t in tools)
                notify("CROUS : nouvelle phase ouverte", f"Phase(s) active(s) : {names}",
                       link=BASE + "/", priority=5, tags=["rotating_light"])
            else:
                notify("CROUS : plus aucune phase ouverte",
                       "Le portail est fermé. Je continue de surveiller et je te "
                       "préviens dès qu'une phase rouvre. Passe aux résidences en direct.",
                       priority=4, tags=["warning"])
        state["tool_ids"] = ids_now
        log(f"Active phases: {[(t['id'], t.get('name')) for t in tools] or 'none'}")

    seen: dict = state.setdefault("seen", {})
    for tool in tools:
        key = str(tool["id"])
        try:
            items = fetch_listings(tool)
        except (requests.RequestException, ValueError) as e:
            log(f"Search failed for phase {key}: {e}")
            continue
        current = {str(i.get("id")) for i in items if i.get("id") is not None}
        if key not in seen:
            seen[key] = sorted(current)
            log(f"Phase {key}: {len(current)} listing(s) already online, recorded silently.")
            if state.get("initialised"):
                # new phase: everything in it is new to us -> notify the wanted ones
                current_new = current
            else:
                continue
        else:
            current_new = current - set(seen[key])
        # Alert only when a room is really bookable (new listing OR a taken
        # room that frees up again). If the check fails, fall back to the
        # old "new listing" alert so nothing is missed.
        bookable_state: dict = state.setdefault("bookable", {})
        before = set(bookable_state.get(key, []))
        bookable_now = set()
        fresh: list = []
        alerts = 0
        rows = []
        events = state.setdefault("events", [])
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        sweeps: dict = state.setdefault("last_sweep", {})
        interval = FAST_SWEEP_MIN if in_fast_window(paris_now()) else SLOW_SWEEP_MIN
        full = minutes_since(sweeps.get(key)) >= interval
        checked = 0
        for item in items:
            iid = str(item.get("id"))
            if not wanted(item):
                continue
            if not full and iid not in current_new:
                if iid in before:          # keep last known status until next sweep
                    bookable_now.add(iid)
                continue
            periods = free_periods(key, item)
            checked += 1
            rows.append(snapshot_row(item, periods))
            if iid in current_new:
                events.append({"t": now_iso, "id": iid, "e": "listed",
                               "bookable": bool(periods)})
            elif periods and iid not in before:
                events.append({"t": now_iso, "id": iid, "e": "freed"})
            name, residence, _, rent = describe(item)
            link = LISTING_URL.format(tool_id=key, item_id=item.get("id"))
            if periods is None:
                if iid in current_new:
                    log(f"NEW (availability unknown) -> {name} | {residence} | {rent}")
                    notify(f"Nouvelle annonce CROUS : {residence}",
                           f"{name}\nLoyer : {rent}\nDisponibilité non vérifiée : ouvre vite.",
                           link=link, priority=4, tags=["house"])
                    alerts += 1
                continue
            if periods:
                bookable_now.add(iid)
                if iid not in before:
                    fresh.append((item, periods))
            elif iid in current_new:
                log(f"New listing already taken -> {name} | {residence}")
        # Best first, so the top choice is the first notification you see.
        fresh.sort(key=lambda x: -score(x[0])[0])
        for rank, (item, periods) in enumerate(fresh, 1):
            name, residence, _, rent = describe(item)
            s, stars, trip = score(item)
            link = LISTING_URL.format(tool_id=key, item_id=item.get("id"))
            p = periods[0]
            fmt = lambda v: "/".join(reversed(str(v)[:10].split("-")))
            area = item.get("area") or {}
            size = f" · {area.get('min'):g} m²" if area.get("min") else ""
            shared = "" if "alone" in _modes(item) else " · ⚠ colocation (logement partagé)"
            rank_txt = f"Choix n°{rank} sur {len(fresh)}" if len(fresh) > 1 else "Seul logement libre"
            short = re.sub(r"^(RESIDENCE|CITE U\.|CITE)\s+", "", residence.upper()).title()
            if s < MIN_ALERT_SCORE:
                log(f"Bookable but below alert level ({s} < {MIN_ALERT_SCORE}) -> {name} | {residence} | {rent}")
                continue
            advice, worth = switch_advice(describe(item)[2], distance_km(item),
                                          shared="alone" not in _modes(item))
            action = (f"👉 {rank_txt} — réserve tout de suite" if not advice
                      else f"👉 {advice}")
            log(f"BOOKABLE -> {stars} {s} | {name} | {residence} | {rent} | {trip} | {link} | worth={worth}")
            notify(f"{'💡 ' if advice and worth else ''}{stars} · {short} · {rent}",
                   f"{room_kind(item)}{size}{shared}\n📍 {trip}\n📅 Libre du {fmt(p.get('arrivalDate'))} "
                   f"au {fmt(p.get('departureDate'))}\n{action}",
                   link=link, priority=(5 if s >= 55 else 4) if worth else 2, tags=["house"])
            alerts += 1
        bookable_state[key] = sorted(bookable_now)
        state["events"] = events[-1000:]
        if full:
            sweeps[key] = now_iso
            write_snapshot(key, rows)
        mode = f"full sweep ({checked} rooms checked)" if full else f"quick ({checked} new checked)"
        if not alerts:
            log(f"Phase {key}: {len(current)} listed, {len(bookable_now)} bookable, nothing new — {mode}.")
        seen[key] = sorted(set(seen[key]) | current)

        # Self-check, once per phase: does the search find anything in France?
        diag = state.setdefault("diag_sent", [])
        if key not in diag:
            try:
                fr = france_total(tool)
            except (requests.RequestException, ValueError) as e:
                log(f"France-wide check failed: {e}")
            else:
                log(f"Phase {key}: {fr} listing(s) France-wide, {len(current)} in Caen.")
                if fr > 0:
                    msg = (f"La recherche fonctionne : {fr} logement(s) en ligne en France, "
                           f"{len(current)} à Caen/Hérouville pour l'instant.")
                else:
                    msg = ("0 logement en ligne dans toute la France pour cette phase. "
                           "Soit rien n'est encore publié, soit la recherche ne marche plus : "
                           "vérifie le site à la main.")
                notify("CROUS : vérification de la recherche", msg,
                       link=BASE + "/", priority=3, tags=["mag"])
                diag.append(key)

    if not state.get("initialised"):
        state["initialised"] = True
        notify("Surveillance CROUS Caen active",
               f"Phases suivies : {ids_now or 'aucune pour l’instant'}. "
               "Je te préviens dès qu'un logement apparaît.",
               link=BASE + "/", tags=["white_check_mark"])
    return state


def main() -> None:
    log("=== CROUS Caen watcher ===")
    if "CHANGE-ME" in NTFY_TOPIC and not DRY_RUN:
        log("WARNING: set NTFY_TOPIC to your own secret topic first.")
    state = load_state()
    while True:
        try:
            state = run_cycle(state)
            save_state(state)
        except Exception as e:  # never crash the loop
            log(f"Unexpected error: {type(e).__name__}: {e}")
        if RUN_ONCE:
            return
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped.")
        sys.exit(0)
