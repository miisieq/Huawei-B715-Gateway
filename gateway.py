#!/usr/bin/env python3
"""Prometheus exporter + REST API dla routera Huawei B715s (na bazie huawei-lte-api).

Wystawia jeden serwer HTTP z endpointami:
    GET    /metrics               – metryki Prometheus (sygnał, ruch, SMS, status)
    GET    /sms?status=all         – lista SMS z odbiorczej; status = all|unread|read
    POST   /sms/{index}/read       – oznacz SMS o danym Index jako przeczytany
    DELETE /sms/{index}            – usuń SMS o danym Index
    GET    /                       – krótka pomoc

Konfiguracja przez plik .env (obok skryptu) lub zmienne środowiskowe:
    ROUTER_USER=admin
    ROUTER_PASS=twoje_haslo
    ROUTER_IP=192.168.0.1
    HTTP_PORT=9101            # opcjonalne, port serwera HTTP
    HTTP_ADDR=0.0.0.0         # opcjonalne, adres nasłuchu

Uruchomienie:
    python3 gateway.py
"""

import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from huawei_lte_api.Client import Client
from huawei_lte_api.Connection import Connection
from huawei_lte_api.enums.sms import BoxTypeEnum
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

load_dotenv()

ROUTER_IP = os.environ.get("ROUTER_IP")
USER = os.environ.get("ROUTER_USER")
PASS = os.environ.get("ROUTER_PASS")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "9101"))
HTTP_ADDR = os.environ.get("HTTP_ADDR", "0.0.0.0")

# Smstat: "0" = nieprzeczytana, "1" = przeczytana (zweryfikowane na B715s)
SMSTAT_READ = "1"

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def build_url() -> str:
    if USER and PASS:
        return f"http://{USER}:{PASS}@{ROUTER_IP}/"
    return f"http://{ROUTER_IP}/"


def to_float(value):
    """Wyciąga liczbę z wartości typu '-99dBm', '3dB' -> float lub None."""
    if value is None:
        return None
    match = _NUM_RE.search(str(value))
    return float(match.group()) if match else None


_EARFCN_DL_RE = re.compile(r"DL:\s*(\d+)")
_CARRIER_RE = re.compile(r"Carrier(\d+)")

# Tabela pasm E-UTRA (DL): band -> (earfcn_min, earfcn_max, F_dl_low[MHz], N_offs_DL)
# Wg 3GPP TS 36.101. Wzor: F_dl = F_dl_low + 0.1 * (EARFCN - N_offs_DL)
_LTE_DL_BANDS = [
    (1, 0, 599, 2110.0, 0),
    (2, 600, 1199, 1930.0, 600),
    (3, 1200, 1949, 1805.0, 1200),
    (4, 1950, 2399, 2110.0, 1950),
    (5, 2400, 2649, 869.0, 2400),
    (7, 2750, 3449, 2620.0, 2750),
    (8, 3450, 3799, 925.0, 3450),
    (20, 6150, 6449, 791.0, 6150),
    (28, 9210, 9659, 758.0, 9210),
    (32, 9920, 10359, 1452.0, 9920),  # SDL (tylko DL)
    (38, 37750, 38249, 2570.0, 37750),  # TDD
    (40, 38650, 39649, 2300.0, 38650),  # TDD
]


def parse_dl_earfcn(value):
    """'DL:1725 UL:19725' -> 1725 (int) lub None."""
    if not value:
        return None
    match = _EARFCN_DL_RE.search(str(value))
    return int(match.group(1)) if match else None


def dl_earfcn_to_mhz(earfcn):
    """Przelicza DL EARFCN na częstotliwość w MHz wg tabeli pasm; None gdy nieznane."""
    if earfcn is None:
        return None
    for _band, lo, hi, f_low, n_offs in _LTE_DL_BANDS:
        if lo <= earfcn <= hi:
            return round(f_low + 0.1 * (earfcn - n_offs), 1)
    return None


