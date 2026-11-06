# Monitor Aste

Controlla ogni ora i portali d'asta, filtra i lotti secondo i tuoi criteri, li mostra in una dashboard e ti avvisa su Telegram quando esce qualcosa che ti interessa.

**Fonti attuali** (si accendono/spengono in `config.yaml` → `fonti`)
- **PVP – Portale Vendite Pubbliche** (Ministero della Giustizia): per legge ci finiscono praticamente tutte le aste giudiziarie e fallimentari d'Italia, qualunque sia il gestore (Astegiudiziarie.it, Astetelematiche, Astalegale, Portaleaste, Fallco, IVG…). Letto tramite la sua API pubblica.
- **Agenzia del Demanio**: veicoli e beni mobili confiscati (non passano dal PVP).
- **Industrial Discount**: macchinari, veicoli, nautica, anche vendite da aziende attive.
- **AsteBusiness**: aste giudiziarie e private; quelle già presenti sul PVP vengono scartate come doppioni.
- **Gobid**: letto dal tuo PC (cartella `pc/`, vedi ISTRUZIONI.txt), perché Gobid blocca i server di GitHub.

Non letti in automatico: **astagiudiziaria.com** (il suo robots.txt vieta i programmi automatici; i suoi lotti marcati "PVP" arrivano comunque dal PVP) e **astalegale.net / portaleaste.com** (bloccano gli accessi automatici; le loro giudiziarie arrivano dal PVP).

## Come funziona
- GitHub Actions lancia `monitor.py` ogni ora dalle 6 alle 22 circa.
- Il primo giro carica gli annunci degli ultimi 30 giorni (senza mandarti avvisi, ti arriva solo un messaggio di conferma).
- Dal secondo giro in poi: lotti nuovi → dashboard + messaggio Telegram se rispettano una regola in `config.yaml`.
- Le aste scadute spariscono da sole.

## Messa in funzione (una volta sola, ~10 minuti)

### 1. Repository GitHub
Crea un repository (es. `monitor-aste`) e caricaci questi file, compresa la cartella `.github`.

> Il repository deve essere **pubblico** perché GitHub Pages gratuito funzioni. Contiene solo dati pubblici (annunci d'asta) e i tuoi filtri: niente di personale. Preferiti e note della dashboard restano solo nel tuo browser.

### 2. Bot Telegram
1. Su Telegram apri **@BotFather** → `/newbot` → scegli nome e username. Ti dà un **token**.
2. Scrivi un messaggio qualsiasi al tuo nuovo bot.
3. Apri nel browser `https://api.telegram.org/bot<TOKEN>/getUpdates` e copia il numero `"chat":{"id": ...}`: è il tuo **chat id**.

### 3. Segreti nel repository
Settings → Secrets and variables → Actions → **New repository secret**:
- `TELEGRAM_BOT_TOKEN` = il token
- `TELEGRAM_CHAT_ID` = il chat id

### 4. Attiva Pages
Settings → Pages → *Build and deployment* → Source: **GitHub Actions**.

### 5. Primo avvio
Actions → **Monitor aste** → **Run workflow**. Dopo qualche minuto la dashboard è su
`https://<tuo-utente>.github.io/monitor-aste/`

## Personalizzare
Tutto è in **`config.yaml`** (modificabile da GitHub con la matita, dal telefono):
- `categorie`: cosa entra in dashboard
- `parole_chiave` / `parole_escluse`
- `immobili.prezzo_max` e `immobili.regioni`
- `avvisi`: le regole che fanno partire un messaggio Telegram (categorie, parole, regioni, prezzo massimo)

Salvando `config.yaml` il monitor riparte subito con i nuovi criteri.

## Dashboard
- Ricerca libera, filtri per categoria, regione, prezzo, scadenza
- "Nuove 24h", "Solo parole chiave", preferiti ★ e note per lotto (salvati nel browser)
- Link diretto alla scheda ufficiale di ogni asta

## Limiti da sapere
- Sul PVP i beni mobili spesso hanno descrizioni generiche ("beni come da inventario"): le parole chiave trovano solo quello che è scritto nell'annuncio. Le categorie aiutano a non perderli.
- Gobid non indica la località nell'elenco aste: per quelle il filtro regione non si applica.
- Se un portale cambia struttura, la dashboard segnala la fonte in errore e continua con le altre.
