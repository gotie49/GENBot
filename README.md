# statustw-discord-bot

Discord-Bot, der eine Liste von Teeworlds/DDNet-Spielern über die öffentliche
[status.tw-API](https://api.status.tw/docs) überwacht und eine Nachricht postet, sobald ein
Spieler online geht – im Stil der Referenz:

```
┃ Player Smash connected to [A] [KoG] GER3 #7 - Solo Gores [kog.tw]
┃ Powered by status.tw • 01.08.2026 01:30
```

(Embed mit violetter Leiste `#701A75`, Spieler- und Servername als Links, Erkennungszeit als
Embed-Zeitstempel. Name und Avatar über der Nachricht sind Name und Avatar deiner Bot-Anwendung.)

Läuft dauerhaft und ressourcenschonend per Docker Compose auf einem Raspberry Pi (arm64, optional armv7).

---

## Inhalt

1. [Discord-Bot erstellen und Token einrichten](#1-discord-bot-erstellen-und-token-einrichten)
2. [Zugang zu status.tw](#2-zugang-zu-statustw)
3. [Ziel-Channel konfigurieren](#3-ziel-channel-konfigurieren)
4. [Starten (lokal und Docker)](#4-starten)
5. [Spieler verwalten](#5-spieler-verwalten)
6. [Dauerbetrieb auf dem Raspberry Pi](#6-dauerbetrieb-auf-dem-raspberry-pi)
7. [Logs, Backups, Fehlerdiagnose](#7-logs-backups-fehlerdiagnose)
8. [Konfigurationsreferenz](#8-konfigurationsreferenz)
9. [API-Analyse, Architektur und Annahmen](#9-api-analyse-architektur-und-annahmen)

---

## 1. Discord-Bot erstellen und Token einrichten

1. <https://discord.com/developers/applications> → **New Application**. Der Name (z. B.
   `teeworlds.bot`) und das App-Icon erscheinen später über jeder Nachricht.
2. Links **Bot** → **Reset Token** → Token kopieren und als `DISCORD_BOT_TOKEN` in `.env` eintragen.
   Das Token ist ein Passwort: nie committen, nie teilen.
3. Auf derselben Seite können alle **Privileged Gateway Intents** *aus* bleiben – der Bot braucht
   keine (nur den nicht-privilegierten `guilds`-Intent).
4. Bot einladen: **OAuth2 → URL Generator**, Scopes `bot` und `applications.commands`,
   Bot-Berechtigungen **View Channels**, **Send Messages**, **Embed Links**. Oder direkt:

   ```
   https://discord.com/oauth2/authorize?client_id=<APPLICATION_ID>&scope=bot+applications.commands&permissions=19456
   ```

   `<APPLICATION_ID>` steht unter **General Information**.

## 2. Zugang zu status.tw

Die vom Bot genutzten Endpunkte sind **öffentlich und ohne Token nutzbar**. Es gilt das
dokumentierte Limit von **100 Anfragen pro 60 Sekunden pro IP-Adresse**; der Bot hält sich
selbst an ein konfigurierbares Budget (Standard 60/min).

Optional kannst du im status.tw-Dashboard (Anmeldung auf <https://status.tw>) ein API-Token
erzeugen und als `STATUS_TW_API_KEY` eintragen. Es wird dann, wie dokumentiert, als
`Authorization: Bearer stw_…` gesendet. Ob ein Token höhere Limits bringt, ist in der
Dokumentation nicht beschrieben.

## 3. Ziel-Channel konfigurieren

1. In Discord **Einstellungen → Erweitert → Entwicklermodus** aktivieren.
2. Rechtsklick auf den Channel → **Kanal-ID kopieren** → `DISCORD_CHANNEL_ID`.
3. Rechtsklick auf den Server → **Server-ID kopieren** → `DISCORD_GUILD_ID` (empfohlen: die
   Slash-Commands erscheinen dann sofort statt erst nach bis zu einer Stunde).
4. Sicherstellen, dass der Bot in diesem Channel *Kanal ansehen*, *Nachrichten senden* und
   *Links einbetten* darf. Fehlende Rechte meldet der Bot beim Start im Log.

## 4. Starten

### Konfiguration

```bash
cp .env.example .env
nano .env        # mindestens DISCORD_BOT_TOKEN und DISCORD_CHANNEL_ID
```

### Per Docker (empfohlen)

```bash
docker compose up -d --build
docker compose logs -f
```

### Lokal (Python ≥ 3.11)

```bash
python3 -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
PYTHONPATH=src python -m statustw_bot
```

Unter Windows/PowerShell: `$env:PYTHONPATH="src"; python -m statustw_bot`.
Lokal wird `.env` im aktuellen Verzeichnis automatisch gelesen; bereits gesetzte
Umgebungsvariablen haben Vorrang. Die Datenbank liegt dann unter `data/bot.sqlite3`.

### End-to-End-Test mit Discord ohne status.tw

`tools/fake_statustw.py` simuliert die genutzten status.tw-Endpunkte lokal. Spieler lassen sich
per `http://127.0.0.1:8399/_online?name=X` / `_offline?name=X` umschalten, `_fail?on=1|0`
simuliert einen Ausfall. Dafür `STATUS_TW_BASE_URL=http://127.0.0.1:8399` setzen
(`http://` wird nur für localhost akzeptiert).

```bash
python tools/fake_statustw.py
```

### Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Die Tests brauchen weder Discord- noch status.tw-Zugangsdaten (die API wird lokal simuliert).

## 5. Spieler verwalten

Alle Befehle sind Slash-Commands unter `/player` und funktionieren nur auf Servern (nicht in DMs).
Antworten sind nur für den Aufrufenden sichtbar. Alle Texte, die Discord-Nutzer sehen, sind englisch.

| Befehl | Wirkung | Berechtigung |
|---|---|---|
| `/player add player:<Name> [server:<IPv4:Port>] [profile_url:<URL>] [aliases:<Name, Name>]` | Spieler hinzufügen. Alle Namen werden bei status.tw geprüft (exakte Schreibweise, Groß-/Kleinschreibung zählt). `server` begrenzt die Benachrichtigung auf einen bestimmten Server, `aliases` hinterlegt weitere Namen (siehe unten). | Manager |
| `/player edit player:<Name> aliases:<Name, Name>` | Aliase ersetzen; `aliases:-` entfernt alle | Manager |
| `/player remove player:<Name>` | Eintrag löschen | Manager |
| `/player enable player:<Name>` | Überwachung fortsetzen | Manager |
| `/player disable player:<Name>` | Überwachung pausieren, Eintrag bleibt | Manager |
| `/player list` | Alle Spieler mit letztem bekanntem Status | alle* |
| `/player status player:<Name>` | Live-Abfrage bei status.tw | alle* |

**Manager** = Serveradministratoren, zusätzlich Mitglieder der Rollen in `ADMIN_ROLE_IDS` und
Nutzer in `ADMIN_USER_IDS`. \*`PUBLIC_READ_COMMANDS=false` beschränkt auch `list`/`status` auf Manager.

Beim Hinzufügen wird **nicht** sofort benachrichtigt, auch wenn der Spieler gerade online ist –
die erste Nachricht kommt beim nächsten Wechsel offline → online. Dasselbe gilt nach `enable`
und nach dem Ändern der Aliase.

### Aliase (mehrere Namen pro Spieler)

```
/player add player:Gotie aliases:G0tie, gotie49
/player edit player:Gotie aliases:G0tie, gotie49, xGotie
/player edit player:Gotie aliases:-
```

- Ein Eintrag steht dann für eine Person mit Hauptnamen und bis zu 5 Aliasen (kommagetrennt;
  Namen, die selbst ein Komma enthalten, sind deshalb als Alias nicht möglich).
- Die Person gilt als online, sobald **irgendeiner** ihrer Namen online ist. Ein Wechsel zwischen
  den Namen während einer Online-Phase löst keine zweite Meldung aus; ein Dummy mit Alias auf
  demselben Server zählt als eine Person.
- Meldung: `Player Gotie (as G0tie) connected to …` – der Profil-Link zeigt auf den Namen, der
  tatsächlich online ist. In der Statusliste steht der Alias in Klammern, solange die Person
  unter diesem Namen online ist.
- Jeder Name gehört höchstens zu einer Person. Alle Befehle finden eine Person auch über einen
  Alias, z. B. `/player status player:G0tie`.
- Jeder Alias kostet pro Prüfung eine eigene Anfrage an status.tw. Die automatische Strategie
  zählt deshalb Namen statt Personen und wechselt bei mehr Namen, als ins Budget passen, auf die
  Sammelabfrage `/player/list`.

### Statusliste (dauerhafte Nachricht)

Optional pflegt der Bot in einem **eigenen Channel** eine dauerhafte Übersicht aller
registrierten Spieler, die laufend aktualisiert wird, statt neue Nachrichten zu posten:

```
┃ 🟢 Gotie        [A] |KoG| GER3 #26 - Insane Gores [kog.tw]     online now
┃ ──────────────────────────────────────────────────────────────────────────
┃ 🟡 Kingjulius   [A] |*KoG*| GER3 #26 - Extreme Gores [kog.tw]  13m ago
┃ ──────────────────────────────────────────────────────────────────────────
┃ 🟠 KOF          [A] |KoG| GER3 #29 - Mods Gores [kog.tw]       2h 16m ago
┃ ──────────────────────────────────────────────────────────────────────────
┃ 🔴 mpampas      [A] |KoG| GER3 #26 - Insane Gores [kog.tw]     18h ago
┃ ──────────────────────────────────────────────────────────────────────────
┃ ⚪ old_timer    DDNet GER10 - Novice                           3d ago
┃ ──────────────────────────────────────────────────────────────────────────
┃ ⚫ newbie       never
```

- Eine Nachricht im neueren Discord-Format (Components V2): ein Container mit farbiger
  Leiste (`BOARD_COLOR`), eine Zeile pro Spieler, dünne Trennlinien dazwischen.
- Der Punkt vor dem Namen zeigt, wie lange der Spieler offline ist: 🟢 gerade online,
  🟡 < 1 h, 🟠 1–5 h, 🔴 5–24 h, ⚪ > 24 h, ⚫ noch nie online gesehen. Sortiert: online zuerst, dann nach
  „zuletzt online“.
- Zeitangaben schreibt der Bot selbst (`18m ago`, `2h 15m ago`, `18h ago`, `3d ago`). Er
  bearbeitet die Nachricht nur, wenn sich der angezeigte Text ändert – höchstens etwa einmal
  pro Minute, also weit unter Discords Limits.
- Bis 15 Spieler passen in eine Nachricht; darüber pflegt der Bot automatisch weitere
  Nachrichten darunter. Wird eine Listen-Nachricht gelöscht, legt der Bot sie neu an. Die
  Nachrichten-IDs liegen in der Datenbank; nach einem Neustart wird dieselbe Nachricht
  weiterbearbeitet.
- Eine Liste aus einer älteren Version (Embeds) lässt sich nicht in das neue Format
  umwandeln – Discord lehnt das ab. Der Bot löscht sie dann einmalig und postet die neue Liste.

**Einrichten:**

1. Einen eigenen Channel anlegen (z. B. `#player-status`), idealerweise so, dass nur der Bot
   schreiben darf. Der Bot braucht dort *Kanal ansehen*, *Nachrichten senden*, *Links einbetten*.
2. Kanal-ID als `DISCORD_STATUS_CHANNEL_ID` in `.env` eintragen (muss sich von
   `DISCORD_CHANNEL_ID` unterscheiden).
3. **Klick auf den Server → DDNet öffnen:** Discord macht `ddnet://`-Links nicht anklickbar.
   Deshalb verlinkt der Bot auf eine kleine Weiterleitungsseite (`docs/connect/index.html`),
   die sofort auf `ddnet://IP:Port` weiterleitet ([DDNet-Links](https://ddnet.org/news/ddnet-links/)).
   Einmalig auf GitHub aktivieren: Repository → **Settings → Pages** → *Source: Deploy from a
   branch* → Branch `master`, Ordner `/docs` → **Save**. Nach ein bis zwei Minuten ist die Seite unter
   `https://gotie49.github.io/GENBot/connect/` erreichbar; `CONNECT_URL_TEMPLATE` in `.env.example`
   zeigt bereits dorthin. Beim ersten Klick fragt der Browser, ob DDNet geöffnet werden darf.
   Ohne `CONNECT_URL_TEMPLATE` verlinkt der Servername auf die status.tw-Serverseite.
4. `docker compose up -d` – im Log erscheinen `status channel ready` und beim ersten Mal `status board updated`.

## 6. Dauerbetrieb auf dem Raspberry Pi

Empfohlen: Raspberry Pi 3/4/5 mit **Raspberry Pi OS 64-bit** (arm64). 32-bit (armv7) wird
ebenfalls unterstützt; dort werden fehlende Python-Wheels beim ersten Build kompiliert (dauert
einige Minuten länger).

```bash
# Docker installieren (offizielles Skript) und beim Booten starten
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER      # danach einmal ab- und wieder anmelden
sudo systemctl enable --now docker

# Projekt holen (git clone oder Ordner per scp kopieren)
cd statustw-discord-bot
cp .env.example .env && nano .env
docker compose up -d --build
```

- `restart: unless-stopped` startet den Container nach Abstürzen und nach einem Neustart des Pi
  automatisch – ohne offenes Terminal oder SSH-Sitzung.
- Ein `HEALTHCHECK` meldet den Container als `unhealthy`, wenn die Überwachungsschleife hängt
  (`docker compose ps`). Stürzt die Schleife selbst ab, beendet sich der Bot bewusst, damit
  Docker ihn neu startet.
- Der Speicher ist per `mem_limit: 256m` begrenzt (der tatsächliche Verbrauch auf dem Pi wurde
  nicht gemessen; im `bulk`-Modus wird pro Zyklus eine ≈ 5 MB große Antwort verarbeitet).
  Logs werden rotiert (3 × 10 MB), damit die SD-Karte nicht volläuft.

| Aufgabe | Befehl |
|---|---|
| Starten / bauen | `docker compose up -d --build` |
| Stoppen | `docker compose down` (Daten bleiben im Volume erhalten) |
| Neustarten | `docker compose restart` |
| Aktualisieren | `git pull && docker compose up -d --build` |
| Status | `docker compose ps` |

`docker compose down -v` löscht **auch die Datenbank** – nur verwenden, wenn das gewollt ist.

## 7. Logs, Backups, Fehlerdiagnose

### Logs

```bash
docker compose logs -f --tail=200
```

Logs gehen nach stdout im Format `ts=… level=… logger=… msg="…" key=value`. Protokolliert werden
Start, Discord-Verbindung, API-Erreichbarkeit, registrierte Spieler, Statuswechsel
(`msg="presence transition" event=came_online|went_offline`), versendete Nachrichten,
Rate-Limits, Wiederholungsversuche und das Herunterfahren. Tokens und API-Schlüssel werden vor
der Ausgabe ersetzt (`***REDACTED***`). Für mehr Details `LOG_LEVEL=DEBUG` setzen und
`docker compose up -d` ausführen.

### Backup

Die SQLite-Datenbank liegt im Docker-Volume `bot-data` (`/data/bot.sqlite3`). Ein konsistentes
Backup ist im laufenden Betrieb möglich:

```bash
docker compose exec bot python -m statustw_bot.backup /data/backup.sqlite3
docker compose cp bot:/data/backup.sqlite3 ./backup-$(date +%F).sqlite3
```

Für regelmäßige Backups beide Zeilen z. B. per `crontab -e` täglich ausführen.

### Wiederherstellen

```bash
docker compose stop
docker compose run --rm --no-deps -v "$PWD:/restore:ro" bot \
  sh -c 'cp /restore/backup-2026-08-01.sqlite3 /data/bot.sqlite3 && rm -f /data/bot.sqlite3-wal /data/bot.sqlite3-shm'
docker compose up -d
```

Das Schema wird beim Start automatisch angelegt bzw. migriert (`PRAGMA user_version`).

### Fehlerdiagnose

| Symptom | Ursache / Lösung |
|---|---|
| `configuration error: …` und Container startet neu | Pflichtwert fehlt oder ist ungültig – Meldung nennt die Variable. |
| `Discord rejected the bot token` | Token falsch oder zurückgesetzt → neues Token eintragen. |
| `notification channel not accessible` / `bot lacks permissions` | Channel-ID prüfen, Bot-Rechte im Channel setzen (siehe Abschnitt 3). |
| Slash-Commands erscheinen nicht | `DISCORD_GUILD_ID` setzen oder bis zu 1 h warten; Bot mit Scope `applications.commands` einladen. |
| `status.tw lookup failed; status unknown` | API vorübergehend nicht erreichbar. Der Status bleibt unverändert (kein falsches „offline“), nach Erholung kommt keine doppelte Meldung. |
| `status.tw rate limit hit` | Anderes Programm im selben Netz nutzt das IP-Limit mit, oder Budget zu hoch → `API_REQUEST_BUDGET_PER_MINUTE` senken bzw. `POLL_INTERVAL_SECONDS` erhöhen. |
| Spieler wird nie gemeldet | Exakte Schreibweise prüfen (`/player status`); bei gesetztem `server`-Filter zählt nur dieser Server. |
| HTTP 403 vom Proxy (z. B. Firmennetz) | Ein Proxy blockiert status.tw – Bot in einem Netz ohne diese Sperre betreiben. |

## 8. Konfigurationsreferenz

Alle Einstellungen sind Umgebungsvariablen (siehe `.env.example` mit Kommentaren).

| Variable | Standard | Bedeutung |
|---|---|---|
| `DISCORD_BOT_TOKEN` | – (Pflicht) | Bot-Token |
| `DISCORD_CHANNEL_ID` | – (Pflicht) | Channel für Benachrichtigungen |
| `DISCORD_GUILD_ID` | leer | Server für sofortigen Command-Sync |
| `DISCORD_STATUS_CHANNEL_ID` | leer | Channel für die dauerhafte Statusliste (leer = aus) |
| `ADMIN_ROLE_IDS`, `ADMIN_USER_IDS` | leer | zusätzliche Manager (kommagetrennte IDs) |
| `PUBLIC_READ_COMMANDS` | `true` | `list`/`status` für alle |
| `STATUS_TW_API_KEY` | leer | optionales Bearer-Token |
| `STATUS_TW_BASE_URL` | `https://api.status.tw` | API-Basis-URL |
| `POLL_INTERVAL_SECONDS` | `60` | Prüfintervall (15–3600) |
| `POLL_STRATEGY` | `auto` | `per_player`, `bulk` oder `auto` |
| `API_REQUEST_BUDGET_PER_MINUTE` | `60` | max. eigene Anfragen/min (≤ 100) |
| `API_TIMEOUT_SECONDS` | `15` | Timeout pro Anfrage |
| `API_MAX_RETRIES` | `2` | Wiederholungen bei Timeout/5xx/429 (exponentielles Backoff) |
| `STARTUP_STRATEGY` | `baseline` | `baseline`, `resume`, `notify` (siehe unten) |
| `OFFLINE_CONFIRMATIONS` | `2` | aufeinanderfolgende Offline-Ergebnisse bis „offline“ |
| `NOTIFY_COOLDOWN_SECONDS` | `0` | Mindestabstand zweier Meldungen pro Spieler |
| `NOTIFY_MIN_INTERVAL_SECONDS` | `1` | Mindestabstand zweier Nachrichten im Channel |
| `EMBED_COLOR` | `#701A75` | Farbe der Embed-Leiste |
| `PLAYER_PROFILE_URL_TEMPLATE` | leer\* | Profil-Link, Platzhalter `{name}` |
| `SERVER_URL_TEMPLATE` | leer\* | Server-Link, Platzhalter `{ip}`, `{port}` |
| `BOARD_COLOR` | `#5865F2` | Farbe der Leiste der Statusliste |
| `CONNECT_URL_TEMPLATE` | leer\* | Server-Link in der Statusliste (Weiterleitung zu `ddnet://`), Platzhalter `{ip}`, `{port}` |
| `LOG_LEVEL` | `INFO` | `DEBUG` … `CRITICAL` |
| `DATABASE_PATH` | `data/bot.sqlite3` | im Container `/data/bot.sqlite3` |
| `HEARTBEAT_FILE` | leer | im Container `/tmp/heartbeat` (Healthcheck) |

\*`.env.example` setzt `https://status.tw/players/{name}` und `https://status.tw/servers/{ip}/{port}`
(siehe Abschnitt 9). Leer lassen ⇒ Namen erscheinen fett statt verlinkt.

**Startstrategien** (Verhalten für Spieler, die beim Start bereits online sind):

- `baseline` – die erste Prüfung nach dem Start legt nur den Ausgangszustand fest; keine Meldungen.
- `resume` – vergleicht mit dem gespeicherten Zustand: war ein Spieler vor dem Neustart offline und
  ist jetzt online, wird gemeldet. War er schon online, nicht.
- `notify` – meldet alle beim Start online Spieler, deren aktuelle Sitzung noch nicht gemeldet war.

In allen Strategien gilt: Wer vor dem Neustart als online gespeichert war und es noch ist, wird
nicht erneut gemeldet.

## 9. API-Analyse, Architektur und Annahmen

### Ergebnisse der API-Analyse (status.tw API 2.1.1, geprüft am 20.10.2026)

Die Doku unter <https://api.status.tw/docs> ist eine Scalar-Seite mit eingebetteter
OpenAPI-3.0-Spezifikation. Relevante, **dokumentierte** Endpunkte:

| Endpunkt | Verwendung |
|---|---|
| `GET /info` | Verbindungstest beim Start (`app`, `version`) |
| `GET /player/name/{name}?withoutClients=true` | Exakter Name → `ClientListDto` mit `players[]` = aktuelle Verbindungen. Leeres Array = offline. **404 = Name nie gesehen.** Antwort ≈ 1 KB. |
| `GET /player/list` | Alle aktuell online Spieler (eine Anfrage, aber ≈ 5 MB / ≈ 4 800 Spieler zum Prüfzeitpunkt). Filter `name` ist nur ein Teilstring-Filter – mehrere Namen pro Anfrage sind **nicht** möglich. |

Genutzte Felder: `name`, `players[].name/isPlayer/isBot/isAfk`, `players[].server.ip/port/name/
numClients/maxClients/map.name/gameType.name/logoUrl`.

- **Authentifizierung:** Bearer-Token (`Authorization: Bearer stw_…`), für die genutzten
  öffentlichen Endpunkte nicht erforderlich (verifiziert: Anfragen ohne Token liefern HTTP 200).
- **Rate Limit:** dokumentiert 100 Anfragen / 60 s pro IP, sonst `429`. Die API sendet zusätzlich
  `x-ratelimit-limit/-remaining/-reset`-Header (beobachtet, nicht dokumentiert); der Bot wertet
  sie aus, falls vorhanden, ist aber nicht darauf angewiesen.
- **Webhooks:** status.tw bietet laut Website Webhooks (u. a. „joins/leaves“) für angemeldete
  Konten. Diese sind in der API-Spezifikation nicht beschrieben (kein Payload-Format) und würden
  einen öffentlich erreichbaren Endpunkt auf dem Pi erfordern. Der Bot nutzt daher Polling.

**Abfragestrategie:** `auto` nutzt pro Spieler eine kleine Anfrage, solange die Spielerzahl ins
Budget passt (Standard: bis 60 Spieler bei 60 s), sonst eine einzige `/player/list`-Anfrage pro Zyklus.

### Architektur

```
src/statustw_bot/
├── __main__.py        Einstieg, Signal-Handling (SIGTERM/SIGINT → kontrolliertes Herunterfahren)
├── config.py          zentrale Konfiguration aus Umgebungsvariablen (+ .env für lokale Läufe)
├── logging_setup.py   key=value-Logging, Secret-Redaction
├── statustw/          API-Client (aiohttp): Modelle, Fehlerklassen, Rate-Limit-Budget, Retry/Backoff
├── presence.py        reine Zustandsautomaten-Logik (keine I/O) – Kern der Tests
├── storage.py         SQLite via aiosqlite (läuft im Worker-Thread), Migrationen
├── monitor.py         Polling-Schleife: abfragen → bewerten → speichern → Benachrichtigung einreihen
├── notifier.py        Embed-Layout + serielle Sende-Queue mit Retry
├── board.py           dauerhafte Statusliste: Rendering + Abgleich der Nachrichten
├── commands.py        /player-Slash-Commands, Eingabevalidierung, Berechtigungen
├── bot.py             discord.Client: verdrahtet alles, Lebenszyklus
├── backup.py          Online-Backup der Datenbank
└── healthcheck.py     Docker-HEALTHCHECK über Heartbeat-Datei
```

**Statuslogik:** `unknown` / `online` / `offline` pro Spieler.

- Ein API-Fehler setzt den Status *nie* auf offline – er erhöht nur einen Fehlerzähler.
- offline → online löst genau eine Nachricht pro Online-Sitzung aus (`session_announced`).
- online → offline wird erst nach `OFFLINE_CONFIRMATIONS` aufeinanderfolgenden Offline-Ergebnissen
  übernommen (verhindert Doppelmeldungen bei Serverwechseln oder einzelnen Lücken). Dafür wird
  kein Discord-Post gesendet, nur geloggt.
- Der neue Zustand wird **vor** dem Einreihen der Nachricht in SQLite geschrieben. Ein Absturz
  zwischen Speichern und Senden führt höchstens zu einer verlorenen, nie zu einer doppelten Meldung.
- Prüfzyklen sind per Lock serialisiert; Statusupdates gelten nur, solange der Spieler noch
  existiert und aktiv ist (`UPDATE … WHERE enabled = 1`), sodass ein gleichzeitiges `remove`/`disable`
  keine Meldung mehr auslöst.
- Nachrichten laufen durch eine einzelne Queue mit Mindestabstand; discord.py behandelt Discords
  429-Antworten zusätzlich selbst.

### Annahmen und Einschränkungen (nicht verifiziert bzw. bewusst gewählt)

- **Profil- und Server-Links:** Die URL-Muster `https://status.tw/players/{name}` und
  `https://status.tw/servers/{ip}/{port}` stehen **nicht in der API-Dokumentation**. Sie wurden aus
  dem Routing der status.tw-Website ermittelt und lieferten HTTP 200. Deshalb sind sie im Code
  standardmäßig *aus* und nur in `.env.example` voreingetragen; ändert status.tw die Website,
  einfach anpassen oder leeren.
- **„Online“** heißt: mindestens eine aktuelle Verbindung laut status.tw – Zuschauer zählen mit.
  Wie aktuell status.tw selbst ist, hängt von dessen Erfassungsintervall ab (nicht dokumentiert).
- **Spielernamen** sind in Teeworlds nicht eindeutig; jeder kann jeden Namen benutzen. Überwacht
  wird ein exakter Name, keine Person.
- **Spieler auf mehreren Servern:** gemeldet wird die erste von status.tw gelieferte Verbindung,
  mit dem Zusatz „(and N other servers)“.
- **Serveradressen** werden als IPv4 angenommen (so dokumentiert).
- **Formatierung:** In Link-Texten wertet Discord Backslash-Escapes *nicht* aus, wendet
  Formatierung aber trotzdem an (in einem echten Discord-Client geprüft). Eckige Klammern bleiben
  daher unverändert (`[A] [KoG] …` wird korrekt dargestellt), `* ~ ` | `` und `_` an Wortgrenzen
  werden durch ähnlich aussehende Unicode-Zeichen ersetzt (`∗ ∼ ˋ ǀ ˍ`). Ohne Link wird normal escaped.
- **Datumsformat** des Zeitstempels bestimmt der Discord-Client des Betrachters (Sprache/Region).

### Was wurde tatsächlich getestet?

| Geprüft | Ergebnis |
|---|---|
| Automatisierte Tests (`pytest`, 150 Tests: Statusübergänge, Neustart-Strategien, API-Fehler/Timeouts/429/ungültige Antworten, Doppelmeldungen, Nebenläufigkeit, Persistenz, Embed-Layout, Konfiguration, Backup, Healthcheck) | ✅ bestanden (Windows, Python 3.12.10) |
| status.tw-Endpunkte, Antwortformate, Header, Auth-Pflicht | ✅ live per HTTP geprüft; Parser mit echtem `/player/list`-Datensatz getestet |
| Bot-Start mit ungültigem Token → saubere Fehlermeldung, Exit 1, kein Token im Log | ✅ |
| Slash-Command-Payloads (Namen, Längen, nur Server-Kontext) | ✅ lokal erzeugt und geprüft |
| Python-API-Client live gegen status.tw | ❌ nicht möglich: Firmenproxy der Testumgebung blockiert status.tw |
| Docker-Build amd64/arm64/armv7, Tests in allen drei Images (ARM per Emulation) | ✅ |
| `compose up`, Neustart nach Fehler, Non-Root, Read-only-FS, Volume-Persistenz, Healthcheck, Backup/Restore | ✅ |
| Echter Discord-Server mit lokalem status.tw-Simulator: Verbindung, Command-Sync, alle `/player`-Befehle, Online-Meldung, keine Doppelmeldung, API-Ausfall, offline→online, Neustart während online, Darstellung der Nachricht | ✅ |
| Statusliste im echten Discord (lokal mit Simulator): Layout mit Farbpunkten und Trennlinien, 13 Spieler in einer Nachricht, Bearbeiten an Ort und Stelle, Ersetzen einer Liste der Vorversion, `BOARD_COLOR` | ✅ |
| Statusliste mit mehr als 15 Spielern (Folgenachrichten), Öffnen von DDNet über die Weiterleitung, Darstellung auf dem Handy | nur automatisiert getestet bzw. offen |
| Betrieb auf echter Raspberry-Pi-Hardware (arm) gegen das echte status.tw: Start, `/player`-Befehle, Online-Meldung, keine Doppelmeldung, offline→online, Neustart des Pi | ✅ (vom Betreiber durchgeführt) |

Empfohlene Abnahme: Bot auf einem Testserver einladen, `/player add` mit einem gerade aktiven
Namen, warten bis der Spieler offline geht (Log `event=went_offline`) und wieder online kommt.