def count_carriers(dl_mcs, ul_mcs):
    """Liczy aktywne nośne po polach CarrierN w mcs (1 = brak agregacji). None gdy brak danych."""
    for src in (dl_mcs, ul_mcs):
        if src:
            found = set(_CARRIER_RE.findall(str(src)))
            if found:
                return len(found)
    return None


def _normalize_messages(sms_list_response) -> list:
    """Zwraca listę wiadomości z odpowiedzi get_sms_list (dict|lista|brak)."""
    messages = (sms_list_response or {}).get("Messages") or {}
    msg = messages.get("Message") if isinstance(messages, dict) else None
    if not msg:
        return []
    return [msg] if isinstance(msg, dict) else msg


def fetch_all_inbox(client: Client) -> list:
    """Pobiera WSZYSTKIE wiadomości ze skrzynki odbiorczej (z paginacją)."""
    total = to_float((client.sms.sms_count() or {}).get("LocalInbox")) or 0
    total = int(total)
    per_page = 50
    collected = []
    page = 1
    while len(collected) < total:
        batch = _normalize_messages(
            client.sms.get_sms_list(
                page=page, box_type=BoxTypeEnum.LOCAL_INBOX, read_count=per_page
            )
        )
        if not batch:
            break
        collected.extend(batch)
        page += 1
    return collected


def sms_to_dict(msg: dict) -> dict:
    return {
        "index": int(msg.get("Index")) if msg.get("Index") is not None else None,
        "phone": msg.get("Phone"),
        "date": msg.get("Date"),
        "content": msg.get("Content"),
        "read": msg.get("Smstat") == SMSTAT_READ,
    }


