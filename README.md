# Oljan – autonom bevaknings- och analysmotor för råolja (WTI/Brent)

Oljan är en resilient Python-daemon som körs dygnet runt och hjälper en aktiv
trader att **agera före marknaden**. Den:

- övervakar oljepriset och beräknar chart-kontext (candlesticks, volym, RSI,
  MACD, EMA, Bollinger, ATR, stöd/motstånd),
- samlar kontinuerligt in nyheter, officiella rapporter (EIA) och social
  media (Reddit, Stocktwits) från gratis/öppna källor,
- bedömer varje relevant händelse: **substans vs. brus/manipulation**,
  riktning (hausse/baisse) och storlek,
- kör en **tidsseriekorrekt historisk analys** (event study) som svarar
  "hur betedde sig priset efter liknande händelser förr?",
- pushar en **notis i nära realtid** med rå nyhet + djup analys + chart-bild +
  konkreta, hävstångsmedvetna åtgärdsförslag,
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
| `market_data.py` | Prisdata via yfinance (nyckelfritt), retry/normalisering |
| `indicators.py` | Tekniska indikatorer + stöd/motstånd via swing-pivots |
| `sentiment.py` | Olje-*riktat* lexikon (bull/bear för priset), VADER sekundärt |
| `collectors/` | Pluggbara källor: RSS, EIA, Reddit, Stocktwits, NewsAPI |
| `events.py` | Relevans, kategori, substans- vs. manipulationspoäng |
| `historical.py` | Event study / analoga fall, tidsseriekorrekt |
| `analysis.py` | Kombinerar allt → rekommendation + notistext |
| `charting.py` | Renderar candlestick-bild (matplotlib, headless) |
| `notifier.py` | Telegram/konsol, dedup, tysta timmar, heartbeat |
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

Systemet **fungerar utan nycklar** (prisdata via yfinance + RSS + Stocktwits +
konsolnotiser). För full funktion, sätt upp följande gratis:

### 1. Telegram (rekommenderat – för push-notiser)
1. Öppna Telegram, sök upp **@BotFather**, skicka `/newbot`, följ stegen.
2. Kopiera **bot-token** → `TELEGRAM_BOT_TOKEN` i `.env`.
3. Skicka valfritt meddelande till din nya bot.
4. Öppna `https://api.telegram.org/bot<DIN_TOKEN>/getUpdates` i webbläsaren,
   leta upp `"chat":{"id":...}` → sätt `TELEGRAM_CHAT_ID` i `.env`.
5. I `config.yaml`: `notifications.channel: "telegram"`.

### 2. EIA (rekommenderat – officiell lagerstatistik)
1. Registrera gratis nyckel: https://www.eia.gov/opendata/register.php
2. `EIA_API_KEY` i `.env`, `eia.enabled: true` i `config.yaml`.
   EIA:s veckorapport (onsdagar) är en av de mest prisdrivande händelserna.

### 3. Reddit (valfritt – social signal)
1. https://www.reddit.com/prefs/apps → **create app** → typ **script**.
2. `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` i `.env`,
   `social.reddit_enabled: true`.

### 4. NewsAPI (valfritt – kompletterande nyheter)
1. Gratis dev-nyckel: https://newsapi.org (100 anrop/dygn).
2. `NEWSAPI_KEY` i `.env`, `news.newsapi_enabled: true`.

> **X/Twitter** är avstängt som standard: gratis åtkomst är kraftigt begränsad
> och opålitlig. `collectors/` är pluggbart – lägg till en X-insamlare om du
> har API-åtkomst (se [Utöka systemet](#utöka-systemet)).

---

## Konfiguration

All konfiguration ligger i `config.yaml` (hemligheter i `.env`). Viktiga delar:

- `instruments` – vilka symboler som bevakas (WTI `CL=F`, Brent `BZ=F`).
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

### Alternativ A: systemd (rekommenderat på en billig VPS)

En liten VPS (t.ex. 1 vCPU/1 GB för några €/mån) räcker gott.

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

För varje ny, oläst källpost:

1. **Relevans** – summan av matchade nyckelords vikter. Under tröskel → ignoreras.
2. **Riktning & storlek** – det olje-riktade lexikonet ger en signerad poäng
   (hausse/baisse) och en magnitud. VADER används bara som svag sekundär signal.
3. **Kategori** – inventory / opec / geopolitical / supply / macro.
4. **Substans (0–1)** – vägt av: källvikt, korroborering (flera oberoende
   källor inom ett tidsfönster), konkreta siffror, och pris/volym-bekräftelse.
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
🛢️ OLJAN 🟡 konfidens: MEDIUM
[INVENTORY · hausse] EIA weekly crude inventories: drawdown of 4.2M barrels
📊 Chart (CL=F): pris 70.40, trend up, RSI 79 (overbought), stöd 66.90
🔎 Bedömning: SUBSTANSIELL. Substans=0.67, manipulationsrisk=0.38 ...
🧭 Rekommendation: Historik: liknande fall gick upp inom 24h i 100% av 8 fall
   (median +7.4%). Nyheten i linje med din long → överväg hålla/öka.
   Stopp ~66.49 = 5.6% på priset ≈ 56% på marginalen vid x10 ...
⚠️ Osäkerheter: ...
```

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

- **Inga notiser?** Kör `--selftest`. Kontrollera `notifications.channel` och
  Telegram-token/chat-id. Utan Telegram skrivs notiser till konsol/logg.
- **"Insufficient candles"** – yfinance kan strula tillfälligt; systemet gör
  retry med backoff och fyller på över tid. Kontrollera nätverk.
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
