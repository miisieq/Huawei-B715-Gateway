#!/usr/bin/env python3
"""Restartuje router Huawei B715 przez API (Control=1, REBOOT).

Router co ~1,5–2 dni wpada w stan, w którym odrzuca NOWE połączenia z sieci lokalnej
(zestawione połączenia przeżywają); restart routera usuwa problem. Skrypt jest
przeznaczony do uruchamiania ręcznie oraz z timera systemd (oneshot, w nocy) — patrz README.

Działanie:
  * Loguje się do routera i wysyła restart przez TYPOWANĄ metodę biblioteki
    (`device.set_control(ControlModeEnum.REBOOT)` = Control=1). Wartość Control jest
    zaszyta w kodzie i NIE pochodzi z zewnątrz — Control=2 to RESET FABRYCZNY (router
    zgubiłby konfigurację Wi-Fi całej lokalizacji), dlatego nie wołamy
    `post_set('device/control', ...)` ręcznie.
  * Ponawia próby co REBOOT_RETRY_INTERVAL (dom. 20 s) maks. przez REBOOT_MAX_DURATION
    (dom. 900 s) przy błędach połączenia/timeoutach ORAZ przejściowych błędach
    logowania/sesji (zajęta sesja 108003, system busy 100004, nadmiar logowań 108007,
    CSRF/token 125002/125003). Pojedyncza próba zawodzi dokładnie wtedy, gdy restart
    jest potrzebny (router nie przyjmuje nowych połączeń), stąd pętla.
  * NIE ponawia przy błędnych danych logowania (108001/108002/108006) — to błąd
    konfiguracji, nie problem przejściowy.
  * Sukces = router odpowiedział `OK`; wtedy kończy natychmiast (bez czekania na powrót).

Kody wyjścia:
    0   - restart wysłany (albo --dry-run zakończony powodzeniem)
    !=0 - porażka (wyczerpane próby albo błąd konfiguracji / danych logowania)

Konfiguracja przez .env (katalog projektu) lub zmienne środowiskowe:
    ROUTER_USER, ROUTER_PASS, ROUTER_IP (wymagane)
    ROUTER_CONNECT_TIMEOUT / ROUTER_READ_TIMEOUT - limity czasu połączenia (dom. 3 / 5 s)
    REBOOT_RETRY_INTERVAL - odstęp między próbami w sekundach (dom. 20)
    REBOOT_MAX_DURATION   - maks. łączny czas ponawiania w sekundach (dom. 900)
    REBOOT_NOTIFY_SUCCESS - 1/0: czy wysyłać powiadomienie Pushover o sukcesie (dom. 1)
    PUSHOVER_TOKEN, PUSHOVER_USER - powiadomienia Pushover (opcjonalne; porażkę zgłasza
                                    zawsze, sukces zależnie od REBOOT_NOTIFY_SUCCESS)

Uruchomienie:
    python3 scripts/reboot_router.py             # restart (z ponawianiem do skutku)
    python3 scripts/reboot_router.py --dry-run   # tylko logowanie + odczyt, BEZ restartu
"""

# dzięki temu `X | None` w adnotacjach działa też na Pythonie 3.9
from __future__ import annotations

import contextlib
import os
import sys
import time

import requests
from dotenv import load_dotenv
from huawei_lte_api.Client import Client
from huawei_lte_api.Connection import Connection
from huawei_lte_api.enums.client import ResponseEnum
from huawei_lte_api.enums.device import ControlModeEnum
from huawei_lte_api.exceptions import LoginErrorInvalidCredentialsException

# .env leży w katalogu projektu (o poziom wyżej niż scripts/)
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

USER = os.environ.get("ROUTER_USER")
PASS = os.environ.get("ROUTER_PASS")
ROUTER_IP = os.environ.get("ROUTER_IP")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN")
PUSHOVER_USER = os.environ.get("PUSHOVER_USER")
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


def build_url() -> str:
    return f"http://{USER}:{PASS}@{ROUTER_IP}/"


def _parse_float_env(name: str, default: str) -> float:
    raw = os.environ.get(name, default)
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise SystemExit(f"[config] {name} musi być liczbą, jest: {raw!r}")
    if val <= 0:
        raise SystemExit(f"[config] {name} musi być > 0, jest: {val}")
    return val


def _parse_bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().casefold() not in ("0", "false", "no", "off", "")


# Limity czasu połączenia z routerem (sekundy) — ten sam mechanizm (connect, read)
# co w sms_to_pushover.py. Chronią pojedynczą próbę przed zawieszeniem, gdy router
# nie przyjmuje połączeń; pętla ponowień i tak spróbuje ponownie.
ROUTER_TIMEOUT = (
    _parse_float_env("ROUTER_CONNECT_TIMEOUT", "3"),
    _parse_float_env("ROUTER_READ_TIMEOUT", "5"),
)
RETRY_INTERVAL = _parse_float_env("REBOOT_RETRY_INTERVAL", "20")
MAX_DURATION = _parse_float_env("REBOOT_MAX_DURATION", "900")
NOTIFY_SUCCESS = _parse_bool_env("REBOOT_NOTIFY_SUCCESS", True)


