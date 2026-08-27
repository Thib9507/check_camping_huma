#!/usr/bin/env python3
"""
Surveille les billets CAMPING en revente sur le site officiel SeeTickets de
la Fête de l'Humanité et envoie une notification push (via ntfy.sh) dès
qu'au moins une place est mise en vente.

Pourquoi on n'analyse pas la page HTML :
La liste des offres n'est pas dans le HTML servi par le serveur. La page ne
contient qu'un composant JS (<post-events-pagination>) qui appelle ensuite
l'API. Hasher le HTML ne verrait donc jamais l'apparition d'une offre.
On interroge directement cette API, qui expose `nbTicket` : le nombre de
billets en revente pour une catégorie. C'est le champ dont le site lui-même
se sert pour afficher soit un lien cliquable (nbTicket > 0), soit une cloche
"créer une alerte" (nbTicket == 0).

Pourquoi une boucle interne :
Le cron de GitHub Actions ne descend pas sous 5 minutes, et les runs
planifiés sont en pratique retardés bien au-delà. Un seul run qui interroge
l'API en boucle donne une cadence réelle de l'ordre de la minute, sans
dépendre de la ponctualité du planificateur.

Anti-spam : on ne notifie qu'au passage de 0 (ou état inconnu) vers > 0.
Tant que des places restent en ligne, on reste silencieux ; on re-notifiera
si tout repart à 0 puis qu'une nouvelle place apparaît. Le compteur est
conservé entre les runs dans `last_count.txt` via le cache GitHub Actions.

Variables d'environnement :
- NTFY_TOPIC   : nom du topic ntfy.sh (obligatoire pour recevoir la notif)
- CATEGORY_ID  : id de la catégorie surveillée (défaut : 8137 = CAMPING)
- POLL_SECONDS : intervalle entre deux vérifications (défaut : 60)
- LOOP_MINUTES : durée totale de la boucle (défaut : 15, 0 = un seul passage)
"""

import os
import random
import re
import sys
import time
import unicodedata

import requests

CATEGORY_ID = os.environ.get("CATEGORY_ID", "8137")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "60"))
LOOP_MINUTES = float(os.environ.get("LOOP_MINUTES", "15"))

API_URL = f"https://resell.seetickets.com/api/categories/{CATEGORY_ID}"
SITE_URL = "https://resell.seetickets.com/fete-de-lhumanite-2026/"
STATE_FILE = "last_count.txt"

# Codes qui signifient "tu insistes trop" : on recule au lieu de réessayer
# tout de suite. C'est ça qui évite le bannissement, bien plus que le choix
# de l'intervalle de polling.
THROTTLE_CODES = {403, 429, 503}
MAX_CONSECUTIVE_BLOCKS = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Une seule connexion TCP réutilisée pour toute la boucle : plus rapide, et
# une poignée de main TLS en moins par vérification côté serveur.
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def slugify(value: str) -> str:
    """Reproduit le slug utilisé dans les URLs du site (accents retirés)."""
    ascii_value = (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", ascii_value)).strip("-")


def fetch_category() -> dict:
    resp = SESSION.get(API_URL, timeout=20)
    resp.raise_for_status()
    return resp.json()


def category_name(category: dict) -> str:
    """Le nom est renvoyé par locale : {"en": "... - CAMPING"}."""
    names = category.get("name") or {}
    if isinstance(names, str):
        return names
    return next(iter(names.values()), "")


def check_is_camping(name: str):
    """
    Garde-fou : si l'id surveillé pointait un jour vers autre chose (les ids
    sont réattribués d'une édition à l'autre), on préfère échouer bruyamment
    plutôt que surveiller silencieusement la mauvaise catégorie.
    "parking" est exclu explicitement à cause de PARKING CAMPEURS et
    PARKING CAMPING-CAR, qui contiennent eux aussi "camping".
    """
    lowered = slugify(name)
    if "camping" not in lowered or "parking" in lowered:
        print(
            f"La catégorie {CATEGORY_ID} s'appelle {name!r} : ce n'est pas le "
            f"camping. Vérifie CATEGORY_ID.",
            file=sys.stderr,
        )
        sys.exit(1)


def build_ticket_url(name: str) -> str:
    """
    Lien direct vers la catégorie. Cette page renvoie 404 tant qu'aucune
    place n'est en vente, donc on vérifie avant de l'envoyer et on retombe
    sur l'accueil du site si elle n'est pas encore ouverte.
    """
    url = f"{SITE_URL}category/{CATEGORY_ID}/{slugify(name)}"
    try:
        if SESSION.get(url, timeout=10).status_code == 200:
            return url
    except requests.RequestException:
        pass
    return SITE_URL


def send_notification(count: int, url: str):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC non défini, notification non envoyée.")
        return

    places = "place" if count == 1 else "places"
    message = f"{count} {places} de camping en revente. Fonce, ça part vite !"
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": f"🏕️ Camping dispo ({count})".encode("utf-8"),
            "Click": url,
            "Priority": "high",
            "Tags": "tent",
        },
        timeout=10,
    )


