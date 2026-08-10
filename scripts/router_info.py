#!/usr/bin/env python3
"""Pobiera podstawowe informacje o routerze Huawei B715s oraz listę SMS-ów.

Użycie:
    python3 router_info.py

Dane logowania (wymagane dla SMS i większości danych) podaj w pliku .env obok
tego skryptu:
    ROUTER_USER=admin
    ROUTER_PASS=twoje_haslo
    ROUTER_IP=192.168.0.1

Alternatywnie możesz je podać jako zmienne środowiskowe (export ...).
"""

import os

from dotenv import load_dotenv
from huawei_lte_api.Client import Client
from huawei_lte_api.Connection import Connection
from huawei_lte_api.enums.sms import BoxTypeEnum

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))  # wczytuje zmienne z pliku .env, jeśli istnieje

ROUTER_IP = os.environ.get("ROUTER_IP")
USER = os.environ.get("ROUTER_USER")
PASS = os.environ.get("ROUTER_PASS")


def build_url() -> str:
    """Buduje URL połączenia, wstrzykując dane logowania jeśli podane."""
    if USER and PASS:
        return f"http://{USER}:{PASS}@{ROUTER_IP}/"
    return f"http://{ROUTER_IP}/"


def section(title: str) -> None:
    print(f"\n{'=' * 60}\n {title}\n{'=' * 60}")


def dump(data: dict, keys=None) -> None:
    """Wypisuje wybrane (lub wszystkie) pola słownika."""
    if not isinstance(data, dict):
        print(data)
        return
    items = [(k, data.get(k)) for k in keys] if keys else data.items()
    for key, value in items:
        print(f"  {key:<28} {value}")


def main() -> None:
    if not ROUTER_IP:
        raise SystemExit("Brak ROUTER_IP – uzupełnij .env.")
    with Connection(build_url()) as connection:
        client = Client(connection)

        # --- Informacje o urządzeniu ---
        section("Urządzenie")
        info = client.device.information()
        dump(info, [
            "DeviceName", "HardwareVersion", "SoftwareVersion",
            "Imei", "Imsi", "Iccid", "Msisdn", "MacAddress1",
            "WanIPAddress", "uptime",
        ])

        # --- Status połączenia / sygnał ---
        section("Status połączenia")
        status = client.monitoring.status()
        dump(status, [
            "ConnectionStatus", "CurrentNetworkType", "SignalIcon",
            "SignalStrength", "CurrentWifiUser", "PrimaryDns", "SecondaryDns",
        ])

        section("Sygnał (poziomy)")
        signal = client.device.signal()
        dump(signal, ["rsrq", "rsrp", "rssi", "sinr", "cell_id", "pci", "band"])

        # --- Operator / sieć ---
        section("Operator")
        plmn = client.net.current_plmn()
        dump(plmn, ["FullName", "ShortName", "Numeric", "State"])

        # --- Ruch / zużycie danych ---
        section("Statystyki ruchu (bieżąca sesja)")
        traffic = client.monitoring.traffic_statistics()
        dump(traffic, [
            "CurrentConnectTime", "CurrentDownload", "CurrentUpload",
            "TotalDownload", "TotalUpload",
        ])

        # --- SMS ---
        section("Wiadomości SMS (skrzynka odbiorcza)")
        try:
            sms_count = client.sms.sms_count()
            dump(sms_count, ["LocalInbox", "LocalUnread", "LocalOutbox"])

            inbox = client.sms.get_sms_list(
                page=1, box_type=BoxTypeEnum.LOCAL_INBOX, read_count=20
            )
            messages = inbox.get("Messages") or {}
            msg_list = messages.get("Message") if isinstance(messages, dict) else None

            if not msg_list:
                print("  (brak wiadomości w skrzynce odbiorczej)")
            else:
                # Pojedyncza wiadomość zwracana jest jako dict, wiele – jako lista
                if isinstance(msg_list, dict):
                    msg_list = [msg_list]
                for msg in msg_list:
                    read = "PRZECZYTANA" if msg.get("Smstat") == "1" else "NIEPRZECZYTANA"
                    print(f"\n  [{read}] od {msg.get('Phone')}  ({msg.get('Date')})")
                    print(f"      {msg.get('Content')}")
        except Exception as exc:  # noqa: BLE001 - SMS zwykle wymaga zalogowania
            print(f"  Nie udało się pobrać SMS-ów: {exc}")
            print("  (Ustaw ROUTER_USER i ROUTER_PASS – odczyt SMS wymaga logowania.)")


if __name__ == "__main__":
    main()