# --------------------------------------------------------------------------- #
#  Prometheus collector
# --------------------------------------------------------------------------- #
class HuaweiCollector:
    """Custom collector – łączy się z routerem przy każdym zbieraniu metryk."""

    def collect(self):
        prefix = "huawei_lte"
        up = GaugeMetricFamily(f"{prefix}_up", "Czy scrape routera się powiódł (1/0)")
        scrape_duration = GaugeMetricFamily(
            f"{prefix}_scrape_duration_seconds", "Czas trwania scrape'u routera"
        )

        start = time.perf_counter()
        try:
            with Connection(build_url()) as connection:
                client = Client(connection)
                metrics = list(self._collect_all(client, prefix))
            up.add_metric([], 1.0)
        except Exception as exc:  # noqa: BLE001
            print(f"[gateway] scrape error: {exc}")
            up.add_metric([], 0.0)
            metrics = []

        scrape_duration.add_metric([], time.perf_counter() - start)
        yield up
        yield scrape_duration
        yield from metrics

    def _collect_all(self, client: Client, prefix: str):
        info = client.device.information() or {}
        device_info = GaugeMetricFamily(
            f"{prefix}_device_info",
            "Statyczne informacje o urządzeniu (wartość zawsze 1)",
            labels=["device_name", "hardware_version", "software_version", "imei"],
        )
        device_info.add_metric(
            [
                str(info.get("DeviceName", "")),
                str(info.get("HardwareVersion", "")),
                str(info.get("SoftwareVersion", "")),
                str(info.get("Imei", "")),
            ],
            1.0,
        )
        yield device_info

        # Operator (metryka info: wartość zawsze 1, nazwa/PLMN w etykietach)
        plmn = client.net.current_plmn() or {}
        operator_info = GaugeMetricFamily(
            f"{prefix}_operator_info",
            "Operator sieci (info; wartość 1, dane w etykietach)",
            labels=["operator", "plmn"],
        )
        operator_info.add_metric(
            [str(plmn.get("FullName") or ""), str(plmn.get("Numeric") or "")], 1.0
        )
        yield operator_info

        signal = client.device.signal() or {}
        signal_gauges = {
            "rsrp_dbm": ("rsrp", "RSRP [dBm]"),
            "rsrq_db": ("rsrq", "RSRQ [dB]"),
            "rssi_dbm": ("rssi", "RSSI [dBm]"),
            "sinr_db": ("sinr", "SINR [dB]"),
            "band": ("band", "Aktualny band LTE"),
            "cell_id": ("cell_id", "Cell ID"),
            "pci": ("pci", "Physical Cell ID"),
        }
        for suffix, (key, desc) in signal_gauges.items():
            val = to_float(signal.get(key))
            if val is not None:
                g = GaugeMetricFamily(f"{prefix}_signal_{suffix}", desc)
                g.add_metric([], val)
                yield g

        # Nośna główna: EARFCN (DL) i przeliczona częstotliwość
        earfcn_dl = parse_dl_earfcn(signal.get("earfcn"))
        if earfcn_dl is not None:
            g = GaugeMetricFamily(
                f"{prefix}_signal_dl_earfcn", "EARFCN nośnej głównej (DL)"
            )
            g.add_metric([], earfcn_dl)
            yield g
            freq = dl_earfcn_to_mhz(earfcn_dl)
            if freq is not None:
                g = GaugeMetricFamily(
                    f"{prefix}_signal_dl_frequency_mhz",
                    "Częstotliwość nośnej głównej (DL) [MHz]",
                )
                g.add_metric([], freq)
                yield g

        # Carrier Aggregation: liczba aktywnych nośnych (1 = brak agregacji)
        carriers = count_carriers(signal.get("dl_mcs"), signal.get("ul_mcs"))
        if carriers is not None:
            g = GaugeMetricFamily(
                f"{prefix}_ca_carriers",
                "Liczba aktywnych nośnych LTE (1 = brak agregacji, >1 = LTE+)",
            )
            g.add_metric([], carriers)
            yield g

        status = client.monitoring.status() or {}
        conn_status = to_float(status.get("ConnectionStatus"))
        if conn_status is not None:
            g = GaugeMetricFamily(
                f"{prefix}_connection_status", "Kod statusu połączenia (901 = połączony)"
            )
            g.add_metric([], conn_status)
            yield g
        net_type = to_float(status.get("CurrentNetworkType"))
        if net_type is not None:
            g = GaugeMetricFamily(f"{prefix}_network_type", "Kod typu sieci (19 = LTE)")
            g.add_metric([], net_type)
            yield g

        traffic = client.monitoring.traffic_statistics() or {}
        current_gauges = {
            "current_connect_time_seconds": ("CurrentConnectTime", "Czas bieżącej sesji [s]"),
            "current_download_bytes": ("CurrentDownload", "Pobrane w bieżącej sesji [B]"),
            "current_upload_bytes": ("CurrentUpload", "Wysłane w bieżącej sesji [B]"),
        }
        for suffix, (key, desc) in current_gauges.items():
            val = to_float(traffic.get(key))
            if val is not None:
                g = GaugeMetricFamily(f"{prefix}_traffic_{suffix}", desc)
                g.add_metric([], val)
                yield g

        total_counters = {
            "download_bytes_total": ("TotalDownload", "Łącznie pobrane [B]"),
            "upload_bytes_total": ("TotalUpload", "Łącznie wysłane [B]"),
        }
        for suffix, (key, desc) in total_counters.items():
            val = to_float(traffic.get(key))
            if val is not None:
                c = CounterMetricFamily(f"{prefix}_traffic_{suffix}", desc)
                c.add_metric([], val)
                yield c

        sms = client.sms.sms_count() or {}
        sms_gauges = {
            "inbox": ("LocalInbox", "Liczba SMS w skrzynce odbiorczej"),
            "unread": ("LocalUnread", "Liczba nieprzeczytanych SMS"),
            "outbox": ("LocalOutbox", "Liczba SMS w skrzynce wysłanych"),
        }
        for suffix, (key, desc) in sms_gauges.items():
            val = to_float(sms.get(key))
            if val is not None:
                g = GaugeMetricFamily(f"{prefix}_sms_{suffix}", desc)
                g.add_metric([], val)
                yield g


