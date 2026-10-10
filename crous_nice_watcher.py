#!/usr/bin/env python3
"""
Surveille trouverunlogement.lescrous.fr et envoie une notification
(Telegram et/ou WhatsApp via CallMeBot) dès qu'un nouveau logement apparaît
dans les Alpes-Maritimes (codes postaux 06xxx : Nice, Sophia, Antibes...).

Le filtre "bounds" de la carte ne marche pas côté serveur : on lit donc toute
la liste France (page par page) et on filtre sur le code postal.

Dépendance : pip install requests
"""

import html as htmllib
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
# URL de recherche CROUS (les paramètres comme ?bounds=... sont ignorés).
SEARCH_URL = os.getenv(
    "CROUS_SEARCH_URL", "https://trouverunlogement.lescrous.fr/tools/47/search"
)

# Préfixes de code postal surveillés, séparés par des virgules.
# "06" = tout le département (Nice, Sophia, Antibes, Cagnes, Cannes...).
# Exemple plus restreint : "060,061,062,063,06560,06410,06600,06700,06800"
POSTCODES = [p.strip() for p in os.getenv("POSTCODES", "06").split(",") if p.strip()]

# Telegram : token donné par @BotFather + ton chat_id
TELEGRAM_TOKEN = os.getenv("TG_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TG_CHAT_ID", "")

# (Optionnel) WhatsApp via CallMeBot : numéro sans "+" et apikey.
WHATSAPP_PHONE = os.getenv("WA_PHONE", "")
CALLMEBOT_APIKEY = os.getenv("WA_APIKEY", "")

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL", "300"))  # 5 min
STATE_FILE = Path(os.getenv("STATE_FILE", "crous_seen.json"))
MAX_PAGES = 40
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
POSTCODE_RE = re.compile(r"\b(\d{5})\b")
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def base_search_url() -> str:
    p = urllib.parse.urlparse(SEARCH_URL)
    return f"{p.scheme}://{p.netloc}{p.path}"


def accommodation_url(acc_id: str) -> str:
    return re.sub(r"/search/?$", "", base_search_url()) + f"/accommodations/{acc_id}"


def load_seen() -> set[str]:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return set()


def save_seen(ids: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(ids)))


def to_text(fragment: str) -> str:
    return SPACE_RE.sub(" ", htmllib.unescape(TAG_RE.sub(" ", fragment))).strip()


ANCHOR_TEXT_RE = re.compile(r"<a\b[^>]*>(.*?)</a>", re.S)


def parse_page(page_html: str) -> dict[str, str]:
    """Retourne {id_logement: texte de la carte} pour une page de résultats.
    Le texte commence par le nom de la résidence, suivi de " ¦ "."""
    first_pos: dict[str, int] = {}
    for m in ACCOMMODATION_RE.finditer(page_html):
        first_pos.setdefault(m.group(1), page_html.rfind("<", 0, m.start()))
    ordered = sorted(first_pos.items(), key=lambda kv: kv[1])
    cards = {}
    for i, (acc_id, start) in enumerate(ordered):
        end = ordered[i + 1][1] if i + 1 < len(ordered) else start + 6000
        chunk = page_html[start:end]
        name = ""
        for a in ANCHOR_TEXT_RE.findall(chunk):
            name = to_text(a)
            if name:
                break
        cards[acc_id] = f"{name} ¦ {to_text(chunk)}"
    return cards


def card_postcode(text: str) -> str | None:
    for code in POSTCODE_RE.findall(text):
        if code[:2].isdigit() and 1 <= int(code[:2]) <= 98:
            return code
    return None


def card_summary(text: str, postcode: str) -> str:
    name = text.split(" ¦ ", 1)[0][:60]
    city_m = re.search(rf"{postcode}\s+([A-Za-zÀ-ÿ'\- ]{{2,40}})", text)
    city = city_m.group(1).strip() if city_m else ""
    price_m = re.search(r"(\d+(?:[.,]\d+)?)\s*€", text)
    price = f"{price_m.group(1)} €" if price_m else ""
    parts = [p for p in (name, f"{postcode} {city}".strip(), price) if p]
    return " · ".join(parts)


