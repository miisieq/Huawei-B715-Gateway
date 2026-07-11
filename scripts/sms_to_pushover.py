#!/usr/bin/env python3
"""Wysyła nieprzeczytane SMS-y (od najstarszej) jako powiadomienia Pushover.

Dla każdej nieprzeczytanej wiadomości wysyła powiadomienie przez API Pushover.
Dopiero gdy Pushover potwierdzi poprawne odebranie (HTTP 200 + status=1),
skrypt oznacza daną wiadomość jako przeczytaną. Wiadomości, których nie uda się
wysłać lub potwierdzić, są pomijane (bez oznaczania jako przeczytane) — skrypt
przechodzi do następnej i próbuje ją wysłać. Pominięte pozostają nieprzeczytane
i zostaną ponowione przy kolejnym uruchomieniu (np. z crona).

Przeznaczony do uruchamiania z crona (na sukces bez nowych wiadomości nic nie wypisuje).

Konfiguracja przez .env (katalog projektu) lub zmienne środowiskowe:
    ROUTER_USER, ROUTER_PASS, ROUTER_IP (wymagane)
    PUSHOVER_TOKEN   - token aplikacji Pushover
    PUSHOVER_USER    - klucz użytkownika (lub grupy) Pushover

Uruchomienie:
    python3 scripts/sms_to_pushover.py            # z podsumowaniem na stdout
    python3 scripts/sms_to_pushover.py --quiet    # cicho: na sukces nic, tylko błędy na stderr
"""

import os
import sys
import time

import requests
from dotenv import load_dotenv
from huawei_lte_api.Client import Client
from huawei_lte_api.Connection import Connection
from huawei_lte_api.enums.sms import BoxTypeEnum

# .env leży w katalogu projektu (o poziom wyżej niż scripts/)
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

USER = os.environ.get("ROUTER_USER")
PASS = os.environ.get("ROUTER_PASS")
ROUTER_IP = os.environ.get("ROUTER_IP")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER")
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"

SMSTAT_UNREAD = "0"  # "0" = nieprzeczytana, "1" = przeczytana (B715s)


def build_url() -> str:
    return f"http://{USER}:{PASS}@{ROUTER_IP}/"


def fetch_all_inbox(client: Client) -> list:
    """Pobiera wszystkie wiadomości ze skrzynki odbiorczej (z paginacją)."""
    total = int((client.sms.sms_count() or {}).get("LocalInbox") or 0)
    per_page = 50
    collected = []
    page = 1
    while len(collected) < total:
        resp = client.sms.get_sms_list(
            page=page, box_type=BoxTypeEnum.LOCAL_INBOX, read_count=per_page
        )
        messages = (resp or {}).get("Messages") or {}
        batch = messages.get("Message") if isinstance(messages, dict) else None
        if not batch:
            break
        if isinstance(batch, dict):
            batch = [batch]
        collected.extend(batch)
        page += 1
    return collected


def to_epoch(date_str):
    """Konwertuje datę z routera ('YYYY-MM-DD HH:MM:SS', czas lokalny) na uniksowy
    timestamp. Zwraca None, gdy pole jest puste lub nie da się sparsować."""
    if not date_str:
        return None
    try:
        return int(time.mktime(time.strptime(date_str, "%Y-%m-%d %H:%M:%S")))
    except (ValueError, OverflowError):
        return None


def send_pushover(title: str, message: str, timestamp=None, timeout: int = 15) -> bool:
    """Wysyła powiadomienie. Zwraca True tylko gdy Pushover potwierdzi (HTTP 200 + status=1)."""
    data = {
        "token": PUSHOVER_TOKEN,
        "user": PUSHOVER_USER,
        "title": title[:250],      # limit tytułu Pushover
        "message": message[:1024],  # limit treści Pushover
    }
    if timestamp is not None:
        # nadpisz czas powiadomienia datą odbioru SMS-a (z API routera)
        data["timestamp"] = timestamp
    resp = requests.post(PUSHOVER_URL, data=data, timeout=timeout)
    if resp.status_code != 200:
        return False
    try:
        return resp.json().get("status") == 1
    except ValueError:  # odpowiedź nie jest JSON-em
        return False


def main() -> None:
    # --quiet: na sukces nic nie wypisuj (tylko błędy na stderr) — przydatne w cronie
    quiet = "--quiet" in sys.argv[1:]

    # sprawdź komplet konfiguracji
    missing = [
        name for name, val in (
            ("ROUTER_USER", USER), ("ROUTER_PASS", PASS), ("ROUTER_IP", ROUTER_IP),
            ("PUSHOVER_TOKEN", PUSHOVER_TOKEN), ("PUSHOVER_USER", PUSHOVER_USER),
        ) if not val
    ]
    if missing:
        raise SystemExit("Brak wymaganych zmiennych: " + ", ".join(missing))

    with Connection(build_url()) as connection:
        client = Client(connection)

        unread = [m for m in fetch_all_inbox(client) if m.get("Smstat") == SMSTAT_UNREAD]
        # od najstarszej: sortuj po dacie rosnąco (fallback po Index)
        unread.sort(key=lambda m: (m.get("Date") or "", int(m.get("Index") or 0)))

        if not unread:
            return  # brak nieprzeczytanych — cicho kończ (przyjazne dla crona)

        sent = 0
        failed = 0
        for msg in unread:
            index = int(msg.get("Index"))
            phone = msg.get("Phone") or "?"
            date = msg.get("Date") or ""
            content = msg.get("Content") or ""
            title = f"SMS od {phone}"
            body = content  # data trafia do pola timestamp, nie do treści

            # 1) wyślij powiadomienie (timestamp = data odbioru SMS-a z API routera)
            try:
                confirmed = send_pushover(title, body, timestamp=to_epoch(date))
            except requests.RequestException as exc:
                print(f"Pushover — błąd sieci przy Index {index}: {exc} — pomijam.", file=sys.stderr)
                failed += 1
                continue  # próbuj kolejnej; ta zostanie ponowiona przy następnym uruchomieniu
            if not confirmed:
                print(f"Pushover nie potwierdził Index {index} — pomijam.", file=sys.stderr)
                failed += 1
                continue

            # 2) dopiero po potwierdzeniu — oznacz jako przeczytaną
            try:
                client.sms.set_read(index)
            except Exception as exc:  # noqa: BLE001
                print(f"Wysłano powiadomienie, ale nie udało się oznaczyć Index {index} "
                      f"jako przeczytany: {exc} — pomijam.", file=sys.stderr)
                failed += 1
                continue
            sent += 1

        if (sent or failed) and not quiet:
            print(f"Wysłano i oznaczono: {sent}; nieudane (do ponowienia): {failed}.")


if __name__ == "__main__":
    main()