# --------------------------------------------------------------------------- #
#  HTTP handler
# --------------------------------------------------------------------------- #
INDEX_HELP = {
    "endpoints": {
        "GET /metrics": "Metryki Prometheus",
        "GET /sms?status=all|unread|read": "Lista SMS ze skrzynki odbiorczej",
        "POST /sms/{index}/read": "Oznacz SMS o danym Index jako przeczytany",
        "DELETE /sms/{index}": "Usuń SMS o danym Index",
    }
}

# Wzorce ścieżek (routing RESTful po Index w ścieżce)
_SMS_READ_RE = re.compile(r"^/sms/(\d+)/read$")
_SMS_ITEM_RE = re.compile(r"^/sms/(\d+)$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # cichszy log
        print(f"[gateway] {self.address_string()} {fmt % args}")

    def _send_json(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_metrics(self):
        output = generate_latest(REGISTRY)
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPE_LATEST)
        self.send_header("Content-Length", str(len(output)))
        self.end_headers()
        self.wfile.write(output)

    # ---- GET ----
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path == "/metrics":
            self._send_metrics()
        elif path == "/sms":
            self._handle_sms_list(query)
        elif path == "/":
            self._send_json(200, INDEX_HELP)
        else:
            self._send_json(404, {"error": "not found", "path": self.path})

    # ---- POST ----
    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        match = _SMS_READ_RE.match(path)
        if match:
            self._handle_sms_read(int(match.group(1)))
        else:
            self._send_json(404, {"error": "not found", "path": self.path})

    # ---- DELETE ----
    def do_DELETE(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        match = _SMS_ITEM_RE.match(path)
        if match:
            self._handle_sms_delete(int(match.group(1)))
        else:
            self._send_json(404, {"error": "not found", "path": self.path})

    # ---- logika ----
    def _handle_sms_list(self, query: dict):
        status = (query.get("status", ["all"])[0] or "all").lower()
        if status not in ("all", "unread", "read"):
            self._send_json(
                400, {"error": "status musi być: all|unread|read", "got": status}
            )
            return
        try:
            with Connection(build_url()) as connection:
                client = Client(connection)
                messages = [sms_to_dict(m) for m in fetch_all_inbox(client)]
        except Exception as exc:  # noqa: BLE001
            self._send_json(502, {"error": "router error", "detail": str(exc)})
            return

        if status == "unread":
            messages = [m for m in messages if not m["read"]]
        elif status == "read":
            messages = [m for m in messages if m["read"]]

        self._send_json(200, {"status": status, "count": len(messages), "messages": messages})

    def _handle_sms_read(self, index: int):
        try:
            with Connection(build_url()) as connection:
                response = Client(connection).sms.set_read(index)
        except Exception as exc:  # noqa: BLE001
            self._send_json(502, {"error": "router error", "detail": str(exc)})
            return
        self._send_json(200, {"ok": True, "index": index, "response": response})

    def _handle_sms_delete(self, index: int):
        try:
            with Connection(build_url()) as connection:
                response = Client(connection).sms.delete_sms(index)
        except Exception as exc:  # noqa: BLE001
            self._send_json(502, {"error": "router error", "detail": str(exc)})
            return
        self._send_json(200, {"ok": True, "index": index, "response": response})


def main() -> None:
    if not ROUTER_IP:
        raise SystemExit("[gateway] Brak ROUTER_IP – ustaw adres routera w .env.")
    if not (USER and PASS):
        print(
            "[gateway] UWAGA: brak ROUTER_USER/ROUTER_PASS – "
            "większość operacji wymaga logowania. Uzupełnij plik .env."
        )
    REGISTRY.register(HuaweiCollector())
    server = ThreadingHTTPServer((HTTP_ADDR, HTTP_PORT), Handler)
    print(
        f"[gateway] start na http://{HTTP_ADDR}:{HTTP_PORT} "
        f"(router {ROUTER_IP}) – endpointy: /metrics, /sms, /sms/{{index}}/read, DELETE /sms/{{index}}"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
