# Huawei B715 Gateway

Bramka HTTP do routera LTE **Huawei B715s** (i pokrewnych) — jest warstwą pośredniczącą pomiędzy zamkniętym API
routera na czysty REST oraz format Prometheus. Oparta na bibliotece [`huawei-lte-api`](https://github.com/Salamek/huawei-lte-api).

Główny proces (`gateway.py`) wystawia serwer HTTP, który:
- **monitoruje** — `/metrics` z metrykami Prometheus (sygnał, agregacja, ruch, SMS, status, operator),
- **zarządza** — REST API do SMS-ów (lista, oznaczanie jako przeczytane, usuwanie).

Niezależnie od usługi, w katalogu `scripts/` są osobne narzędzia uruchamiane ręcznie lub przez timer systemd:
blokada pasm LTE, masowe operacje na SMS-ach oraz przekazywanie SMS-ów do Pushover.

## Zawartość projektu

| Plik | Opis |
|------|------|
| `gateway.py` | Główny serwer: bramka HTTP (Prometheus + REST API SMS) |
| `requirements.txt` | Zależności Pythona |
| `.env.example` | Szablon konfiguracji |
| `scripts/` | Niezależne narzędzia (uruchamiane poza usługą — ręcznie lub przez timer systemd) |

### `scripts/` — narzędzia pomocnicze

| Skrypt | Opis |
|--------|------|
| `router_info.py` | Wypisuje info o routerze, połączeniu i SMS-y |
| `set_band.py` | Blokada pasm LTE (band lock); maskę podaje się jako argument |
| `mark_all_read.py` | Oznacza wszystkie SMS-y jako przeczytane |
| `delete_all_smses.py` | Usuwa wszystkie SMS-y (dry-run; kasuje z `--yes`) |
| `sms_to_pushover.py` | Wysyła nieprzeczytane SMS-y jako powiadomienia Pushover (z timera systemd) |
| `reboot_router.py` | Restartuje router przez API (z ponawianiem; ręcznie lub z timera systemd) |

> Skrypty w `scripts/` ładują `.env` z katalogu projektu, więc można je uruchamiać z dowolnego miejsca.

### Uruchamianie skryptów

Skrypty uruchamiaj **interpreterem z venv** (nie systemowym `python3`), podając ścieżkę `scripts/<nazwa>.py`.
Ponieważ ładują `.env` z katalogu projektu, można je odpalać z dowolnego katalogu.

Na serwerze (wdrożenie w `/srv/huawei-b715-gateway`):

```bash
cd /srv/huawei-b715-gateway
./venv/bin/python scripts/router_info.py             # podgląd routera, połączenia i SMS-ów
./venv/bin/python scripts/set_band.py 45             # blokada pasm LTE (tu: B1+B3+B7)
./venv/bin/python scripts/mark_all_read.py           # oznacz wszystkie SMS-y jako przeczytane
./venv/bin/python scripts/delete_all_smses.py        # dry-run — pokazuje, ile by usunął
./venv/bin/python scripts/delete_all_smses.py --yes  # faktyczne usunięcie wszystkich SMS-ów
./venv/bin/python scripts/sms_to_pushover.py         # wyślij nieprzeczytane SMS-y do Pushover
./venv/bin/python scripts/reboot_router.py           # zrestartuj router (z ponawianiem do skutku)
./venv/bin/python scripts/reboot_router.py --dry-run # test: logowanie + odczyt, BEZ restartu
```

Lokalnie działa tak samo (najpierw utwórz venv — patrz [Instalacja](#instalacja)).

Bez wchodzenia do katalogu projektu użyj pełnych ścieżek — konfiguracja `.env` i tak zostanie znaleziona:

```bash
/srv/huawei-b715-gateway/venv/bin/python /srv/huawei-b715-gateway/scripts/sms_to_pushover.py
```

> `sms_to_pushover.py` jest zwykle uruchamiany przez timer systemd — patrz [Powiadomienia SMS → Pushover (systemd timer)](#powiadomienia-sms--pushover-systemd-timer).

## Wymagania

- Python 3.9+
- Sieciowy dostęp do routera (adres podajesz w `ROUTER_IP`, np. `http://192.168.0.1`)
- Dane logowania do panelu routera (odczyt większości danych i SMS-ów wymaga zalogowania)

## Instalacja

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## Konfiguracja

Skopiuj `.env.example` do `.env` i uzupełnij:

```ini
ROUTER_USER=admin
ROUTER_PASS=twoje_haslo        # hasło do panelu (dla B715 zwykle z naklejki)
ROUTER_IP=192.168.0.1          # adres routera (wymagane)
HTTP_PORT=9101                 # opcjonalne, port serwera HTTP
HTTP_ADDR=0.0.0.0              # opcjonalne, adres nasłuchu
ROUTER_CONNECT_TIMEOUT=3       # opcjonalne, limit czasu nawiązania połączenia [s, float]
ROUTER_READ_TIMEOUT=5          # opcjonalne, limit czasu oczekiwania na odpowiedź [s, float]
```

> `ROUTER_CONNECT_TIMEOUT`/`ROUTER_READ_TIMEOUT` chronią przed zawieszeniem, gdy router
> przestaje przyjmować połączenia. Bez nich żądanie wisi do systemowego timeoutu TCP
> (~2 min), scrape przekracza `scrape_timeout` (10 s), a wiszące połączenia się kumulują.
> Błędna wartość (nie-liczba lub ≤ 0) kończy start czytelnym błędem.

> `.env` jest w `.gitignore` — nie trafia do repozytorium.

## Uruchomienie

```bash
./venv/bin/python gateway.py
# podgląd:
curl http://localhost:9101/metrics
```

### Jako usługa systemd

`/etc/systemd/system/huawei-b715-gateway.service`:

```ini
[Unit]
Description=huawei-b715-gateway (LTE monitoring + REST API)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/srv/huawei-b715-gateway
ExecStart=/srv/huawei-b715-gateway/venv/bin/python /srv/huawei-b715-gateway/gateway.py
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now huawei-b715-gateway.service
journalctl -u huawei-b715-gateway -f     # logi na żywo
```

## Endpointy HTTP

| Metoda | Ścieżka | Opis |
|--------|---------|------|
| GET | `/metrics` | Metryki Prometheus |
| GET | `/sms?status=all\|unread\|read` | Lista SMS ze skrzynki odbiorczej (JSON) |
| POST | `/sms/{index}/read` | Oznacz SMS o danym Index jako przeczytany |
| DELETE | `/sms/{index}` | Usuń SMS o danym Index |
| GET | `/` | Krótka pomoc (JSON) |

Przykłady:

```bash
curl "http://localhost:9101/sms?status=unread"
curl -X POST "http://localhost:9101/sms/40001/read"
curl -X DELETE "http://localhost:9101/sms/40001"
```

> Aby oznaczyć wszystkie nieprzeczytane naraz, użyj skryptu `scripts/mark_all_read.py`.

## Metryki

| Metryka | Typ | Opis |
|---------|-----|------|
| `huawei_lte_up` | gauge | 1 = scrape routera OK, 0 = błąd |

> **Semantyka `up` vs `huawei_lte_up`.** Gdy router nie przyjmuje połączeń, scrape kończy się
> szybko na `connect` (ok. `ROUTER_CONNECT_TIMEOUT`, poniżej `scrape_timeout` 10 s). Scraper
> widzi wtedy `up=1` (eksporter odpowiedział HTTP 200) z `huawei_lte_up 0` (router
> nieosiągalny) — czyli „eksporter działa, routera nie ma". Bez limitu `connect` żądanie
> wisiałoby do systemowego timeoutu TCP (~2 min), scrape przekroczyłby `scrape_timeout`, a
> scraper zapisałby `up=0` („eksporter nie działa"), gubiąc to rozróżnienie.
>
> Uwaga: `ROUTER_READ_TIMEOUT` działa **per żądanie**, a scrape to kilka kolejnych żądań —
> router, który przyjmuje połączenie, ale odpowiada bardzo wolno, może mimo limitów przekroczyć
> `scrape_timeout`. Limity chronią przede wszystkim przed martwym (nieodpowiadającym) routerem.

| `huawei_lte_scrape_duration_seconds` | gauge | Czas scrape'u |
| `huawei_lte_device_info{device_name,hardware_version,software_version,imei}` | gauge | Info o urządzeniu (wartość 1) |
| `huawei_lte_operator_info{operator,plmn}` | gauge | Operator sieci (wartość 1) |
| `huawei_lte_signal_rsrp_dbm` | gauge | RSRP [dBm] — moc sygnału |
| `huawei_lte_signal_rsrq_db` | gauge | RSRQ [dB] — jakość sygnału |
| `huawei_lte_signal_rssi_dbm` | gauge | RSSI [dBm] — całkowita moc odbierana |
| `huawei_lte_signal_sinr_db` | gauge | SINR [dB] — sygnał/szum |
| `huawei_lte_signal_band` | gauge | Aktualny band LTE |
| `huawei_lte_signal_cell_id` | gauge | Cell ID |
| `huawei_lte_signal_pci` | gauge | Physical Cell ID |
| `huawei_lte_signal_dl_earfcn` | gauge | EARFCN nośnej głównej (DL) |
| `huawei_lte_signal_dl_frequency_mhz` | gauge | Częstotliwość nośnej głównej [MHz] |
| `huawei_lte_ca_carriers` | gauge | Liczba aktywnych nośnych (1 = brak agregacji, >1 = LTE+) |
| `huawei_lte_connection_status` | gauge | Kod statusu połączenia (901 = połączony) |
| `huawei_lte_network_type` | gauge | Kod typu sieci (19 = LTE) |
| `huawei_lte_traffic_current_connect_time_seconds` | gauge | Czas bieżącej sesji [s] |
| `huawei_lte_traffic_current_download_bytes` | gauge | Pobrane w bieżącej sesji [B] |
| `huawei_lte_traffic_current_upload_bytes` | gauge | Wysłane w bieżącej sesji [B] |
| `huawei_lte_traffic_download_bytes_total` | counter | Łącznie pobrane [B] |
| `huawei_lte_traffic_upload_bytes_total` | counter | Łącznie wysłane [B] |
| `huawei_lte_sms_inbox` | gauge | Liczba SMS w skrzynce odbiorczej |
| `huawei_lte_sms_unread` | gauge | Liczba nieprzeczytanych SMS |
| `huawei_lte_sms_outbox` | gauge | Liczba SMS w skrzynce wysłanych |

### Interpretacja sygnału

| Parametr | 🔴 Źle | 🟡 Umiarkowanie | 🟢 Dobrze |
|----------|--------|-----------------|-----------|
| RSRP [dBm] | < −100 | −100…−90 | > −90 |
| RSSI [dBm] | < −75 | −75…−65 | > −65 |
| RSRQ [dB] | < −15 | −15…−10 | > −10 |
| SINR [dB] | < 0 | 0…13 | > 13 |

## Prometheus — scrape config

```yaml
scrape_configs:
  - job_name: huawei-b715
    static_configs:
      - targets: ['SERWER:9101']
```

## Grafana

Metryki są standardowe (Prometheus), więc po dodaniu źródła danych Prometheus można je
wizualizować w Grafanie dowolnym dashboardem (panele oparte np. o `huawei_lte_signal_*`,
`huawei_lte_traffic_*`, `huawei_lte_ca_carriers`).

## Blokada pasm LTE (`scripts/set_band.py`)

`scripts/set_band.py` ustawia maskę pasm LTE (band lock). Maskę podaje się **jako argument**
(szesnastkowo, bez `0x`):

```bash
./venv/bin/python scripts/set_band.py 45      # zablokuj na B1+B3+B7
./venv/bin/python scripts/set_band.py 5       # zablokuj na B1+B3
./venv/bin/python scripts/set_band.py auto    # wszystkie pasma + tryb AUTO (powrót do automatu)
./venv/bin/python scripts/set_band.py         # bez argumentu: pokazuje pomoc i bieżącą maskę
```

### Jak wyliczyć maskę

Każdemu pasmu **BN** odpowiada bit **2^(N-1)**. Maska to **suma** (OR) bitów pasm, które chcesz
dopuścić, zapisana szesnastkowo bez `0x`.

| Pasmo | Bit (2^(N-1)) | Wartość hex |
|-------|---------------|-------------|
| B1  | 2^0  | `1` |
| B3  | 2^2  | `4` |
| B7  | 2^6  | `40` |
| B8  | 2^7  | `80` |
| B20 | 2^19 | `80000` |
| B28 | 2^27 | `8000000` |
| B38 | 2^37 | `2000000000` |

Aby połączyć kilka pasm, **zsumuj ich wartości hex**:

- B1 + B3 → `1 + 4` = `5`
- B1 + B3 + B7 → `1 + 4 + 40` = `45`
- B1 + B3 + B20 → `1 + 4 + 80000` = `80005`
- wszystkie pasma → `7FFFFFFFFFFFFFFF` (lub `auto`)

Szybkie wyliczenie w Pythonie (np. B1+B3+B7):

```python
print("%X" % (2**(1-1) | 2**(3-1) | 2**(7-1)))   # -> 45
```

> Router może zignorować pasma, których fizycznie nie obsługuje — zwraca wtedy maskę
> przyciętą do swoich możliwości (np. `2088080085`).

> ⚠️ Zmiana maski na chwilę zrywa połączenie LTE (router się przerejestrowuje).

## Panel routera przez reverse proxy (opcjonalnie)

Jeśli udostępniasz panel routera przez nginx, WebUI Huawei wpada w **nieskończoną pętlę
przeładowań** — WebUI wysyła token CSRF w nagłówku `__RequestVerificationToken`, a nginx
domyślnie wycina nagłówki z podkreśleniami. Naprawa:

```nginx
server {
    listen 80;
    underscores_in_headers on;          # kluczowe — inaczej pętla przeładowań
    location / {
        proxy_pass http://192.168.0.1/;
        proxy_set_header Host 192.168.0.1;      # CSRF Huawei sprawdza Host/Referer
        proxy_set_header Referer http://192.168.0.1/;
        proxy_set_header Origin http://192.168.0.1;
    }
}
```

## Powiadomienia SMS → Pushover (systemd timer)

`scripts/sms_to_pushover.py` pobiera nieprzeczytane SMS-y (od najstarszej), wysyła każdą
jako powiadomienie [Pushover](https://pushover.net/api#messages) i **dopiero po potwierdzeniu**
(HTTP 200 + `status:1`) oznacza wiadomość jako przeczytaną. Wiadomości, których nie uda się
wysłać lub potwierdzić, pomija (bez oznaczania) i przechodzi do kolejnej — pominięte pozostają
nieprzeczytane i zostaną ponowione przy następnym uruchomieniu.

Powiadomienie: tytuł „SMS od {nadawca}", treść = tekst SMS-a, a data odbioru SMS-a trafia
do pola `timestamp` (Pushover pokaże i posortuje wg rzeczywistego czasu nadejścia).

Dopisz do `.env`:

```ini
PUSHOVER_TOKEN=token_aplikacji_pushover
PUSHOVER_USER=klucz_uzytkownika_pushover
```

### Pomijanie wybranych nadawców (ignorelista)

Opcjonalnie możesz wskazać nadawców, których wiadomości mają **nie** trafiać do Pushover
(np. alerty RCB czy SMS-y marketingowe). Ustaw zmienną `SMS_IGNORE_SENDERS` — listę
rozdzieloną przecinkami. Dopasowanie działa po polu `Phone` wiadomości, czyli po
**numerze telefonu** albo — dla wiadomości bez numeru — po **nazwie nadawcy**
(np. `ALERT RCB`). Wielkość liter nie ma znaczenia.

```ini
SMS_IGNORE_SENDERS=ALERT RCB
# kilku nadawców naraz:
# SMS_IGNORE_SENDERS=ALERT RCB,+48555111222,InfoSMS
```

Wiadomości od zignorowanych nadawców są oznaczane jako przeczytane (bez powiadomienia),
żeby nie zapełniały skrzynki routera i nie były przetwarzane przy każdym uruchomieniu.

Uruchamiane cyklicznie przez **timer systemd** (co 1 minutę). Dwa pliki:

`/etc/systemd/system/sms-pushover.service`:

```ini
[Unit]
Description=Przekazywanie nieprzeczytanych SMS do Pushover
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/srv/huawei-b715-gateway
ExecStart=/srv/huawei-b715-gateway/venv/bin/python scripts/sms_to_pushover.py --quiet
TimeoutStartSec=60
NoNewPrivileges=true
```

`/etc/systemd/system/sms-pushover.timer`:

```ini
[Unit]
Description=Uruchamiaj SMS->Pushover co minute

[Timer]
OnCalendar=*:0/1
AccuracySec=10s

[Install]
WantedBy=timers.target
```

Włączenie i podgląd:

```bash
systemctl enable --now sms-pushover.timer
systemctl list-timers sms-pushover.timer     # kiedy następny / ostatni przebieg
journalctl -u sms-pushover -f                # logi + czas wykonania każdego przebiegu
```

`Type=oneshot` nie pozwala nałożyć się dwóm przebiegom (zastępuje `flock`), a `TimeoutStartSec=60` ubija ewentualny zwis (np. gdy router przestanie odpowiadać na żądanie bez timeoutu) — inaczej proces mógłby wisieć w nieskończoność, blokując kolejne uruchomienia. Z flagą `--quiet` skrypt na sukces nie wypisuje nic; bez niej pokazuje podsumowanie (`Wysłano i oznaczono: X; nieudane: Y`). Czas wykonania i exit code każdego przebiegu są w `journalctl -u sms-pushover`.

## Restart routera (`scripts/reboot_router.py`)

Router co ~1,5–2 dni wpada w stan, w którym odrzuca **nowe** połączenia z sieci lokalnej
(ping, TCP, DNS — także do samego `192.168.8.1`) w rytmie ~5 min przerwy co ~10 min; już
zestawione połączenia przeżywają. Restart routera usuwa problem, więc skrypt można odpalać
ręcznie, a profilaktycznie — co noc z timera systemd.

Skrypt loguje się do routera i wysyła restart **typowaną** metodą biblioteki
(`device.set_control(ControlModeEnum.REBOOT)` = `Control=1`).

> ⚠️ **`Control=2` to RESET FABRYCZNY** (router zgubiłby konfigurację Wi-Fi całej lokalizacji).
> Skrypt nigdy nie przyjmuje wartości `Control` z zewnątrz i nie woła `post_set('device/control', …)`
> ręcznie — wysyła wyłącznie `REBOOT`.

**Ponawianie do skutku:** pojedyncza próba zawodzi dokładnie wtedy, gdy restart jest potrzebny
(router nie przyjmuje nowych połączeń), więc skrypt ponawia próbę co `REBOOT_RETRY_INTERVAL`
(dom. 20 s) maksymalnie przez `REBOOT_MAX_DURATION` (dom. 900 s). Ponawia przy błędach
połączenia/timeoutach oraz przejściowych błędach logowania/sesji (zajęta sesja przez bramkę,
`system busy`, nadmiar logowań, błędy CSRF/tokenu). **Nie** ponawia przy błędnych danych
logowania — to błąd konfiguracji. Sukces = router odpowiedział `OK`; wtedy kończy natychmiast.

**Kody wyjścia:** `0` = restart wysłany (lub `--dry-run` zakończony powodzeniem);
`≠0` = porażka (`2` = błędne dane logowania, `1` = wyczerpane próby / inna porażka).

**`--dry-run`** loguje się do routera (z tą samą pętlą ponowień) i czyta `device.information()`,
ale **nie** restartuje — do bezpiecznego testu konfiguracji i łączności.

Zmienne `.env` (wszystkie opcjonalne; reszta — `ROUTER_*`, `PUSHOVER_*`,
`ROUTER_CONNECT_TIMEOUT`/`ROUTER_READ_TIMEOUT` — jak w pozostałych narzędziach):

```ini
REBOOT_RETRY_INTERVAL=20    # odstęp między próbami (s)
REBOOT_MAX_DURATION=900     # maks. łączny czas ponawiania (s)
REBOOT_NOTIFY_SUCCESS=1     # 1/0: powiadomienie Pushover o sukcesie (porażkę zgłasza zawsze)
```

Powiadomienia Pushover używają tych samych `PUSHOVER_TOKEN`/`PUSHOVER_USER` co SMS-y; gdy nie są
ustawione, skrypt działa normalnie (bez powiadomień). Błąd wysyłki Pushovera nie zmienia kodu wyjścia.

### Restart z timera systemd (oneshot, w nocy)

Dwa pliki — analogicznie do [SMS → Pushover](#powiadomienia-sms--pushover-systemd-timer).

`/etc/systemd/system/huawei-reboot.service`:

```ini
[Unit]
Description=Nocny restart routera Huawei B715
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/srv/huawei-b715-gateway
ExecStart=/srv/huawei-b715-gateway/venv/bin/python scripts/reboot_router.py
# musi być WIĘKSZE niż REBOOT_MAX_DURATION (dom. 900 s), żeby nie ubić pętli ponowień
TimeoutStartSec=960
NoNewPrivileges=true
```

`/etc/systemd/system/huawei-reboot.timer`:

```ini
[Unit]
Description=Uruchamiaj restart routera co noc

[Timer]
OnCalendar=*-*-* 04:15:00
AccuracySec=1m
# Persistent=false (domyślne): NIE odrabiaj pominiętego przebiegu. Gdyby host był
# wyłączony o 04:15, Persistent=true odpaliłby zaległy restart zaraz po starcie hosta —
# czyli zrestartowałby router w środku dnia. Lepiej pominąć i poczekać do kolejnej nocy.
Persistent=false

[Install]
WantedBy=timers.target
```

Włączenie i podgląd:

```bash
systemctl enable --now huawei-reboot.timer
systemctl list-timers huawei-reboot.timer    # kiedy następny / ostatni przebieg
journalctl -u huawei-reboot -f               # logi + czas wykonania i exit code
```

`Type=oneshot` nie pozwala nałożyć się dwóm przebiegom, a `TimeoutStartSec=960` (> `REBOOT_MAX_DURATION`)
daje pętli ponowień dokończyć pracę, zanim systemd ubije proces. Ręczny restart poza timerem:
`systemctl start huawei-reboot.service` (albo bezpośrednio skryptem — patrz wyżej).

## Uwagi bezpieczeństwa

- Serwer (metryki i REST API) **nie ma uwierzytelniania** — a API potrafi kasować SMS-y. Ogranicz `HTTP_ADDR`
  do zaufanego interfejsu (np. adresu Tailscale/VPN) albo postaw przed nim reverse proxy z autoryzacją.
- `.env` zawiera hasło do routera — trzymaj go poza repozytorium (jest w `.gitignore`).

## Licencja

[MIT](LICENSE) © miisieq

Projekt korzysta z biblioteki [`huawei-lte-api`](https://github.com/Salamek/huawei-lte-api)
(licencja LGPL-3.0) jako zależności — nie wpływa to na licencję tego kodu.