def fetch_all() -> tuple[dict[str, str], int | None]:
    """Parcourt toutes les pages. Retourne ({id: texte}, nombre total France)."""
    all_cards: dict[str, str] = {}
    total = None
    for page in range(1, MAX_PAGES + 1):
        r = requests.get(
            base_search_url(), params={"page": page}, headers=HEADERS, timeout=30
        )
        r.raise_for_status()
        page_html = r.text
        if page == 1:
            if "Aucun logement trouvé" in page_html:
                return {}, 0
            m = COUNT_RE.search(to_text(page_html))
            total = int(m.group(1)) if m else None
        cards = parse_page(page_html)
        new = {k: v for k, v in cards.items() if k not in all_cards}
        if not new:
            break
        all_cards.update(new)
        if total is not None and len(all_cards) >= total:
            break
        time.sleep(1)
    return all_cards, total


def send_telegram(text: str) -> None:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=30,
        )
        log("Telegram envoyé." if r.ok else f"Erreur Telegram {r.status_code}: {r.text[:200]}")
    except requests.RequestException as e:
        log(f"Erreur Telegram : {e}")


def send_whatsapp(text: str) -> None:
    if not (WHATSAPP_PHONE and CALLMEBOT_APIKEY):
        return
    try:
        r = requests.get(
            "https://api.callmebot.com/whatsapp.php",
            params={"phone": WHATSAPP_PHONE, "text": text, "apikey": CALLMEBOT_APIKEY},
            timeout=30,
        )
        log("WhatsApp envoyé." if r.ok else f"Erreur CallMeBot {r.status_code}: {r.text[:200]}")
    except requests.RequestException as e:
        log(f"Erreur WhatsApp : {e}")


def notify(text: str) -> None:
    send_telegram(text)
    send_whatsapp(text)


def check_once(seen: set[str]) -> set[str]:
    cards, total = fetch_all()

    matches = {}
    for acc_id, text in cards.items():
        cp = card_postcode(text)
        if cp and any(cp.startswith(p) for p in POSTCODES):
            matches[acc_id] = card_summary(text, cp)

    log(f"{total if total is not None else '?'} logements en France "
        f"({len(cards)} lus), {len(matches)} dans la zone {','.join(POSTCODES)}.")

    if total and not cards:
        log("⚠️  Le site annonce des logements mais aucun n'a pu être lu (HTML changé ?).")

    new_ids = set(matches) - seen
    if new_ids:
        lines = [f"• {matches[i]}\n  {accommodation_url(i)}" for i in sorted(new_ids)]
        msg = (
            f"🏠 CROUS : {len(new_ids)} nouveau(x) logement(s) dans ta zone !\n\n"
            + "\n\n".join(lines)
            + f"\n\nRéserve vite : {base_search_url()}"
        )
        log(msg.replace("\n", " | "))
        notify(msg)

    # On garde uniquement ce qui est encore affiché : si un logement
    # disparaît puis réapparaît, tu seras de nouveau notifié.
    return set(matches)


def main() -> None:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID) and not (WHATSAPP_PHONE and CALLMEBOT_APIKEY):
        sys.exit("⚠️  Configure Telegram (TG_TOKEN, TG_CHAT_ID) et/ou WhatsApp (WA_PHONE, WA_APIKEY).")

    if "--test" in sys.argv:
        notify("✅ Test : le watcher CROUS Nice fonctionne.")
        return

    if "--dry-run" in sys.argv:
        # Affiche ce que le script voit, sans rien envoyer
        cards, total = fetch_all()
        log(f"{total} logements annoncés, {len(cards)} lus :")
        for acc_id, text in cards.items():
            cp = card_postcode(text) or "?????"
            print(f"  {acc_id}  {card_summary(text, cp)}")
        return

    once = "--once" in sys.argv
    seen = load_seen()
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
