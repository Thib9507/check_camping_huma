#!/usr/bin/env python3
"""
Surveille la page de revente SeeTickets de la Fête de l'Humanité et envoie
une notification push (via ntfy.sh) dès que le contenu de la page change
(signe potentiel qu'une offre est apparue).

Fonctionnement :
- On télécharge la page.
- On isole la zone de contenu utile (on retire les parties qui changent
  tout le temps sans rapport avec les billets : cookies, timestamps, etc.)
- On calcule une empreinte (hash) de ce contenu.
- On compare ce hash à celui de la dernière exécution (stocké dans un
  fichier `last_hash.txt`, conservé entre les runs via le cache GitHub Actions).
- Si le hash a changé -> on envoie une notif push avec un lien direct.
- On ajoute aussi une détection par mots-clés simples (ex: présence du mot
  "Ajouter" ou d'un prix en €) pour donner un indice dans la notif.

On surveille la page de l'événement plutôt qu'une catégorie précise : les
URLs de catégorie (/category/<id>/...) n'existent que quand des offres sont
en ligne, et renvoient 404 le reste du temps.

Variables d'environnement attendues :
- NTFY_TOPIC : le nom de ton topic ntfy.sh (obligatoire)
- TARGET_URL : l'URL à surveiller (optionnel, valeur par défaut ci-dessous)
"""

import hashlib
import os
import re
import sys

import requests
from bs4 import BeautifulSoup

DEFAULT_URL = "https://resell.seetickets.com/fete-de-lhumanite-2026/"

TARGET_URL = os.environ.get("TARGET_URL", DEFAULT_URL)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
HASH_FILE = "last_hash.txt"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def fetch_page(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def extract_relevant_text(html: str) -> str:
    """Isole le contenu qui nous intéresse et enlève le bruit."""
    soup = BeautifulSoup(html, "html.parser")

    # On enlève les balises qui ne portent jamais d'info utile
    for tag in soup(["script", "style", "noscript", "svg", "img"]):
        tag.decompose()

    text = soup.get_text(separator=" ", strip=True)

    # On enlève les nombres qui pourraient être des timestamps/compteurs
    # variables sans rapport avec la dispo (à ajuster si trop de faux positifs)
    text = re.sub(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?", "", text)

    return text


def looks_like_ticket_available(text: str) -> bool:
    """Heuristique simple : indices qu'une offre est présente."""
    lowered = text.lower()
    positive_signals = ["ajouter au panier", "acheter", "quantité"]
    negative_signals = ["aucun billet", "aucune offre", "indisponible", "épuisé"]

    has_positive = any(s in lowered for s in positive_signals)
    has_negative = any(s in lowered for s in negative_signals)

    return has_positive and not has_negative


def send_notification(title: str, message: str, url: str):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC non défini, notification non envoyée.")
        return

    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": title.encode("utf-8"),
            "Click": url,
            "Priority": "high",
            "Tags": "ticket",
        },
        timeout=10,
    )


def load_previous_hash() -> str | None:
    if os.path.exists(HASH_FILE):
        with open(HASH_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return None


def save_hash(new_hash: str):
    with open(HASH_FILE, "w", encoding="utf-8") as f:
        f.write(new_hash)


def main():
    try:
        html = fetch_page(TARGET_URL)
    except requests.HTTPError as e:
        # Un 404/410 veut dire que l'URL surveillée n'existe plus : le script
        # ne peut plus rien détecter. On fait échouer le job pour que le run
        # apparaisse en rouge dans l'onglet Actions (et déclenche le mail
        # d'échec de GitHub) plutôt que de passer au vert sans rien faire.
        print(f"URL surveillée injoignable ({e}). Vérifie TARGET_URL.", file=sys.stderr)
        sys.exit(1)
    except requests.RequestException as e:
        # Timeout, coupure réseau, 5xx passager : on ne fait pas planter le job.
        print(f"Erreur réseau ponctuelle : {e}", file=sys.stderr)
        sys.exit(0)

    text = extract_relevant_text(html)
    current_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    previous_hash = load_previous_hash()

    if previous_hash is None:
        # Premier run : on enregistre juste l'état de référence
        print("Premier passage : enregistrement du hash de référence.")
        save_hash(current_hash)
        return

    if current_hash != previous_hash:
        print("Changement détecté sur la page !")
        available = looks_like_ticket_available(text)
        if available:
            title = "🎫 Billet(s) probablement disponible(s) !"
            message = "Un changement suggérant une offre a été détecté. Va vérifier vite !"
        else:
            title = "🔔 Changement détecté sur la page"
            message = "Le contenu de la page a changé (pas sûr à 100% que ce soit un billet). Va vérifier."

        send_notification(title, message, TARGET_URL)
        save_hash(current_hash)
    else:
        print("Aucun changement détecté.")


if __name__ == "__main__":
    main()
