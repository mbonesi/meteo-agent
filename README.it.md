# meteo-agent

**English version: [README.md](README.md).** Questa è la traduzione italiana del
README: in caso di differenze fa fede quello inglese.

Codice, commenti, log e output della CLI sono in inglese. Le notifiche Telegram
sono in italiano: sono il messaggio che ricevi.

Agente meteo per NUC. Ogni 15 minuti scarica previsioni e osservazioni per un
punto, le archivia su SQLite con finestra mobile di 72 ore, valuta un insieme di
regole booleane e manda una notifica Telegram quando scattano. Il testo della
notifica può arricchirlo un LLM (opzionale, disattivato di default), ma solo il
testo: la decisione è deterministica.

POC in Python, pensato per essere tradotto in C++ una volta validato il flusso.

---

## Cosa fa, in concreto

```
                  ┌───────────────┐
   Open-Meteo ───►│               │
   (previsione)   │    fetch      │
                  │               │──► store ──► purge ──► regole ──► notifica
   ARPAE E-R  ───►│               │    SQLite    72h       (AST)      Telegram
   (osservato)    └───────────────┘                          │
                                                             ▼
                                                        Claude (redattore)
```

Ogni ciclo è indipendente e nessuna sorgente può farlo cadere: se ARPAE non
risponde, le regole su previsione girano lo stesso, e viceversa.

### Le due sorgenti

| | Open-Meteo | ARPAE Emilia-Romagna |
|---|---|---|
| Cosa | previsione oraria fino a 72 h | osservato dalla rete idro-meteo regionale |
| Formato | JSON | JSONL, data model RMAP, tabella B WMO |
| Chiave | non serve | non serve (CC-BY) |
| Direzione raffica | **non disponibile** | sì (B11043) |

### Cosa viene archiviato

Entrambe le cose che servono per ragionare:

- **`observation`** — le osservazioni ARPAE, indicizzate sull'istante di misura.
  Finestra mobile su `ts`.
- **`forecast`** — **ogni snapshot** di previsione scaricato, indicizzato su
  `(fetched_at, valid_ts)`. Non solo l'ultima previsione: tenendo gli snapshot si
  vede come la previsione per un dato istante è cambiata nel tempo. Finestra
  mobile su `fetched_at`, così ogni snapshot conservato è completo del suo
  orizzonte.

