/* lotto_sqlite.sql - il file di scambio SQLite di un lotto (lo stesso schema che crea "python lotto.py crea")

   CREARE IL FILE SQLITE IN DBEAVER
     1. Database > Nuova connessione (spina con il +) > SQLite > Avanti
     2. "Path": scrivi il percorso di un file che NON esiste ancora, es. C:\lotti\lotto_001.db
        (se DBeaver chiede di scaricare il driver SQLite, conferma)
     3. Fine: alla prima connessione il file viene creato, vuoto
     4. Tasto destro sulla connessione > Editor SQL > Apri script SQL,
        incolla tutto questo file ed eseguilo come script (Alt+X)

   In alternativa, sul computer di doc2md:  python lotto.py crea lotto_001.db  (stesso risultato)
   Un file per lotto (lotto_001.db, lotto_002.db, ...): si copia, si archivia, si cancella.
*/

-- i documenti da convertire: li riempie l'esportazione da SQL Server (query A in lotto_sqlserver.sql)
CREATE TABLE IF NOT EXISTS documenti (
    chiave    TEXT PRIMARY KEY NOT NULL,   -- id del record d'origine, come testo
    nome      TEXT,                        -- facoltativo: il tipo lo riconosce doc2md dal contenuto
    contenuto BLOB,
    meta      TEXT,                        -- facoltativo, testo libero
    atteso    TEXT,                        -- facoltativo: testo principale atteso (colonna "test"),
                                           -- lotto.py lo confronta col Markdown prodotto
    url       TEXT                         -- la scrive lotto.py per i documenti letti da un link
);

-- i risultati: li scrive lotto.py, uno per documento
CREATE TABLE IF NOT EXISTS risultati (
    chiave            TEXT PRIMARY KEY,
    stato             TEXT NOT NULL CHECK (stato IN ('fatto', 'errore')),
    tipo              TEXT,                -- pdf, docx, xlsx, doc, png, ... riconosciuto dal contenuto
    doc_id            TEXT UNIQUE,         -- la chiave in base64url: dal .md si risale all'origine
    markdown          TEXT,
    pagine            INTEGER,
    metodo            TEXT,                -- JSON, es. {"nativa": 3, "ocr": 1}
    jev               REAL,                -- solo con lotto.py --jev
    jev_da_verificare INTEGER,
    immagini          INTEGER,
    secondi           REAL,
    errore            TEXT,
    rapporto          TEXT,                -- JSON completo di doc2md
    convertito        TEXT,                -- data e ora ISO 8601 con fuso, es. 2026-11-19T18:40:00+01:00
    verifica          REAL,                -- quota del testo atteso ritrovata nel Markdown (0-1)
    mancanti          TEXT,                -- parole del testo atteso che mancano piu' spesso
    completezza       REAL,                -- quota del testo del PDF ritrovata nel Markdown (0-1)
    mancanti_pdf      TEXT                 -- parole del PDF che mancano piu' spesso nel Markdown
);

-- le immagini: una riga per immagine DIVERSA, chiave = impronta del contenuto (SHA-256, 32 caratteri).
-- Il Markdown le cita come img/<hash>.<est>; i documenti che la contengono si trovano cercando l'hash
CREATE TABLE IF NOT EXISTS immagini (
    hash   TEXT PRIMARY KEY,
    mime   TEXT,
    dati   BLOB
);

-- informazioni sul lotto (modo cartella: "sorgente" = albero d'origine, usato dal visore)
CREATE TABLE IF NOT EXISTS info (
    chiave TEXT PRIMARY KEY,
    valore TEXT
);

CREATE INDEX IF NOT EXISTS risultati_stato ON risultati(stato);