def notify(title: str, message: str) -> None:
    """Best-effort powiadomienie Pushover. Brak konfiguracji lub błąd wysyłki NIE
    zmienia kodu wyjścia skryptu (co najwyżej log na stderr)."""
    if not (PUSHOVER_TOKEN and PUSHOVER_USER):
        return
    try:
        requests.post(
            PUSHOVER_URL,
            data={
                "token": PUSHOVER_TOKEN,
                "user": PUSHOVER_USER,
                "title": title[:250],      # limit tytułu Pushover
                "message": message[:1024],  # limit treści Pushover
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        print(f"[reboot] Pushover: nie udało się wysłać powiadomienia: {exc}", file=sys.stderr)


def response_is_ok(resp) -> bool:
    """Czy router potwierdził operację odpowiedzią `OK`."""
    return isinstance(resp, str) and resp.strip().upper() == ResponseEnum.OK.value


def send_reboot(client: Client):
    """Wysyła restart (Control=1) WYŁĄCZNIE przez typowaną metodę biblioteki, bez
    konstruowania wartości Control u nas (Control=2 = reset fabryczny)."""
    device = client.device
    if hasattr(device, "set_control"):
        return device.set_control(control=ControlModeEnum.REBOOT)
    return device.reboot()  # starsze wersje huawei-lte-api wystawiają reboot()


def attempt(dry_run: bool):
    """Jedna próba: logowanie do routera, a następnie restart (lub — w trybie dry-run —
    odczyt `device.information()` bez restartu). Zwraca odpowiedź routera. Wyjątki
    połączenia/logowania propagują w górę; o ponawianiu decyduje pętla w main()."""
    connection = Connection(build_url(), timeout=ROUTER_TIMEOUT)  # logowanie dzieje się tutaj
    try:
        client = Client(connection)
        if dry_run:
            return client.device.information() or {}
        return send_reboot(client)
    finally:
        # Po udanym restarcie router już się restartuje — wylogowanie bywa nieudane
        # lub zawisa (do READ_TIMEOUT); ignorujemy błąd close(), żeby udany restart
        # nie wyglądał na porażkę i nie wymuszał kolejnych prób.
        with contextlib.suppress(Exception):
            connection.close()


def main() -> None:
    dry_run = "--dry-run" in sys.argv[1:]

    missing = [
        name for name, val in (
            ("ROUTER_USER", USER), ("ROUTER_PASS", PASS), ("ROUTER_IP", ROUTER_IP),
        ) if not val
    ]
    if missing:
        raise SystemExit("Brak wymaganych zmiennych: " + ", ".join(missing))

    mode = "dry-run (bez restartu)" if dry_run else "restart"
    print(f"[reboot] start: {mode}; ponawianie co {RETRY_INTERVAL:.0f}s maks. przez {MAX_DURATION:.0f}s")

    deadline = time.monotonic() + MAX_DURATION
    attempt_no = 0
    last_error = None

    while True:
        attempt_no += 1
        try:
            result = attempt(dry_run)
        except LoginErrorInvalidCredentialsException as exc:
            # 108001/108002/108006 — błędne dane logowania; ponawianie nic nie zmieni.
            msg = f"Błędne dane logowania do routera ({exc}). Sprawdź ROUTER_USER / ROUTER_PASS."
            print(f"[reboot] {msg}", file=sys.stderr)
            if not dry_run:  # dry-run to test — nie alarmuj o restarcie, którego nie zlecono
                notify("Huawei B715: restart NIEUDANY", msg)
            raise SystemExit(2)
        except Exception as exc:  # noqa: BLE001
            # Wszystko inne traktujemy jak błąd przejściowy i ponawiamy (w granicach deadline):
            # połączenie/timeout (requests.RequestException), przejściowe błędy API/sesji
            # (zajęta sesja 108003, system busy 100004, nadmiar logowań 108007, CSRF/token
            # 125002/125003 → ResponseErrorException), ale też zniekształcone odpowiedzi z
            # biblioteki przy logowaniu (ValueError "No pub key was found", JSONDecodeError,
            # KeyError, błędy parsowania XML) — właśnie wtedy, gdy router jest w stanie
            # zdegradowanym, dla którego ten restart powstał. Dzięki temu porażka zawsze
            # trafia w pętlę i w końcowe powiadomienie Pushover, zamiast lecieć tracebackiem.
            last_error = exc
        else:
            if dry_run:
                print(f"[reboot] dry-run OK (próba {attempt_no}): router odpowiada, restart pominięty.")
                return
            if response_is_ok(result):
                print(f"[reboot] restart wysłany i potwierdzony (OK) — próba nr {attempt_no}.")
                if NOTIFY_SUCCESS:
                    notify("Huawei B715: restart wysłany",
                           f"Router przyjął polecenie restartu (próba nr {attempt_no}).")
                return
            # odpowiedź inna niż OK — potraktuj jak błąd przejściowy i ponów.
            last_error = RuntimeError(f"router nie potwierdził restartu (odpowiedź: {result!r})")

        now = time.monotonic()
        if now >= deadline:
            break
        print(f"[reboot] próba {attempt_no} nieudana ({type(last_error).__name__}: {last_error}); "
              f"ponawiam za {RETRY_INTERVAL:.0f}s", file=sys.stderr)
        time.sleep(min(RETRY_INTERVAL, deadline - now))

    # wyczerpany limit czasu — porażka
    what = "dry-run NIEUDANY" if dry_run else "Restart NIEUDANY"
    msg = (f"{what} po {attempt_no} próbach ({MAX_DURATION:.0f}s). "
           f"Ostatni błąd: {type(last_error).__name__}: {last_error}")
    print(f"[reboot] {msg}", file=sys.stderr)
    if not dry_run:  # dry-run to test — nie wysyłaj alertu Pushover
        notify("Huawei B715: restart NIEUDANY", msg)
    raise SystemExit(1)


if __name__ == "__main__":
    main()
