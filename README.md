# Oljan – autonom bevaknings- och analysmotor för råolja (WTI/Brent)

Oljan är en resilient Python-daemon som körs dygnet runt och hjälper en aktiv
**intradagstrader** att **agera före marknaden**. Den är byggd för korta
tidsramar (1m/5m/15m + 1h som kontext) och:

- övervakar oljepriset på **flera tidsramar samtidigt** och beräknar
  chart-kontext (candlesticks, volym, RSI, MACD, EMA, Bollinger, ATR,
  stöd/motstånd) samt trend-samsyn mellan tidsramarna (MTF-confluence),
- samlar kontinuerligt in nyheter, officiella rapporter (EIA) och social
  media (Reddit, Stocktwits) från gratis/öppna källor,
- bedömer varje relevant händelse: **substans vs. brus/manipulation**,
  riktning (hausse/baisse) och storlek,
- kör en **tidsseriekorrekt historisk analys** (event study) som svarar
  "hur betedde sig priset efter liknande händelser förr?",
- pushar en **notis i nära realtid via ntfy** (eller Telegram) med rå nyhet +
  djup analys + chart-bild + konkreta, hävstångsmedvetna åtgärdsförslag,
- är alltid **transparent**: konfidensnivå, källor, matchade nyckelord och
  osäkerheter anges i varje notis.

> ⚠️ Oljan är **beslutsstöd, inte finansiell rådgivning** och lägger inga
> ordrar. Du fattar och utför alla beslut själv.

---

## Innehåll

