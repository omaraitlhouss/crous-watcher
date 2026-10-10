#!/usr/bin/env python3
"""
Surveille trouverunlogement.lescrous.fr et envoie une notification
(Telegram et/ou WhatsApp via CallMeBot) dès qu'un nouveau logement apparaît pour la recherche donnée.

Dépendance : pip install requests
"""

import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
# 1) Fais ta recherche "Nice" sur le site, puis copie l'URL de la barre d'adresse
#    (elle ressemble à https://trouverunlogement.lescrous.fr/tools/XX/search?bounds=...)
SEARCH_URL = os.getenv(
    "CROUS_SEARCH_URL",
    "https://trouverunlogement.lescrous.fr/tools/XX/search?bounds=7.1819535_43.7607635_7.323912_43.6454189",
)

# 2) Telegram : token donné par @BotFather + ton chat_id
TELEGRAM_TOKEN = os.getenv("TG_TOKEN", "XXXXXXX")
TELEGRAM_CHAT_ID = os.getenv("TG_CHAT_ID", "XXXXXXX")

# 3) (Optionnel) WhatsApp via CallMeBot : numéro sans "+" et apikey.
#    Laisse vide pour n'utiliser que Telegram.
WHATSAPP_PHONE = os.getenv("WA_PHONE", "")
CALLMEBOT_APIKEY = os.getenv("WA_APIKEY", "")

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL", "300"))  # 5 min
STATE_FILE = Path(os.getenv("STATE_FILE", "crous_seen.json"))
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
}

ACCOMMODATION_RE = re.compile(r"/tools/\d+/accommodations/(\d+)")
COUNT_RE = re.compile(r"(\d+)\s+logements?\s+trouvés?", re.IGNORECASE)


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_seen() -> set[str]:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return set()


def save_seen(ids: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(ids)))


def fetch_listings() -> tuple[set[str], int | None]:
    """Retourne (ids des logements trouvés sur la page, nombre annoncé)."""
    r = requests.get(SEARCH_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    html = r.text

    if "Aucun logement trouvé" in html:
        return set(), 0

    ids = set(ACCOMMODATION_RE.findall(html))
    m = COUNT_RE.search(html)
    count = int(m.group(1)) if m else (len(ids) or None)
    return ids, count


def send_whatsapp(text: str) -> None:
    if not (WHATSAPP_PHONE and CALLMEBOT_APIKEY):
        return
    try:
        r = requests.get(
            "https://api.callmebot.com/whatsapp.php",
            params={"phone": WHATSAPP_PHONE, "text": text, "apikey": CALLMEBOT_APIKEY},
            timeout=30,
        )
        if r.status_code != 200:
            log(f"Erreur CallMeBot {r.status_code}: {r.text[:200]}")
        else:
            log("WhatsApp envoyé.")
    except requests.RequestException as e:
        log(f"Erreur WhatsApp : {e}")


def notify(text: str) -> None:
    send_telegram(text)
    send_whatsapp(text)


def send_telegram(text: str) -> None:
    if "XXXX" in TELEGRAM_TOKEN:
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True},
        timeout=30,
    )
    if r.status_code != 200:
        log(f"Erreur Telegram {r.status_code}: {r.text[:200]}")
    else:
        log("Telegram envoyé.")


def base_url() -> str:
    p = urllib.parse.urlparse(SEARCH_URL)
    return f"{p.scheme}://{p.netloc}"


def check_once(seen: set[str]) -> set[str]:
    ids, count = fetch_listings()

    if not ids and not count:
        log("Aucun logement.")
        return seen

    new_ids = ids - seen
    if new_ids:
        links = "\n".join(
            f"{base_url()}{re.sub(r'/search.*', '', urllib.parse.urlparse(SEARCH_URL).path)}/accommodations/{i}"
            for i in sorted(new_ids)
        )
        msg = (
            f"🏠 CROUS Nice : {len(new_ids)} nouveau(x) logement(s) dispo "
            f"({count} au total)\n{links}\n\nRecherche : {SEARCH_URL}"
        )
        log(msg.replace("\n", " | "))
        notify(msg)
    elif count and not ids:
        # Le compteur indique des résultats mais on n'a pas su extraire d'ID
        # (changement de structure HTML) -> on prévient quand même une fois.
        if "__count_alert__" not in seen:
            notify(f"🏠 CROUS Nice : {count} logement(s) trouvé(s) !\n{SEARCH_URL}")
            ids = {"__count_alert__"}
    else:
        log(f"{count} logement(s), rien de nouveau.")

    # On garde uniquement ce qui est encore affiché : si un logement
    # disparaît puis réapparaît, tu seras de nouveau notifié.
    return ids


def main() -> None:
    if "/tools/XX/" in SEARCH_URL:
        sys.exit("⚠️  Configure CROUS_SEARCH_URL avant de lancer.")
    if "XXXX" in TELEGRAM_TOKEN and not (WHATSAPP_PHONE and CALLMEBOT_APIKEY):
        sys.exit("⚠️  Configure Telegram (TG_TOKEN, TG_CHAT_ID) et/ou WhatsApp (WA_PHONE, WA_APIKEY).")

    once = "--once" in sys.argv  # mode cron / GitHub Actions
    seen = load_seen()

    if "--test" in sys.argv:
        notify("✅ Test : le watcher CROUS Nice fonctionne.")
        return

    while True:
        try:
            seen = check_once(seen)
            save_seen(seen)
        except requests.RequestException as e:
            log(f"Erreur réseau : {e}")
        if once:
            break
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
