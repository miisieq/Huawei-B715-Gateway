#!/usr/bin/env python3
"""One-shot: oznacza WSZYSTKIE nieprzeczytane SMS-y jako przeczytane.

Pobiera całą skrzynkę odbiorczą (z paginacją), wybiera nieprzeczytane
(Smstat == "0") i wywołuje set_read() dla każdego. Na końcu weryfikuje licznik.

Konfiguracja przez .env (obok skryptu) lub zmienne środowiskowe:
    ROUTER_USER, ROUTER_PASS, ROUTER_IP (wymagane)

Uruchomienie:
    python3 mark_all_read.py
"""

import os

from dotenv import load_dotenv
from huawei_lte_api.Client import Client
from huawei_lte_api.Connection import Connection
from huawei_lte_api.enums.sms import BoxTypeEnum

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

USER = os.environ.get("ROUTER_USER")
PASS = os.environ.get("ROUTER_PASS")
ROUTER_IP = os.environ.get("ROUTER_IP")

SMSTAT_UNREAD = "0"  # "0" = nieprzeczytana, "1" = przeczytana (B715s)


def build_url() -> str:
    if USER and PASS:
        return f"http://{USER}:{PASS}@{ROUTER_IP}/"
    return f"http://{ROUTER_IP}/"


def fetch_all_inbox(client: Client) -> list:
    """Pobiera wszystkie wiadomości ze skrzynki odbiorczej (z paginacją)."""
    count = client.sms.sms_count() or {}
    total = int(count.get("LocalInbox") or 0)
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
        raise SystemExit("Brak ROUTER_USER/ROUTER_PASS/ROUTER_IP – uzupełnij .env (odczyt SMS wymaga logowania).")

    with Connection(build_url()) as connection:
        client = Client(connection)

        before = client.sms.sms_count() or {}
        print(f"Skrzynka: {before.get('LocalInbox')} wiadomości, "
              f"{before.get('LocalUnread')} nieprzeczytanych.")

        messages = fetch_all_inbox(client)
        unread = [m for m in messages if m.get("Smstat") == SMSTAT_UNREAD]
        print(f"Do oznaczenia: {len(unread)} nieprzeczytanych.")

        if not unread:
            print("Nic do zrobienia – brak nieprzeczytanych.")
            return

        ok = 0
        errors = []
        for i, msg in enumerate(unread, 1):
            index = msg.get("Index")
            try:
                client.sms.set_read(int(index))
                ok += 1
            except Exception as exc:  # noqa: BLE001 - pojedynczy błąd nie przerywa całości
                errors.append((index, str(exc)))
            if i % 25 == 0 or i == len(unread):
                print(f"  oznaczono {i}/{len(unread)}...")

        after = client.sms.sms_count() or {}
        print(f"\nGotowe: oznaczono {ok}/{len(unread)} jako przeczytane.")
        if errors:
            print(f"Błędy ({len(errors)}): " + ", ".join(f"idx {i} ({e})" for i, e in errors[:5])
                  + (" ..." if len(errors) > 5 else ""))
        print(f"Nieprzeczytanych teraz: {after.get('LocalUnread')}")


if __name__ == "__main__":
    main()