1. [Arkitektur](#arkitektur)
2. [Designprinciper & skydd mot bias](#designprinciper--skydd-mot-bias)
3. [Installation](#installation)
4. [Gratis nycklar (steg för steg)](#gratis-nycklar-steg-för-steg)
5. [Konfiguration](#konfiguration)
6. [Köra & testa](#köra--testa)
7. [Köra 24/7](#köra-247)
8. [Hur analysen fungerar](#hur-analysen-fungerar)
9. [Utöka systemet](#utöka-systemet)
10. [Felsökning](#felsökning)

---

## Arkitektur

```
                         ┌─────────────────────────────────────────┐
                         │                daemon.py                 │
                         │  resilient scheduler (per-task backoff,  │
                         │  graceful shutdown, heartbeat)           │
                         └───────────────┬──────────────────────────┘
          ┌───────────────┬──────────────┼───────────────┬───────────────┐
          ▼               ▼              ▼               ▼               ▼
   market_data.py    collectors/     events.py     historical.py     notifier.py
   (yfinance) ──┐    rss/eia/reddit/  relevans +    event study /     Telegram /
   candlestick  │    stocktwits/      kategori +    analoga fall      console
   + volym      │    newsapi          substans vs   (no look-ahead)   + charting.py
                │        │            manipulation        │
                ▼        ▼                 │               │
             indicators.py           sentiment.py          │
             RSI/MACD/EMA/BB/ATR      oil-riktat lexikon    │
             stöd & motstånd         (VADER sekundär)       │
                │                          │                │
                └──────────────┬───────────┴────────────────┘
                               ▼
                          analysis.py  ("hjärnan")
              chart + händelse + historik → rekommendation
              (hävstångsmedveten, med konfidens/källor/osäkerheter)
                               │
                               ▼
                          storage.py (SQLite)
              candles · events · outcomes · dedup · notiser
```

**Modulöversikt**

| Modul | Ansvar |
|-------|--------|
| `config.py` | Läser YAML + hemligheter från `.env` |
| `logging_setup.py` | Roterande fil- + konsolloggning |
| `storage.py` | SQLite: sparar candles (så historik ackumuleras bortom API:ets fönster), events, utfall, dedup, notiser |
| `providers.py` | Pluggbara prisdatakällor: Yahoo chart-JSON via `requests` (nyckelfritt, default), yfinance valfritt |
| `market_data.py` | Multi-tidsram-hämtning (1m/5m/15m/1h), retry/backoff, rate-limit-skydd |
| `indicators.py` | Tekniska indikatorer + stöd/motstånd via swing-pivots |
| `sentiment.py` | Olje-*riktat* lexikon (bull/bear för priset), VADER sekundärt |
| `collectors/` | Pluggbara källor: RSS, EIA, Reddit, Stocktwits, NewsAPI |
| `events.py` | Relevans, kategori, substans- vs. manipulationspoäng |
| `historical.py` | Event study / analoga fall, tidsseriekorrekt |
| `analysis.py` | Kombinerar allt → rekommendation + notistext |
| `charting.py` | Renderar candlestick-bild (matplotlib, headless) |
| `notifier.py` | ntfy/Telegram/konsol, dedup, tysta timmar, heartbeat |
| `daemon.py` | 24/7-loop, felhantering, återhämtning |

---

## Designprinciper & skydd mot bias

Systemet prioriterar **enkelhet, tolkningsbarhet och robusthet** framför
komplexa svartlådemodeller. Konkreta skydd:

- **Ingen look-ahead-bias.** En händelses framåtavkastning beräknas *enbart*
  från candles som spelats in *efter* händelsens tidsstämpel, och först när
  horisonten (1/2/4/24h) faktiskt har passerat. Händelsen markeras då som
  "matured". Se `historical.py` och `storage.analog_outcomes`.
- **Inget data-läckage.** Analoga fall matchas endast på egenskaper kända vid
  händelsetillfället (kategori + riktning). Den aktuella händelsen exkluderas
  alltid från sitt eget analog-underlag.
- **Ingen overfitting.** Historiken är rent *deskriptiv* statistik
  (träfffrekvens, median, kvartiler) – inga parametrar tränas, alltså finns
  inget att överanpassa. Urvalsstorleken rapporteras alltid och styr
  konfidensnivån.
- **Dedup** hindrar att samma story räknas flera gånger och blåser upp basraten.
- **Allt i UTC** internt för att undvika tidszonsfel.
- **Transparens.** Varje notis visar substans/manipulationspoäng med dess
  delfaktorer, matchade nyckelord, källa, konfidens och osäkerheter.

---

## Installation

Kräver Python 3.10+.

```bash
git clone <detta-repo> oljan && cd oljan

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

> Om `feedparser` inte går att installera i din miljö fungerar systemet ändå:
> RSS-insamlaren faller automatiskt tillbaka på en inbyggd stdlib-parser.

Kopiera exempelfilerna:

```bash
cp config.example.yaml config.yaml
cp .env.example .env
```

---

## Gratis nycklar (steg för steg)

Systemet **fungerar utan nycklar** (prisdata via Yahoo + RSS + Stocktwits).
Push-notiser via **ntfy kräver ingen nyckel alls**. För full funktion:

### 1. ntfy – push-notiser (rekommenderat, inget konto behövs)
1. Installera **ntfy**-appen (iOS/Android) eller använd webben på
   https://ntfy.sh.
2. Välj ett **långt, svårgissat topic-namn** (ntfy-topics är publika utifrån
   namnet – behandla det som ett lösenord). Exempel: `oljan-9f3a1c7b2e5d8a04`.
3. Sätt det i `config.yaml` under `notifications.ntfy.topic` och
   `notifications.channel: "ntfy"`.
4. **Prenumerera på exakt samma topic** i appen (Subscribe → skriv topic-namnet).
5. Klart – notiser dyker upp direkt på telefonen. (Vill du skydda topicet med
   inloggning: skapa konto på ntfy.sh, sätt token i `.env` som `NTFY_TOKEN`.)

### 2. Telegram (alternativ push-kanal)
1. Öppna Telegram, sök upp **@BotFather**, skicka `/newbot`, följ stegen.
2. Kopiera **bot-token** → `TELEGRAM_BOT_TOKEN` i `.env`.
3. Skicka valfritt meddelande till din nya bot.
4. Öppna `https://api.telegram.org/bot<DIN_TOKEN>/getUpdates` i webbläsaren,
   leta upp `"chat":{"id":...}` → sätt `TELEGRAM_CHAT_ID` i `.env`.
5. I `config.yaml`: `notifications.channel: "telegram"`.

### 3. EIA (rekommenderat – officiell lagerstatistik)
1. Registrera gratis nyckel: https://www.eia.gov/opendata/register.php
2. `EIA_API_KEY` i `.env`, `eia.enabled: true` i `config.yaml`.
   EIA:s veckorapport (onsdagar) är en av de mest prisdrivande händelserna.

### 4. Reddit (valfritt – social signal)
1. https://www.reddit.com/prefs/apps → **create app** → typ **script**.
2. `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` i `.env`,
   `social.reddit_enabled: true`.

### 5. NewsAPI (valfritt – kompletterande nyheter)
1. Gratis dev-nyckel: https://newsapi.org (100 anrop/dygn).
2. `NEWSAPI_KEY` i `.env`, `news.newsapi_enabled: true`.

### 6. X / Twitter – underrättelsekällor (keylöst via Nitter)
X-innehåll hämtas **nyckelfritt via Nitter-RSS** för en kurerad lista av konton
som bevisligen är *tidiga* på oljerelevant information (`social.x_accounts`):
- **Headline-reläer** (Bloomberg/Reuters-terminalens hastighet): `DeItaone`
  (Walter Bloomberg), `FirstSquawk`, `LiveSquawk`, `financialjuice`.
- **Fysisk olje-underrättelse**: `TankerTrackers` (satellit/AIS-spårning av
  tankfartyg – tidiga på verkliga leveransstörningar).
- **Geopolitik/OSINT**: `sentdefender`, `Faytuks`, `WarMonitors`, `spectatorindex`.

Länkar skrivs om till kanoniska `x.com`-länkar. Varje X-konto har en egen
källvikt i `classification.source_weights` (reläer/tankertrackers högre, OSINT
lägre). Brus filtreras bort (retweets, fragment, koordinat-/sifferdumpar). Har
du officiell X API v2-åtkomst: sätt `X_BEARER_TOKEN` i `.env` så används den.
Nitter-instanser kan ligga nere – flera anges i `social.x_nitter_instances`
och systemet växlar mellan dem. Stäng av med `social.x_enabled: false`.

> Varje notis visar **exakt länk**, **publiceringstid (UTC)** och **latens**
> (hur länge sedan uppgiften publicerades → upptäcktes). Latensen avslöjar t.ex.
> när en "Breaking"-post egentligen är dagar gammal.

---

## Konfiguration

All konfiguration ligger i `config.yaml` (hemligheter i `.env`). Viktiga delar:

- `instruments` – vilka symboler som bevakas (WTI `CL=F`, Brent `BZ=F`).
- `market_data.timeframes` – **intradags-tidsramarna** som hämtas/analyseras
  (1m/5m/15m/1h). `analysis_timeframe` (default `5m`) är den primära tidsramen
  för notis-chart, stöd/motstånd och event-study-horisonter. Yahoo-gränser:
  1m→7d, 5m/15m→60d, 1h→730d historik.
- `historical.horizons_hours` – intradagsanpassade horisonter, default
  `[0.25, 0.5, 1, 2, 4]` (15m, 30m, 1h, 2h, 4h).
- `position` – **din position** (`side`, `leverage`, valfritt `entry_price`)
  så att rekommendationerna blir hävstångsmedvetna (t.ex. x10 long).
- `relevance.keywords` – nyckelord och deras vikt (styr vad som är "relevant").
- `directional_lexicon` – fras → **prispåverkan** (hausse `+`, baisse `-`).
  Notera skillnaden mot vanlig sentiment: "war" är negativ ton men **bullish**
  för olja; "ceasefire" är positiv ton men **bearish**.
- `classification` – källvikter, trösklar för substans/manipulation.
- `historical.horizons_hours` – vilka framåthorisonter som studeras.
- `notifications` – kanal, tysta timmar, dedup, heartbeat, tröskel för push.

Se `config.example.yaml` för alla fält med kommentarer.

---

## Köra & testa

**Enhetstester** (indikatorer + tidsseriekorrekt event study):

```bash
pip install pytest
pytest -q
```

**Selftest** – verifierar dataåtkomst, insamlare och notiskanal, och skickar
ett testmeddelande:

```bash
python -m oiltrader --config config.yaml --selftest
```

**Ett enda pass** (bra för cron eller felsökning):

```bash
python -m oiltrader --config config.yaml --once
```

**Kontinuerlig drift** (förgrund):

```bash
python -m oiltrader --config config.yaml
```

Loggar skrivs till konsol och `data/logs/oljan.log` (roterande).

---

## Köra 24/7

> **Var kör man?** En riktig 24/7-drift behöver en dator som alltid är på.
> Bra gratis/billiga alternativ:
> - **Oracle Cloud "Always Free"** – en liten VM som är gratis för alltid
>   (räcker gott för Oljan) och alltid påslagen.
> - **Raspberry Pi** hemma – engångskostnad, drar minimalt med ström.
> - **Billig VPS** (Hetzner/Netcup m.fl.) för några €/mån.
>
> ntfy behöver ingen nyckel, så när du väl klonat repot och fyllt i ditt
> topic i `config.yaml` är det bara att starta enligt nedan. Kör på en
> residential- eller VPS-IP – delade moln-IP:n kan bli rate-limitade av Yahoo;
> höj då `market_data.refresh_seconds`/`request_spacing` vid behov.

### Alternativ A: systemd (rekommenderat på en VPS / Oracle Free / Pi)

En liten VM (t.ex. 1 vCPU/1 GB) räcker gott.

```bash
sudo useradd -r -m -d /opt/oljan oljan
sudo cp -r . /opt/oljan && cd /opt/oljan
sudo -u oljan python3 -m venv .venv
sudo -u oljan .venv/bin/pip install -r requirements.txt
# lägg din config.yaml och .env i /opt/oljan

sudo cp deploy/oljan.service /etc/systemd/system/oljan.service
sudo systemctl daemon-reload
sudo systemctl enable --now oljan
journalctl -u oljan -f        # följ loggen
```

systemd startar om processen automatiskt (`Restart=always`) och hanterar
SIGTERM för ren avstängning.

### Alternativ B: Docker / docker-compose

```bash
cp config.example.yaml config.yaml   # redigera
cp .env.example .env                 # fyll i
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml logs -f
```

`restart: unless-stopped` ger automatisk återstart. Data (SQLite, charts,
loggar) persisteras i en namngiven volym.

### Alternativ C: lokalt i bakgrunden

`tmux`/`screen`, eller `nohup python -m oiltrader --config config.yaml &`.
För en riktig 24/7-drift rekommenderas dock systemd eller Docker.

---

## Hur analysen fungerar

Per pollning:

0. **Färskhetsgrind** – uppgifter äldre än `news.max_age_minutes` ignoreras
   (intradagsfokus). **Story-klustring** slår sedan ihop samma händelse från
   flera källor till *en* story → äkta korroborering + "vem var först".

För varje story:

1. **Relevans** – summan av matchade nyckelords vikter (ordgräns-matchning, så
   "build" ≠ "building"). Under tröskel → ignoreras.
2. **Riktning & storlek** – det olje-riktade lexikonet ger en signerad poäng
   (hausse/baisse) och magnitud, med **negationshantering** ("no ceasefire"
   flippar). VADER används bara som svag sekundär signal.
3. **Kategori** – inventory / opec / geopolitical / supply / macro.
4. **Substans (0–1)** – vägt av: bästa källvikt i storyn, korroborering (antal
   *oberoende källor på samma story*), konkreta siffror, och pris/volym-bekräftelse.
   **Konviktion (0–100)** sammanfattar dessutom substans + korroborering +
   färskhet + källvikt + historik + MTF-samsyn till ett enda triage-tal.
5. **Manipulations-/brusrisk (0–1)** – hög när en *stor* påstådd effekt kommer
   från en *svag, obekräftad* källa *utan* stöd i tape:n (klassisk röd flagga).
6. **Historik** – analoga tidigare fall (samma kategori + riktning) ger
   träfffrekvens och avkastningsfördelning per horisont, t.ex.
   *"gick fortsatt upp inom 2–4h i 70 % av 12 fall (median +0,9 %)"*.
7. **Rekommendation** – kombinerar allt, medvetet om din position och hävstång:
   konkreta nivåer (stöd/motstånd), ATR-baserat stoppförslag, hur många procent
   på marginalen ett stopp/en motrörelse innebär vid x-hävstång, och en
   likvidationsvarning.
8. **Notis** – rå nyhet + chart-kontext + bild + bedömning + åtgärd +
   konfidens + källor + osäkerheter, pushad via Telegram i nära realtid.

Exempel på notis (förkortad):

```
🛢️ HÅLL/ÖKA long  ·  konviktion 68/100 🟩🟩🟩⬜⬜
[HAUSSE · konv 68 · 3 källor] HÅLL/ÖKA long — Houthis Declare Naval Blockade...
🎯 3 källor bekräftar, färsk (8m). Substans 0.70/manip 0.21.
📊 CL=F 5m 80.71 · trend up · RSI 73 · vol 1.2x · stöd 79.45
MTF: 1m ↑ · 5m ↑ · 15m ↑ · 1h →
🔎 SUBSTANSIELL · substans 0.70 · manip 0.21 · konfidens HIGH
📈 Historik: upp inom 15m i 91% av 11 liknande fall (median +0.3%).
🧭 I linje med din long. Överväg hålla/öka; stop ~79.25 (≈18% på marginal vid x10).
🗞 Källor (3): news.google 12:27 · rigzone 14:06 · oilprice 16:30
⏱ Först: news.google 12:27 UTC (+5m sedan)
🔗 https://oilprice.com/...
⚠️ Korrelation ≠ kausalitet ... (ej finansiell rådgivning)
```

**Notisen leder med handling.** Första raden ger allt för snabb triage: åtgärd
(HÅLL/ÖKA / MINSKA/HEDGA / AVVAKTA / BEVAKA), en **konviktionspoäng 0–100** (väger
substans, korroborering, färskhet, källvikt, historik och MTF-samsyn) och antal
källor. Samma story från flera källor slås ihop till **en** notis med
korsvis-bekräftelse och "vem var först"-tidsstämpel.

**Så bygger historiken upp sig:** basraterna blir bättre ju längre systemet
kört, eftersom varje händelse lagras och "mognar" när dess horisonter passerat.
De första dagarna rapporteras "för få mognade fall" och besluten vilar på
chart + källkvalitet – detta är medvetet och ärligt (ingen påhittad statistik).

---

## Utöka systemet

Lägg till en ny källa genom att implementera `Collector.collect()`:

```python
# oiltrader/collectors/mysource.py
from .base import Collector, NewsItem, now_utc

class MySourceCollector(Collector):
    name = "mysource"
    def collect(self):
        return [NewsItem(source="mysource", title="...", content="...",
                         url="...", ts=now_utc())]
```

Registrera den i `collectors/__init__.py:build_collectors()`. Justera
`classification.source_weights` för hur mycket den ska väga i substansbedömningen.

Andra utökningar: fler indikatorer i `indicators.py`, fler kategorier i
`events.CATEGORIES`, fler notiskanaler i `notifier.py` (t.ex. e-post/Discord).

---

## Felsökning

- **Inga notiser?** Kör `--selftest`. För ntfy: kontrollera att du
  prenumererar på **exakt** samma topic som i `config.yaml`. För Telegram:
  kontrollera token/chat-id. Utan giltig kanal skrivs notiser till konsol/logg.
- **"Insufficient candles" / 429 rate-limit** – Yahoo kan strypa delade
  moln-IP:n. Systemet gör retry med backoff och roterar mellan query1/query2.
  Höj `market_data.refresh_seconds` och `request_spacing`, eller kör på en
  residential-IP/Raspberry Pi. yfinance-providern kan användas som alternativ.
- **feedparser-fel vid install** – ignorera; stdlib-fallbacken används.
- **Få händelser** – sänk `relevance.min_score` eller lägg till fler RSS-feeds
  och nyckelord i `config.yaml`.
- **För många notiser** – höj `notifications.min_notify_score` och/eller
  `dedup_minutes`, eller sätt `quiet_hours`.
- **Loggar** finns i `data/logs/oljan.log`.

---

## Licens & ansvarsfriskrivning

Använd på egen risk. Oljan tillhandahåller informationsstöd och lägger inga
ordrar. Handel med hävstång (t.ex. x10) innebär hög risk för snabb
likvidation. Detta är inte finansiell rådgivning.