def load_previous_count() -> int | None:
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        content = f.read().strip()
    try:
        return int(content)
    except ValueError:
        # Fichier de cache corrompu ou hérité d'une version précédente :
        # on repart de zéro plutôt que de planter.
        return None


def save_count(count: int):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(str(count))


def check_once(previous: int | None) -> int:
    """Une vérification. Renvoie le compteur observé."""
    category = fetch_category()
    name = category_name(category)
    check_is_camping(name)

    count = category.get("nbTicket") or 0
    stamp = time.strftime("%H:%M:%S")

    if count > 0 and not previous:
        # `not previous` couvre 0 et None (premier run ou cache expiré) : on
        # préfère une notif de trop qu'une place manquée.
        print(f"[{stamp}] {count} place(s) ! Envoi de la notification.")
        send_notification(count, build_ticket_url(name))
    elif count > 0:
        print(f"[{stamp}] {count} place(s), déjà notifié.")
    else:
        print(f"[{stamp}] aucune place.")

    save_count(count)
    return count


def main():
    previous = load_previous_count()
    print(
        f"Catégorie {CATEGORY_ID} | compteur précédent : {previous} | "
        f"1 vérification toutes les {POLL_SECONDS:.0f}s pendant "
        f"{LOOP_MINUTES:.0f} min"
    )

    deadline = time.monotonic() + LOOP_MINUTES * 60
    blocks = 0

    while True:
        try:
            previous = check_once(previous)
            blocks = 0
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status in THROTTLE_CODES:
                blocks += 1
                if blocks >= MAX_CONSECUTIVE_BLOCKS:
                    print(
                        f"{blocks} refus consécutifs ({status}) : on arrête "
                        f"là pour ne pas insister.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                # Recul exponentiel : 2 min, puis 4 min.
                backoff = 120 * 2 ** (blocks - 1)
                print(
                    f"Refus {status}, pause de {backoff}s avant de réessayer.",
                    file=sys.stderr,
                )
                time.sleep(backoff)
                continue
            # 404/410 : l'API ne connaît plus cette catégorie, le script ne
            # peut plus rien détecter. On fait échouer le job pour que le run
            # passe au rouge plutôt que de rester vert sans rien faire.
            print(f"Catégorie {CATEGORY_ID} injoignable ({e}).", file=sys.stderr)
            sys.exit(1)
        except (requests.RequestException, ValueError) as e:
            # Timeout, coupure réseau, JSON invalide : sans gravité, on
            # retentera au prochain passage.
            print(f"Erreur ponctuelle : {e}", file=sys.stderr)

        # On ne démarre pas une attente qui dépasserait la fin prévue.
        if time.monotonic() + POLL_SECONDS > deadline:
            break
        # Un peu de jitter pour ne pas taper pile sur la seconde ronde à
        # chaque fois, comme le ferait un script.
        time.sleep(POLL_SECONDS + random.uniform(0, 5))

    print("Fin de la boucle.")


if __name__ == "__main__":
    main()
