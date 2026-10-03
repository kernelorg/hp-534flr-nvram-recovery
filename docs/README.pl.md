# Odzyskiwanie NVRAM HP 534FLR-SFP+ (BCM57810)

[English](../README.md) · **Polski** · [Українська](README.uk.md) · [Русский](README.ru.md)

Narzędzia i instrukcje do odzyskiwania kart sieciowych HP FlexFabric 10Gb 2-port **534FLR-SFP+** (Broadcom/QLogic **BCM57810**, `14e4:168e`, subsystem `103c:1930`), w których pamięć SPI flash zawiera niespójny zestaw firmware. Metoda działa także dla innych kart na BCM57810/bnx2x o takim samym układzie NVRAM.

Zawartość repozytorium:

| Ścieżka | Zawartość |
|---|---|
| `firmware/esp-serprog/` | Firmware zamieniający **ESP32 / ESP8266** w programator SPI dla [flashrom](https://flashrom.org) (protokół `serprog`) |
| `tools/nvmtool.py` | Narzędzie do obrazów: analiza zrzutu, szablon bez danych karty, budowa obrazu dla konkretnej karty (Python 3, bez zależności) |
| `tools/bnx2x_nvm.py` | Dostęp do NVRAM bezpośrednio z Linuksa przez PCI BAR0 karty: log MCP, odczyt, zapis (bez wylutowywania) |
| `templates/` | Zanonimizowany szablon dla 534FLR-SFP+ (firmware 7.14.62, bez danych karty) |

> Szablon w `templates/` nie zawiera danych karty (MAC, numer seryjny). Oryginalne zrzuty prawdziwych kart nie są tu przechowywane: każdy zawiera numer seryjny i adresy MAC konkretnej karty. Zob. [Skąd wziąć szablon](#5-skąd-wziąć-szablon).

---

## 1. Objawy

Karta nie jest widoczna w iLO ani w systemie, a w `dmesg` widać:

```
bnx2x: [bnx2x_init_shmem:...]BAD MCP validity signature
bnx2x 0000:05:00.0: part number 394D4342-31383735-31543030-47303030
bnx2x: [bnx2x_get_igu_cam_info:...]CAM configuration error
bnx2x 0000:05:00.0: probe with driver bnx2x failed with error -22
pci 0000:05:00.0: VPD access failed.  This is likely a firmware bug on this device.
```

Procesor zarządzający (MCP) w BCM57810 uruchamia bootcode z pamięci flash, ale nie kończy inicjalizacji. Przyczynę pokazuje jego własny bufor śledzenia (`bnx2x_nvm.py trace`, rozdział 3):

```
MFW1 7.16.15 Begin, TS 0x965d72 SI 0x8020458.
...
no img 0x30000003
load failure
```

W przypadku, od którego zaczął się ten projekt, kartę uszkodziła **przerwana aktualizacja firmware HPE**. Narzędzie *HPE QLogic NX2 Online Firmware Upgrade Utility* (CP064333, MBI 7.14.79 → 7.19.27) zapisało nowy bootcode 7.16.15, usunęło stare moduły MCP i zakończyło się błędem (`Return code: 7`), zanim zapisało nowe. CRC wszystkich obrazów w pamięci pozostały poprawne, ale bootcode nie znalazł wymaganego zestawu modułów. `nvmtool.py info` wykrywa taką sytuację. Rozwiązaniem jest zapisanie pełnego, spójnego zestawu firmware z działającej karty z zachowaniem adresów MAC i numeru seryjnego własnej karty.

> ⚠️ Na tym samym serwerze (Debian 13 / Proxmox VE, jądro 7.0 — system nieobsługiwany) ta aktualizacja po raz drugi uszkodziła kartę w ten sam sposób. Po odzyskaniu karty **nie uruchamiaj** tam ponownie tego komponentu firmware NX2.

Jeśli karta jest nadal widoczna na magistrali PCI, można ją odzyskać **bezpośrednio z Linuksa, bez wylutowywania pamięci** (rozdział 3). W przeciwnym razie kość trzeba wylutować i zaprogramować zewnętrznym programatorem (rozdział 4).

## 2. Pamięć flash M45PE16

- Oznaczenie: **4SPE16**, ST/Micron **M45PE16**, 16 Mbit (2 MB), obudowa SO8W (208 mil), 2,7–3,6 V.
- JEDEC ID: `0x20 0x40 0x15`.

⚠️ **Rozkład wyprowadzeń M45PE16 NIE jest standardowy dla serii 25.** Standardowe adaptery SOIC8, klipsy i podstawki są dla tej kości podłączone błędnie:

```
          ┌───────┐
    D   1 │●      │ 8  Q       D  = wejście danych (MOSI)  Q   = wyjście danych (MISO)
    C   2 │       │ 7  VSS     C  = zegar (SCK)            VSS = masa
 Reset  3 │       │ 6  VCC     S  = wybór układu (CS#)     VCC = 3,3 V
    S   4 │       │ 5  W       W  = ochrona zapisu         Reset = reset, aktywny 0
          └───────┘
```

Reset (pin 3) **musi** być podciągnięty do 3,3 V, inaczej kość pozostaje w stanie resetu i odczytuje się jako `0xFF`/`0x00`.

Ponadto rodzina M45PE **nie ma polecenia kasowania całej pamięci** (Bulk/Chip Erase), a jedynie Page Erase `0xDB` i Sector Erase `0xD8`. Od tego zależy wybór programatora.

## 3. Odzyskiwanie z Linuksa bez wylutowywania

Jeśli `lspci -nn -d 14e4:168e` pokazuje kartę, ale sterownik się nie ładuje, NVRAM można odczytywać i zapisywać przez własny interfejs NVRAM karty w PCI BAR0, tą samą sekwencją rejestrów, której używa sterownik bnx2x. `tools/bnx2x_nvm.py` wymaga tylko Pythona 3 i uprawnień root. Funkcja nie może być przypisana do sterownika; po nieudanym probe nie jest.

```
B=0000:05:00.0                                    # adres PCI funkcji 0

python3 tools/bnx2x_nvm.py $B trace               # stan MCP i jego log: dlaczego się zatrzymał
python3 tools/bnx2x_nvm.py $B read backup1.bin    # cała NVRAM 2 MB, ok. 2 s
python3 tools/bnx2x_nvm.py $B read backup2.bin
cmp backup1.bin backup2.bin
python3 tools/nvmtool.py info backup1.bin
```

Zbuduj obraz dla swojej karty (rozdział 6) i zapisz go:

```
python3 tools/bnx2x_nvm.py $B testpage 0x180000   # test zapisu jednej strony, potem jej przywrócenie
python3 tools/bnx2x_nvm.py $B write my_card_new.bin
python3 tools/bnx2x_nvm.py $B read check.bin
sha256sum my_card_new.bin check.bin               # muszą być identyczne
```

- `write` zapisuje ponownie tylko różniące się strony po 256 bajtów (zwykle kilkaset, to sekundy), a potem odczytuje i porównuje całą pamięć.
- Kontroler NVRAM karty sam kasuje każdą stronę, więc M45PE16 daje się zapisać, choć TL866II+ tego nie potrafi.
- Po zapisie **wyłącz serwer i odłącz zasilanie na 30–60 s**. MCP działa na zasilaniu dyżurnym i wczytuje bootcode ponownie dopiero po pełnym odłączeniu zasilania.
- Jeśli zapis zostanie przerwany, karta może zniknąć z magistrali PCI; wtedy użyj zewnętrznego programatora (rozdział 4) i kopii zapasowej.

## 4. Programatory

### 4.1 TL866II+ (minipro): tylko odczyt

TL866II+ potrafi kość **odczytać**, ale **nie potrafi jej zapisać**. Jego firmware kasuje pamięci SPI wyłącznie poleceniem Bulk Erase (`0xC7`), które M45PE16 ignoruje. minipro mimo to wypisuje `Erasing... OK`, następnie zapisuje dane na nieskasowaną pamięć i weryfikacja kończy się błędem. W kości zostaje `stare AND nowe`. Profil `M45PE16` w bazie minipro jest zdefiniowany tylko dla T48/T56. Dla TL866II+ nie ma algorytmu, a aktualizacja firmware programatora go nie dodaje.

Do odczytu na TL866II+ nóżki kości łączy się z tymi stykami ZIF, które zajmowałaby standardowa pamięć SOIC8 serii 25 (góra podstawki, od strony dźwigni):

| Pin M45PE16 | Sygnał | Styk ZIF | (pozycja standardowej pamięci) |
|---|---|---|---|
| 4 | S | 1 | CS |
| 8 | Q | 2 | DO |
| 5 | W | 3 | WP |
| 7 | VSS | 4 | GND |
| 1 | D | 37 | DI |
| 2 | C | 38 | CLK |
| 3 | Reset | 39 | HOLD (w stanie „1") |
| 6 | VCC | 40 | VCC (3,3 V) |

```
minipro -p "M45PE16@SOIC8" -D            # oczekiwane: Chip ID: 0x204015  OK
minipro -p "M45PE16@SOIC8" -r dump.bin
```

### 4.2 ESP32 / ESP8266 + flashrom: odczyt i zapis ✅

flashrom obsługuje M45PE16 (łącznie z kasowaniem sektorów) i komunikuje się z ESP przez USB-UART protokołem `serprog`.

#### Kompilacja i wgrywanie firmware ESP

Wymagany jest [PlatformIO](https://platformio.org):

```
cd firmware/esp-serprog
pio run -e esp32   -t upload --upload-port /dev/ttyUSB0     # ESP32 DevKit
pio run -e esp8266 -t upload --upload-port /dev/ttyUSB0     # NodeMCU / Wemos D1 mini
```

Sprawdzenie, czy flashrom widzi programator (pamięci jeszcze nie podłączać):

```
flashrom -p serprog:dev=/dev/ttyUSB0:921600
# serprog: Programmer name is "esp-serprog"
```

Ostrzeżenie `Warning: NAK to query serial buffer size` jest oczekiwane i nieszkodliwe.

#### Podłączenie (bezpośrednio do nóżek kości)

| Pin M45PE16 | Sygnał | ESP32 | ESP8266 (NodeMCU) |
|---|---|---|---|
| 1 | D (MOSI) | GPIO23 | D7 (GPIO13) |
| 2 | C (SCK) | GPIO18 | D5 (GPIO14) |
| 3 | Reset | **3V3** | **3V3** |
| 4 | S (CS) | GPIO5 | D1 (GPIO5) |
| 5 | W | **3V3** | **3V3** |
| 6 | VCC | **3V3** | **3V3** |
| 7 | VSS | GND | GND |
| 8 | Q (MISO) | GPIO19 | D6 (GPIO12) |

- Zasilanie **tylko z 3V3**, nigdy z 5V/VIN.
- Krótkie przewody (≤ 10–15 cm). Domyślny zegar SPI 4 MHz (maksymalnie 8 MHz).
- Podłączaj przewody przy odłączonym USB.
- Wersja dla ESP32 została przetestowana na prawdziwym sprzęcie z M45PE16. Wersja dla ESP8266 się kompiluje, ale nie była testowana na sprzęcie.

#### Odczyt i zapis

```
P="serprog:dev=/dev/ttyUSB0:921600"

flashrom -p $P -c M45PE16                       # wykrycie: Found ... "M45PE16"
flashrom -p $P -c M45PE16 -r backup1.bin        # odczyt (≈30 s)
flashrom -p $P -c M45PE16 -r backup2.bin
cmp backup1.bin backup2.bin && echo "odczyt stabilny"

flashrom -p $P -c M45PE16 -w new.bin            # kasowanie + zapis + weryfikacja (≈3 min)
flashrom -p $P -c M45PE16 -r check.bin
sha256sum new.bin check.bin                     # muszą być identyczne
```

Jeśli przy wykrywaniu pojawia się `id1 0xff` (MISO podciągnięte, kość milczy), sprawdź zasilanie i Reset na pinach 6 i 3 oraz okablowanie. Jeśli również TL866 odczytuje `0xff`, prawie zawsze winne jest błędne podłączenie.

## 5. Skąd wziąć szablon

Potrzebny jest pełny, 2-megabajtowy zrzut NVRAM z **działającej** karty tego samego modelu (ten sam PCI subsystem ID: `103c:1930` dla 534FLR-SFP+). Z działającej karty w serwerze z Linuksem:

```
ethtool -e <iface> raw on > working_dump.bin      # 2097152 bajtów
```

Można też odczytać jej pamięć flash programatorem, jak opisano wyżej.

### Gotowy szablon

Repozytorium zawiera gotowy szablon `templates/534FLR-SFP+_fw7.14.62_template.bin`. Jest to zanonimizowany zrzut działającej karty 534FLR-SFP+ (`103c:1930`): bootcode 7.14.62 z pełnym, spójnym zestawem modułów MCP (7.14.62/7.14.37), MBA 7.14.10. Wszystkie sumy kontrolne są poprawne. Dane karty zastąpiono wartościami zastępczymi: MAC `02:4e:56:4d:00:00`…`07`, SN `XXXXXXXXXX`, kod daty `0000`. Integralność sprawdza się poleceniem `sha256sum -c templates/SHA256SUMS`.

> Szablon zawiera własnościowy firmware HP/QLogic. Udostępniany jest wyłącznie w celu odzyskania własnego sprzętu.

### Jak zrobić własny szablon

Zamień zrzut działającej karty na szablon bez jej danych:

```
python3 tools/nvmtool.py sanitize working_dump.bin -o template.bin
# MAC -> 02:4e:56:4d:00:00, SN -> XXXXXXXXXX, kod daty -> 0000
```

## 6. Budowa obrazu dla własnej karty

Najpierw koniecznie odczytaj i zachowaj oryginalną zawartość swojej kości (rozdziały 3–4), a potem ją przeanalizuj:

```
python3 tools/nvmtool.py info my_card_original.bin
```

`info` sprawdza sygnaturę, wszystkie bloki konfiguracyjne chronione CRC, katalog główny i rozszerzony oraz każdy obraz. Wypisuje VPD, PCI ID i adresy MAC, a jeśli bootcode i moduły MCP pochodzą z różnych wydań, wyświetla ostrzeżenie.

Budowa obrazu z danymi własnej karty:

```
# MAC, numer seryjny i kod daty pobierane ze zrzutu tej samej karty
python3 tools/nvmtool.py build template.bin --from-dump my_card_original.bin -o my_card_new.bin

# albo podawane jawnie (np. z naklejki na karcie)
python3 tools/nvmtool.py build template.bin --mac aa:bb:cc:dd:ee:00 --serial CN0000ABCD --date-code 1234 -o my_card_new.bin
```

Co robi `build`:

- zastępuje 8 kolejnych adresów MAC (2 porty × 4 funkcje Flex-10) wszędzie, gdzie występują: manuf_info obu portów i tablica multi-function (wpisy Ethernet, iSCSI i FCoE);
- zapisuje w VPD karty numer seryjny (SN), MAC (V4) i opcjonalnie kod daty (V2), przelicza sumę kontrolną VPD (RV);
- przelicza CRC wszystkich zmienionych bloków i przed zapisem weryfikuje cały obraz.

`--mac` to bazowy adres MAC: port 0, funkcja 0, najniższy z ośmiu (pole VPD V4). Numer seryjny musi mieć taką samą długość jak w szablonie (u HP — 10 znaków).

Wersje firmware i konfiguracja pochodzą z szablonu. Dlatego pola wersji V1/V3/V6 w VPD opisują firmware szablonu i tak właśnie powinno być.

## 7. Pełna procedura odzyskiwania

1. Dwukrotnie wykonać kopię pamięci i porównać: z Linuksa przez `bnx2x_nvm.py read` (rozdział 3) albo programatorem po wylutowaniu (rozdział 4).
2. `nvmtool.py info backup.bin` i `bnx2x_nvm.py trace` — potwierdzić diagnozę.
3. Użyć gotowego szablonu albo zrobić własny z działającej karty (`sanitize`), a następnie `nvmtool.py build ... --from-dump backup.bin`.
4. Zapisać obraz: `bnx2x_nvm.py write` z systemu albo przez ESP + flashrom. Odczytać ponownie i porównać SHA-256.
5. Jeśli kość była wylutowana, wlutować ją z powrotem (pin 1 — kropka, zgodnie z oznaczeniem na płytce).
6. Odłączyć zasilanie serwera na 30–60 s, potem włączyć.
7. W `dmesg | grep -iE 'bnx2x|MCP'` nie może być `BAD MCP validity signature`, a oba porty muszą pojawić się z twoimi adresami MAC.

Firmware aktualizuj później wyłącznie narzędziami HPE na obsługiwanym systemie, nigdy przez `ethtool -f`, i przed każdą aktualizacją rób zrzut pamięci.

## Zastrzeżenie

Używasz na własne ryzyko. Błędny obraz, napięcie lub podłączenie może trwale uszkodzić kość flash lub kartę. Przechowuj oryginalny zrzut swojej kości.

HP, HPE, FlexFabric, Broadcom i QLogic są znakami towarowymi ich właścicieli. Projekt nie jest z nimi powiązany.

## Licencja

[MIT](../LICENSE)
