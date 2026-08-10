#!/usr/bin/env python3
"""One-shot: usuwa WSZYSTKIE wiadomości SMS ze skrzynki odbiorczej.

⚠️ OPERACJA NIEODWRACALNA. Dla bezpieczeństwa domyślnie robi tylko dry-run
(pokazuje, ile wiadomości zostałoby usuniętych). Faktyczne kasowanie następuje
dopiero po podaniu flagi --yes.

Konfiguracja przez .env (obok skryptu) lub zmienne środowiskowe:
    ROUTER_USER, ROUTER_PASS, ROUTER_IP (wymagane)

Uruchomienie:
    python3 delete_all.py            # dry-run (nic nie kasuje)
    python3 delete_all.py --yes      # faktyczne usunięcie
"""

import os
import sys

from dotenv import load_dotenv
from huawei_lte_api.Client import Client
from huawei_lte_api.Connection import Connection
from huawei_lte_api.enums.sms import BoxTypeEnum

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

USER = os.environ.get("ROUTER_USER")
PASS = os.environ.get("ROUTER_PASS")
ROUTER_IP = os.environ.get("ROUTER_IP")


def build_url() -> str:
    if USER and PASS:
        return f"http://{USER}:{PASS}@{ROUTER_IP}/"
    return f"http://{ROUTER_IP}/"


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


def main() -> None:
    if not (USER and PASS and ROUTER_IP):
        raise SystemExit("Brak ROUTER_USER/ROUTER_PASS/ROUTER_IP – uzupełnij .env (operacje SMS wymagają logowania).")

    confirm = "--yes" in sys.argv[1:]

    with Connection(build_url()) as connection:
        client = Client(connection)

        messages = fetch_all_inbox(client)
        print(f"Skrzynka odbiorcza: {len(messages)} wiadomości.")

        if not messages:
            print("Nic do usunięcia.")
            return

        if not confirm:
            print(f"DRY-RUN: usunęłoby {len(messages)} wiadomości. "
                  "Uruchom z --yes, aby faktycznie skasować.")
            return

        ok = 0
        errors = []
        for i, msg in enumerate(messages, 1):
            index = msg.get("Index")
            try:
                client.sms.delete_sms(int(index))
                ok += 1
            except Exception as exc:  # noqa: BLE001 - pojedynczy błąd nie przerywa całości
                errors.append((index, str(exc)))
            if i % 25 == 0 or i == len(messages):
                print(f"  usunięto {i}/{len(messages)}...")

        after = client.sms.sms_count() or {}
        print(f"\nGotowe: usunięto {ok}/{len(messages)}.")
        if errors:
            print(f"Błędy ({len(errors)}): "
                  + ", ".join(f"idx {i} ({e})" for i, e in errors[:5])
                  + (" ..." if len(errors) > 5 else ""))
        print(f"Wiadomości w skrzynce teraz: {after.get('LocalInbox')}")


if __name__ == "__main__":
    main()
