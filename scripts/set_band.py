#!/usr/bin/env python3
"""Ustawia maskę pasm LTE (band lock) na routerze.

Maskę podaje się jako argument — szesnastkowo, bez przedrostka `0x`.
Maska to suma bitów 2^(N-1) dla każdego dozwolonego pasma N (patrz README).

Przykłady masek:
    1      = B1
    5      = B1 + B3
    45     = B1 + B3 + B7
    auto   = wszystkie pasma (7FFFFFFFFFFFFFFF) + tryb AUTO (powrót do automatu)

Uruchomienie:
    python3 scripts/set_band.py <maska>
    python3 scripts/set_band.py 45
    python3 scripts/set_band.py auto

⚠️ Zmiana maski na chwilę zrywa połączenie LTE (router się przerejestrowuje).

Konfiguracja przez .env (katalog projektu): ROUTER_USER, ROUTER_PASS, ROUTER_IP (wymagane).
"""

from __future__ import annotations  # `str | None` w adnotacjach działa też na Pythonie 3.9

import os
import sys

from dotenv import load_dotenv
from huawei_lte_api.Client import Client
from huawei_lte_api.Connection import Connection
from huawei_lte_api.enums.net import LTEBandEnum, NetworkBandEnum, NetworkModeEnum

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

USER = os.environ.get("ROUTER_USER")
PASS = os.environ.get("ROUTER_PASS")
ROUTER_IP = os.environ.get("ROUTER_IP")

ALL_BANDS = f"{LTEBandEnum.ALL.value:X}"  # 7FFFFFFFFFFFFFFF


def build_url() -> str:
    return f"http://{USER}:{PASS}@{ROUTER_IP}/"


def usage(current: str | None = None) -> None:
    print("Użycie: set_band.py <maska_hex | auto>", file=sys.stderr)
    print("  np. 1 (B1), 5 (B1+B3), 45 (B1+B3+B7), auto (wszystkie + tryb AUTO)", file=sys.stderr)
    if current is not None:
        print(f"  aktualna maska LTEBand: {current}", file=sys.stderr)


def main() -> None:
    if not (USER and PASS and ROUTER_IP):
        raise SystemExit("Brak ROUTER_USER/ROUTER_PASS/ROUTER_IP – uzupełnij .env.")

    args = sys.argv[1:]
    if len(args) != 1:
        # bez argumentu: pokaż pomoc i bieżącą maskę
        try:
            with Connection(build_url()) as c:
                current = (Client(c).net.net_mode() or {}).get("LTEBand")
        except Exception:  # noqa: BLE001
            current = None
        usage(current)
        raise SystemExit(2)

    arg = args[0].strip().lower()
    if arg == "auto":
        lteband = ALL_BANDS
    else:
        # waliduj: poprawna liczba szesnastkowa (bez 0x)
        try:
            int(arg, 16)
        except ValueError:
            usage()
            raise SystemExit(f"Niepoprawna maska: {args[0]!r} (oczekiwano hex bez 0x, np. 45).")
        lteband = arg.upper()

    with Connection(build_url()) as c:
        client = Client(c)
        before = (client.net.net_mode() or {}).get("LTEBand")
        print(f"PRZED:   LTEBand={before}")
        print(f"USTAWIAM LTEBand={lteband}, tryb=AUTO")
        resp = client.net.set_net_mode(
            lteband=lteband,
            networkband=NetworkBandEnum.ALL.value,
            networkmode=NetworkModeEnum.MODE_AUTO.value,
        )
        print("ODPOWIEDŹ:", resp)


if __name__ == "__main__":
    main()
