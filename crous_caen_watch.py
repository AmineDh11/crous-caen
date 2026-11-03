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


def wanted(item: dict) -> bool:
    _, residence, rent_min, _ = describe(item)
    if RESIDENCE_FILTER.strip():
        keys = [_norm(k) for k in RESIDENCE_FILTER.split(",") if k.strip()]
        if not any(k in _norm(residence) for k in keys):
            return False
    if MAX_RENT_EUR > 0 and rent_min is not None and rent_min > MAX_RENT_EUR:
        return False
    return True


# ============================== CYCLE ========================================
def run_cycle(state: dict) -> dict:
    try:
        tools = active_tools()
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
        hits = [i for i in items if str(i.get("id")) in current_new and wanted(i)]
        for item in hits:
            name, residence, _, rent = describe(item)
            link = LISTING_URL.format(tool_id=key, item_id=item.get("id"))
            log(f"NEW -> {name} | {residence} | {rent} | {link}")
            notify(f"Logement CROUS libre : {residence}",
                   f"{name}\nLoyer : {rent}\nRéserve MAINTENANT (1er arrivé, 1er servi).",
                   link=link, priority=5, tags=["house", "rotating_light"])
        if not hits:
            log(f"Phase {key}: no new listing ({len(current)} online).")
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