A regime sono ~20 MB di database (circa 290 snapshot da ~600 valori ciascuno). Le scritture sono idempotenti: rilanciare un
ciclo non duplica niente, e le correzioni ARPAE sovrascrivono il dato vecchio,
come prevede il loro modello (l'ultimo dato ricevuto vince).

---

## Limite da conoscere prima di scrivere le regole

**Open-Meteo non prevede la direzione della raffica**, solo l'intensità
(`wind_gusts_10m`). La direzione vera della raffica esiste solo nell'osservato
ARPAE (codice B11043 → `wind_gusts_direction_10m`).

Quindi una regola tipo *"raffiche da E/SE sopra 45 km/h"* si scrive in due modi:

```toml
# su PREVISIONE: la direzione è quella del vento medio, usata come proxy
when = 'dir_in(wind_direction_10m, ["NE","ENE","E","ESE","SE"]) and wind_gusts_10m > 45'

# su OSSERVATO: direzione vera della raffica
when = 'dir_in(wind_gusts_direction_10m, ["NE","ENE","E","ESE","SE"]) and wind_gusts_10m > 45'
```

`config.example.toml` contiene entrambe.

---

## Installazione su Ubuntu 24.04

Testato su Python 3.12.3, lo stesso di 24.04.

### 1. Dipendenze e utente di servizio

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip git

# Utente di sistema senza login e senza home: l'agente non ne ha bisogno
sudo useradd --system --no-create-home --shell /usr/sbin/nologin meteo
```

### 2. Codice e virtualenv

```bash
sudo mkdir -p /opt/meteo-agent/meteoagent
sudo cp -r meteoagent/* /opt/meteo-agent/meteoagent
sudo cp pyproject.toml /opt/meteo-agent/

cd /opt/meteo-agent

sudo python3 -m venv venv
sudo ./venv/bin/pip install -e .

# Directory dei dati e della configurazione
sudo mkdir -p /var/lib/meteo-agent /etc/meteo-agent
sudo chown meteo:meteo /var/lib/meteo-agent
```

> **Dopo un upgrade di Ubuntu che cambia la versione di Python** (es. 24.04 →
> 26.04, Python 3.12 → 3.14) la venv si rompe: `python3` punta al nuovo
> interprete, che non vede i pacchetti installati per il vecchio. Il servizio
> muore con `ModuleNotFoundError: No module named 'meteoagent'`. Va ricreata:
>
> ```bash
> sudo rm -rf /opt/meteo-agent/venv
> sudo python3 -m venv /opt/meteo-agent/venv
> sudo /opt/meteo-agent/venv/bin/pip install -e /opt/meteo-agent
> sudo systemctl reset-failed meteo-agent && sudo systemctl restart meteo-agent
> ```

### 3. Configurazione

```bash
sudo cp config.example.toml /etc/meteo-agent/config.toml
sudo cp meteo-agent.env.example /etc/meteo-agent/meteo-agent.env

# Il file dei segreti lo legge solo systemd e il gruppo meteo
sudo chown root:meteo /etc/meteo-agent/meteo-agent.env
sudo chmod 640 /etc/meteo-agent/meteo-agent.env
```

Le coordinate in `config.example.toml` (`44.49373453937952, 11.342959900671165`,
centro di Bologna) sono un **segnaposto**: sostituiscile in
`/etc/meteo-agent/config.toml` con quelle del punto che vuoi sorvegliare, quota
(`elevation_m`) compresa.

---

## Creare il bot Telegram

Serve una volta sola, dal telefono o da Telegram Desktop.

### 1. Creare il bot

1. Apri Telegram e cerca **@BotFather** (quello con la spunta blu).
2. `/start`, poi `/newbot`.
3. Ti chiede un **nome** (quello che vedi tu, es. `Meteo Casa`).
4. Poi uno **username**, che deve finire per `bot` ed essere libero
   (es. `meteo_casa_1234_bot`).
5. BotFather risponde con il **token**, una stringa tipo
   `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxx`.

Il token è una credenziale: chi ce l'ha può scrivere a nome del tuo bot.
Se lo perdi o lo esponi, `/revoke` su BotFather e te ne dà uno nuovo.

Opzionale, per far comparire una descrizione decente: `/setdescription`,
`/setuserpic`.

### 2. Trovare il chat_id

Un bot **non può iniziare una conversazione**: devi scrivergli tu per primo,
altrimenti non ha il permesso di mandarti niente.

1. Cerca il tuo bot per username, aprilo, premi **Avvia** (`/start`).
2. Manda un messaggio qualunque, anche solo `ciao`.
3. Sul NUC:

```bash
/opt/meteo-agent/venv/bin/meteo-agent chat-id 123456789:AAExxxxxxxxxxxx
```

Output:

```
chat_id = 123456789   (private: mario)
```

Se dice *"No messages received"*, non hai ancora scritto al bot, oppure hai
scritto al bot sbagliato.

### 3. Scrivere i segreti

```bash
sudo nano /etc/meteo-agent/meteo-agent.env
```

```ini
TELEGRAM_BOT_TOKEN=123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=123456789
```

Niente `export`, niente virgolette: systemd le prenderebbe alla lettera. La
chiave dell'LLM serve solo se lo attivi (passo 4).

### 4. Redattore LLM (opzionale)

**Di default è disattivato** (`[llm].enabled = false`): le notifiche partono con
titolo, sorgente, ora di aggiornamento e tabella degli istanti, senza prosa, e
non serve nessuna chiave. Le allerte scattano esattamente negli stessi casi:
l'LLM riassume soltanto la tabella in due frasi. Esempio di notifica:

```
⚠️ Vento da E/SE in arrivo
Previsione · open-meteo:best_match · aggiornato sab 17/10 11:30

Istanti interessati (ora locale)
• sab 17/10 13:00 — E (95°), 22.0 km/h
• sab 17/10 14:00 — E (100°), 26.0 km/h
• sab 17/10 15:00 — ESE (110°), 28.0 km/h
• sab 17/10 16:00 — ESE (120°), 21.0 km/h
```

Per attivarlo metti `enabled = true` nella sezione `[llm]` (il vecchio nome
`[claude]` è ancora accettato) e la chiave nel file dei segreti: nel TOML va
solo il **nome** della variabile d'ambiente che la contiene, con `api_key_env`.

**Anthropic (provider di default).** Chiave da <https://console.anthropic.com/settings/keys>,
in `ANTHROPIC_API_KEY`. Serve un po' di credito sul workspace. La redazione usa
`claude-haiku-4-5`, poche centinaia di token per notifica: la spesa è
trascurabile e comunque limitata da `cooldown_h`.

**GitHub Models, con un Personal Access Token.** Crea un PAT fine-grained con
permesso *Models: read* (<https://github.com/settings/tokens>), mettilo nel file
dei segreti come `GITHUB_TOKEN=github_pat_...` e in `config.toml`:

```toml
[llm]
enabled     = true
provider    = "openai"     # qualsiasi endpoint compatibile con OpenAI chat/completions
endpoint    = "https://models.github.ai/inference/chat/completions"
model       = "openai/gpt-4.1-mini"
api_key_env = "GITHUB_TOKEN"
max_tokens  = 400
```

Lo stesso `provider = "openai"` funziona con OpenAI o con un proxy locale:
cambiano solo `endpoint`, `model` e `api_key_env`. `check` stampa provider,
modello e variabile della chiave effettivamente in uso.

---

## Validazione (nell'ordine)

Da eseguire sul NUC. I primi comandi non mandano niente a nessuno.

### 1. La configurazione sta in piedi (con il source dell'environment)

```bash
cd /opt/meteo-agent
sudo -u meteo ./venv/bin/meteo-agent -c /etc/meteo-agent/config.toml [--env /etc/meteo-agent/meteo-agent.env] check
```

Compila tutte le espressioni delle regole e ti dice quali variabili usano.
Non tocca la rete. Se una regola ha un errore di sintassi lo scopri qui, non
in produzione fra tre settimane.

> `check` legge i segreti dall'ambiente. Se lo lanci a mano:
> `sudo -u meteo env $(sudo cat /etc/meteo-agent/meteo-agent.env | xargs) ./venv/bin/meteo-agent ... check`
> oppure passa `--env /etc/meteo-agent/meteo-agent.env`.

### 2. Quale stazione ARPAE stiamo leggendo

```bash
sudo -u meteo ./venv/bin/meteo-agent -c /etc/meteo-agent/config.toml [--env /etc/meteo-agent/meteo-agent.env] stations -v > arpae-stations.txt
```

Scarica il feed, cerca le stazioni entro 15 km e stampa **cosa pubblicano
davvero**: codice tabella B, livello e timerange di ogni canale. Le stazioni
utilizzabili per il vento sono marcate con `*` prima della distanza.
[TIP: pe3r cercarla puoi usare `grep -B 5 -A 2 wind_ arpae-stations.txt`]

Questo è il comando più importante della lista. La rete ARPAE non è omogenea:
non tutte le stazioni hanno l'anemometro, e quelle che ce l'hanno non usano tutte
lo stesso timerange. Guarda l'output, scegli la stazione, e **pinnala** in
`/etc/meteo-agent/config.toml`:

```toml
[arpae]
station_lon     = 1134677
station_lat     = 4449420
station_network = "simnbo"
```

Il comando ti stampa già il blocco pronto da incollare. Senza pin l'agente sceglie
da solo la più vicina e te lo scrive nel log — comodo il primo giro, ma a regime
se quella stazione cade offline ne leggeresti un'altra senza accorgertene.

Pinnare la stazione serve anche al traffico: il feed ARPAE è un unico file di
~8,6 MB con tutte le stazioni della regione, e il server non lo comprime. Con
la stazione pinnata l'agente lo legge in streaming e tiene solo le righe della
tua stazione (qualche KB in memoria invece del file intero); senza pin deve
leggerlo tutto per scegliere. In più chiede il file solo se è cambiato
(`ETag`): se non lo è, il server risponde `304` senza mandare niente.

Il download resta comunque il grosso del traffico, ~825 MB al giorno a ogni
ciclo. Per ridurlo:

```toml
[arpae]
interval_minutes = 30   # 0 = a ogni ciclo (default); multiplo di interval_minutes
```

Il feed tiene circa 6 ore di storico, quindi scaricarlo ogni 30 o 60 minuti non
perde dati: le regole su osservato vedono gli stessi istanti, solo fino a 30 o
60 minuti più tardi. Le regole su previsione restano a ogni ciclo. Un download
fallito non consuma la finestra: si ritenta al ciclo dopo.

Se `--radius 15` non trova niente con il vento, allarga: `--radius 30`.

### 3. Telegram

```bash
sudo -u meteo ./venv/bin/meteo-agent -c /etc/meteo-agent/config.toml [--env /etc/meteo-agent/meteo-agent.env] telegram-test
```

Ti arriva un messaggio di prova. Se fallisce, il log dice il perché
(`chat not found` = chat_id sbagliato, `Unauthorized` = token sbagliato).

### 4. Un ciclo completo, senza mandare niente

```bash
sudo -u meteo ./venv/bin/meteo-agent -c /etc/meteo-agent/config.toml [--env /etc/meteo-agent/meteo-agent.env] once --dry-run
```

Fa tutto — scarica, archivia, valuta le regole, compone i messaggi — ma non invia
e non salva lo stato delle regole. I messaggi che *avrebbe* mandato finiscono nel
log, preceduti da `[dry-run] notification NOT sent`. È il comando con cui tarare le
soglie.

Fronte e cooldown valgono anche qui: una regola già notificata, o in cooldown,
non compone niente, esattamente come non partirebbe nel servizio vero. Se il
redattore LLM è attivo, il dry-run lo chiama davvero, per mostrarti il testo
completo.

### 5. Cosa c'è in archivio

```bash
sudo -u meteo ./venv/bin/meteo-agent -c /etc/meteo-agent/config.toml status
```

Righe, snapshot, intervallo temporale coperto, variabili effettivamente presenti
per sorgente, stato di ogni regola.

Se dopo il primo ciclo `wind_gusts_direction_10m` non compare fra le variabili
osservate, la tua stazione non pubblica B11043 e la regola
`raffiche_est_in_atto` non scatterà mai: cambia stazione o togli la regola da config.toml.
(sezione `--- Raffiche da E/SE in atto (direzione vera, B11043) ----------------------`)

### 6. Ritarare le regole sui dati già scaricati

```bash
sudo -u meteo ./venv/bin/meteo-agent -c /etc/meteo-agent/config.toml rules-test
```

Rivaluta tutto sull'archivio, senza rete e senza notifiche. Cambi una soglia,
rilanci, vedi cosa sarebbe scattato. Loop di taratura da un secondo.

---

## Avvio come servizio

```bash
sudo cp systemd/meteo-agent.service systemd/meteo-agent-failure.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meteo-agent

systemctl status meteo-agent
journalctl -u meteo-agent -f
```

I cicli sono allineati al clock: cadono a :00, :15, :30, :45. Al riavvio il primo
ciclo parte subito, senza aspettare il quarto d'ora.

Se l'agente va in crash loop (5 riavvii in 10 minuti) systemd smette di
riavviarlo e lancia `meteo-agent-failure.service`, che ti manda un messaggio
Telegram con `curl`, indipendente dalla venv dell'agente. Senza, un agente
fermo è indistinguibile da un periodo senza allerte. Per provarlo:
`sudo systemctl start meteo-agent-failure.service`. Non copre il NUC spento o
senza rete.

Se l'invio Telegram di un'allerta fallisce, la regola resta "da notificare" e
si ritenta al ciclo successivo: un'allerta non si perde per un errore di rete.

L'unit è in `ProtectSystem=strict`: l'unica directory scrivibile è
`/var/lib/meteo-agent`. Se cambi `[store].path` devi aggiornare `ReadWritePaths=`.

Per controllare l'hardening: `systemd-analyze security meteo-agent`.

---

## Aggiornare il servizio (deploy)

Il servizio gira dalla **copia** in `/opt/meteo-agent`, non dal workspace: una
modifica al codice non arriva all'agente finché non la copi lì. Da eseguire
sul NUC, dalla radice del repository, in quest'ordine.

### 1. Verificare nel workspace

```bash
source .venv/bin/activate
python -m pytest                     # devono passare tutti, nessuno tocca la rete
git status                           # sai cosa stai per mettere in produzione
```

### 2. Copiare codice e unit

```bash
# --delete: un file tolto dal repository sparisce anche da /opt
sudo rsync -a --delete --exclude __pycache__ meteoagent/ /opt/meteo-agent/meteoagent/
sudo cp pyproject.toml /opt/meteo-agent/
sudo /opt/meteo-agent/venv/bin/pip install -q -e /opt/meteo-agent

sudo cp systemd/meteo-agent.service systemd/meteo-agent-failure.service /etc/systemd/system/
sudo systemctl daemon-reload
```

Il `pip install` serve solo se sono cambiate le dipendenze in `pyproject.toml`,
ma è idempotente e costa pochi secondi: tanto vale lanciarlo sempre. Se fallisce
con errori sulla versione di Python, la venv va ricreata: vedi la nota in
[Codice e virtualenv](#2-codice-e-virtualenv).

### 3. Controllare la configurazione con il codice nuovo

Prima di riavviare, così un errore lo vedi qui e non in un crash loop:

```bash
sudo -u meteo /opt/meteo-agent/venv/bin/meteo-agent -c /etc/meteo-agent/config.toml \
    --env /etc/meteo-agent/meteo-agent.env check
```

Le opzioni nuove hanno default compatibili: una `config.toml` che funzionava
continua a funzionare (es. la sezione `[claude]` resta valida come nome storico
di `[llm]`). Per vedere cosa offre la versione nuova:
`diff /etc/meteo-agent/config.toml config.example.toml`.

### 4. Riavviare e verificare

```bash
sudo systemctl reset-failed meteo-agent    # azzera il contatore se era in crash loop
sudo systemctl restart meteo-agent
journalctl -u meteo-agent -n 30 --no-pager
```

Nel log devi vedere `agent ready: N rules ...` e, entro pochi secondi, il primo
`cycle completed`. Se cambi qualcosa in Telegram o nel redattore, una prova
senza aspettare un'allerta vera:
`sudo -u meteo /opt/meteo-agent/venv/bin/meteo-agent -c /etc/meteo-agent/config.toml --env /etc/meteo-agent/meteo-agent.env telegram-test`.

Per provare l'avviso di agente fermo (manda davvero il messaggio "si è fermato"):
`sudo systemctl start meteo-agent-failure.service`.

### Tornare indietro

```bash
git log --oneline                    # trova il commit che funzionava
git checkout <commit>
# ...e ripeti i passi 2-4
git checkout master                  # poi torna sul ramo
```

Finché `SCHEMA_VERSION` in `store.py` resta lo stesso fra le due versioni, il
rollback non tocca i dati. Se un commit lo cambia, controlla prima cosa fa.

---

## Scrivere le regole

```toml
[[rules]]
name       = "vento_est_previsto"
title      = "Vento da E/SE in arrivo"
source     = "forecast"        # forecast (guarda avanti) | observation (guarda indietro)
horizon_h  = 48
when       = 'dir_in(wind_direction_10m, ["NE","ENE","E","ESE","SE"]) and wind_speed_10m > 20'
mode       = "consecutive"
min_hours  = 2
cooldown_h = 12
trigger    = "edge"
```

### `when`

Espressione booleana valutata su **ogni istante** della finestra. Niente `eval`:
viene compilata in AST e accetta solo i nodi in whitelist, quindi un file di
configurazione non può eseguire codice. Gli errori si vedono al `check`.

Variabili (unità canoniche, identiche per previsione e osservato):

| variabile | unità | previsione | osservato |
|---|---|:---:|:---:|
| `temperature_2m` | °C | ✓ | ✓ |
| `relative_humidity_2m` | % | ✓ | ✓ |
| `precipitation` | mm | ✓ | ✓ |
| `pressure_msl` | hPa | ✓ | ✓ |
| `wind_speed_10m` | km/h | ✓ | ✓ |
| `wind_direction_10m` | gradi | ✓ | ✓ |
| `wind_gusts_10m` | km/h | ✓ | ✓ |
| `wind_gusts_direction_10m` | gradi | — | ✓ |

Derivate su finestra mobile, calcolate automaticamente:
`precipitation_sum_3h`, `_6h`, `_24h`, `wind_gusts_max_3h`.

Funzioni: `dir_in(gradi, ["NE","E"])`, `dir_between(gradi, 30, 150)`,
`sector(gradi)`, `min`, `max`, `abs`, `round`.

Operatori: `and or not`, `< <= > >= == != in`, `+ - * / %`, confronti
concatenati (`0 < temperature_2m < 5`).

I settori sono a 16 punti, ampi 22,5° e **centrati** sul nome: `E` copre
78,75°–101,25°. `dir_in(d, ["NE","ENE","E","ESE","SE"])` copre 33,75°–146,25°.
Si accetta anche la notazione italiana `O` per Ovest.

### `mode` — il quantificatore

Cosa deve succedere nella finestra perché la condizione conti:

- `any` — almeno un istante
- `all` — tutti gli istanti
- `count` — almeno `min_hours` ore di istanti, anche sparsi
- `consecutive` — almeno `min_hours` ore **di fila**

`min_hours` è in ore e viene convertito nel numero di campioni sul passo reale
della serie: 2 ore sono 2 campioni su una previsione oraria e 8 su un osservato a
15 minuti. Serve a non far scattare un allarme per un'ora isolata di modello
nervoso.

Un istante con variabili mancanti viene **saltato**, non considerato falso: un
buco di dati non deve travestirsi da "tutto a posto".

### `trigger` e `cooldown_h` — l'antispam

Senza questi, una condizione vera per 12 ore genererebbe 48 notifiche.

- `trigger = "edge"` — notifica solo sulla transizione falso → vero. Un evento,
  una notifica.
- `trigger = "level"` — notifica a ogni ciclo in cui è vera, se il cooldown è
  scaduto.
- `cooldown_h` — silenzio minimo fra due notifiche della stessa regola.

Lo stato è persistente su SQLite: riavviare l'agente non ti rimanda le notifiche
già uscite. Un fronte soppresso dal cooldown non viene perso: esce appena il
cooldown scade.

---

## Il ruolo dell'LLM (opzionale, disattivato di default)

Volutamente ristretto: riceve in JSON i dati che hanno **già** fatto scattare la
regola e scrive due frasi. Non decide se notificare, non interpreta le soglie, non
vede altro che i valori delle variabili citate dalla regola.

La ragione è che una condizione booleana deve dare lo stesso esito sugli stessi
dati, oggi e fra sei mesi. Un LLM in quel punto renderebbe il sistema non
verificabile.

Se l'API è lenta, in errore, o senza credito, la notifica **parte lo stesso**, con
la sola parte deterministica. Un agente di allerta non deve dipendere da un
servizio esterno per dirti che sta per tirare vento.

Il messaggio ha sempre: titolo, sorgente, ora di aggiornamento, e la tabella degli
istanti che hanno fatto scattare la regola con i valori delle variabili guardate.

---

## Sviluppo

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q     # 176 test, ~0,4 s
```

Struttura:

```
meteoagent/
├── config.py        TOML + segreti da env, validazione stretta
├── expr.py          valutatore AST in whitelist
├── vars.py          vocabolario delle variabili canoniche
├── store.py         SQLite, finestra mobile, stato regole
├── http.py          sessione con retry/backoff
├── sources/
│   ├── openmeteo.py previsioni
│   └── arpae.py     osservazioni: parsing RMAP, QC, scelta stazione
├── rules.py         derivate, quantificatori, fronte, cooldown
├── message.py       composizione deterministica
├── redactor.py      LLM (Anthropic o compatibile OpenAI), con degrado controllato
├── telegram.py      sendMessage / getUpdates
├── agent.py         orchestrazione e scheduler
└── __main__.py      CLI

config.example.toml     configurazione di produzione (db in /var/lib, soglie reali)
config.dev.toml         configurazione di sviluppo (db nel workspace, soglie basse)
meteo-agent.env.example modello per i segreti
systemd/                unit del servizio, con hardening, e avviso Telegram se si ferma
.vscode/                interprete, test e configurazioni di debug
tests/                  176 test, nessuno tocca la rete
```

I test coprono i pezzi che non dipendono dalla rete: valutatore (incluso il
rifiuto di costrutti pericolosi), quantificatori, fronte/cooldown, finestra
mobile, parsing RMAP con feed sintetico, controllo qualità B33007/B33196,
conversioni di unità. **L'integrazione con i feed live va validata sul NUC** con
la sequenza sopra.

### Provare dal workspace, con VSCode Remote sul NUC

`config.dev.toml` è pensato per questo: scrive il database in `./data/`, alza il
log a DEBUG e tiene `[llm].enabled = false`, così l'agente gira **senza root,
senza systemd e senza chiave LLM**. Le soglie sono abbassate rispetto a
`config.example.toml` per vedere qualcosa scattare anche in una giornata
tranquilla: **vanno ritarate** prima di passare in produzione.

Dal terminale integrato di VSCode, collegato al NUC:

```bash
cd meteo-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp meteo-agent.env.example .env      # letto in automatico: sta accanto al config
$EDITOR .env                         # almeno TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID

python -m pytest                     # 176 test, devono passare tutti
python -m meteoagent -c config.dev.toml check
python -m meteoagent -c config.dev.toml stations -v      # <-- il primo vero test di rete
python -m meteoagent -c config.dev.toml once --dry-run
```

Poi <kbd>Ctrl+Shift+P</kbd> → *Python: Select Interpreter* → `./.venv/bin/python`.
Da lì il pannello **Testing** trova i 176 test e **Run and Debug** (<kbd>F5</kbd>)
ha già i sei comandi pronti con breakpoint: `check`, `stations -v`,
`once --dry-run`, `rules-test`, `status`, `run`.

Il punto su cui mettere il primo breakpoint, se qualcosa non torna, è
`arpae.extract()`: è lì che i codici della tabella B diventano variabili
canoniche, ed è l'unico pezzo che non ho potuto provare contro il feed vero.

`.gitignore` esclude già `.env` e `data/`. Il `.env` non deve finire in git:
in produzione lo stesso file va in `/etc/meteo-agent/meteo-agent.env` a `0640
root:meteo`, letto solo da systemd.

---

## Riferimenti

- Open-Meteo — <https://open-meteo.com/en/docs>
- ARPAE, dati osservati — <https://dati.arpae.it/dataset/dati-dalle-stazioni-meteo-locali-della-rete-idrometeorologica-regionale>
- RFC RMAP (data model, formato JSON, QC) — <https://doc.rmap.cc/rmap_rfc/rfc.html>
- Tabella B — <https://github.com/ARPA-SIMC/dballe/blob/master/tables/dballe.txt>
- Livelli e timerange — <https://arpa-simc.github.io/dballe/general_ref/ltypes.html>, <https://arpa-simc.github.io/dballe/general_ref/tranges.html>
- Telegram Bot API — <https://core.telegram.org/bots/api>

Dati ARPAE: licenza CC-BY.

---

## Verso il C++

Il POC è strutturato per essere tradotto senza sorprese. La mappa:

| modulo | equivalente C++ |
|---|---|
| `http.py` | libcurl (`CURLOPT_*`, retry a mano) |
| `sources/*` | nlohmann/json |
| `store.py` | SQLite3 C API, statement preparati |
| `expr.py` | il pezzo di lavoro vero: parser a discesa ricorsiva su un AST tuo |
| `rules.py` | traduzione diretta |
| `redactor.py` | POST JSON via libcurl |
| logging | spdlog |
| `config.py` | toml++ |

Il punto delicato è `expr.py`: in Python il modulo `ast` regala il parser, in C++
va scritto. Sono circa 300 righe per la grammatica supportata. In alternativa si
congela la grammatica in una struct compilata dal TOML, perdendo espressività ma
evitando il parser.

Il resto è meccanico. Lo schema del database non cambia: il DB scritto dal POC
resta leggibile dalla versione C++, così si può girare in parallelo e confrontare.

---

## Disinstallazione e Pulizia Sistema

Procedura inversa rispetto a [Installazione su Ubuntu 24.04](#installazione-su-ubuntu-2404):
rimuove servizio, codice, dati, configurazione e utente di sistema. Da eseguire
sul NUC, in quest'ordine.

### 0. Backup (opzionale)

Il passo 3 cancella storico e configurazione senza ritorno. Se pensi di
reinstallare, o vuoi tenere i dati:

```bash
mkdir -p ~/meteo-agent-backup
sudo cp /var/lib/meteo-agent/meteo.db /etc/meteo-agent/config.toml ~/meteo-agent-backup/
sudo chown -R "$USER": ~/meteo-agent-backup
```

Il file dei segreti (`/etc/meteo-agent/meteo-agent.env`) è escluso di
proposito: copialo solo se reinstalli, e non lasciarlo in giro nella home.

### 1. Fermare e disabilitare il servizio

```bash
sudo systemctl stop meteo-agent
sudo systemctl disable meteo-agent
sudo rm -f /etc/systemd/system/meteo-agent.service /etc/systemd/system/meteo-agent-failure.service
sudo systemctl daemon-reload
sudo systemctl reset-failed
```

Verifica che non giri più nulla:

```bash
systemctl status meteo-agent   # deve dare "could not be found" o "inactive"
```

### 2. Rimuovere codice e virtualenv

```bash
sudo rm -rf /opt/meteo-agent
```

### 3. Rimuovere dati e configurazione

Il database SQLite (osservazioni, snapshot di previsione, stato delle regole) e
i segreti (Telegram, chiave LLM) vivono fuori da `/opt`, quindi vanno rimossi a
parte. **Questo passo cancella tutto lo storico**: se serve un backup, vedi il
[passo 0](#0-backup-opzionale).

```bash
sudo rm -rf /var/lib/meteo-agent
sudo rm -rf /etc/meteo-agent
```

### 4. Rimuovere l'utente di servizio

```bash
sudo userdel meteo
```

Essendo un utente `--system --no-create-home`, non lascia una home da ripulire.
Se in passato l'hai creato diversamente e possiede ancora file altrove:

```bash
sudo find / -xdev -user meteo 2>/dev/null
```

### 5. Dipendenze di sistema (opzionale)

`python3-venv`, `python3-pip` e `git` sono pacchetti generici, quasi certamente
usati anche da altro sul NUC: **di norma non vanno rimossi**. Se sei sicuro che
servissero solo a questo agente:

```bash
sudo apt remove -y python3-venv python3-pip
sudo apt autoremove -y
```

### 6. Bot Telegram (opzionale)

L'agente non lascia tracce sul lato Telegram, ma se il bot non serve più:

1. Apri **@BotFather**, `/mybots`, seleziona il bot.
2. **Delete Bot** per cancellarlo definitivamente, oppure `/revoke` per
   invalidare solo il token corrente mantenendo il bot.

### 7. Chiave del redattore LLM (opzionale)

Se la chiave era dedicata a questo agente, revocala per evitare che resti
valida dopo la disinstallazione:

- `ANTHROPIC_API_KEY`: da <https://console.anthropic.com/settings/keys>;
- PAT GitHub (se usavi `provider = "openai"` con GitHub Models): da
  <https://github.com/settings/tokens>.

### 8. Workspace di sviluppo (opzionale)

I passi sopra non toccano la copia del repository da cui hai installato. Dentro
ci sono la venv di sviluppo (`.venv/`), l'eventuale database di sviluppo
(`data/`, creato da `config.dev.toml`) e il file dei segreti di sviluppo
(`.env`), tutti fuori da git:

```bash
cd meteo-agent
rm -rf .venv data .env
```

Per eliminare anche il repository, controlla prima con `git status` e
`git log origin/master..` di non avere modifiche o commit non ancora pushati:
cancellando la cartella andrebbero persi.

### Verifica finale

```bash
systemctl status meteo-agent    # unit inesistente
id meteo                        # utente inesistente
ls /opt/meteo-agent \
   /etc/meteo-agent \
   /var/lib/meteo-agent 2>&1    # "No such file or directory" su tutti e tre
```

Se tutti i controlli falliscono come atteso (cioè non trovano nulla), la
disinstallazione è completa e il sistema è tornato allo stato precedente
all'installazione.

## Licenza e Copyright

Copyright (c) 2026 Maurizio Bonesi

Questo progetto è open source ed è distribuito sotto licenza [MIT](LICENSE).

Sei libero di utilizzare, modificare e distribuire questo software per scopi personali, accademici o commerciali, 
a condizione che la nota sul copyright originale e l'attribuzione all'autore siano mantenute in tutte le copie o parti sostanziali del codice.
