/* lotto_sqlserver.sql - andata e ritorno tra SQL Server e il file SQLite di un lotto, solo con DBeaver,
   senza tabelle di appoggio.

   IL GIRO
     0. (una volta sola)  crea in SQL Server le tabelle di destinazione: parte 0 qui sotto
     1. crea il file SQLite del lotto: lotto_sqlite.sql
     2. ESPORTA   SQL Server -> SQLite "documenti"                     parte 1, si lancia su SQL Server
     3. CONVERTI  sul computer di doc2md: python lotto.py db lotto_001.db
     4. IMPORTA   SQLite "risultati" e "immagini" -> SQL Server        parte 2, si lancia sul SQLite
     5. USA       JOIN tra il tuo archivio e i risultati               parte 3

   COME SI ESPORTA UNA QUERY VERSO UNA TABELLA DI UN ALTRO DB (DBeaver 6.3; vale per i passi 2 e 4)
     a. apri l'Editor SQL sulla connessione SORGENTE, scrivi la query ed eseguila (Ctrl+Invio).
        La griglia mostra solo le prime 200 righe: e' normale, l'esportazione rilegge tutto.
     b. tasto destro dentro la griglia dei risultati > "Export results..."
     c. tipo di destinazione: "Database" > Next
     d. "Target container": pulsante "Choose..." > la connessione di DESTINAZIONE
     e. nella riga della query, "Target": la tabella (menu a tendina o scritta a mano).
        "Mapping" deve dire "existing", MAI "create" (se dice create il nome e' sbagliato).
        Pulsante "Columns...": ogni colonna deve dire "existing" verso la colonna con lo stesso nome.
     f. impostazioni di estrazione e caricamento: vedi le note di ciascun passo
     g. Next > Proceed (o Start), poi aspetta il messaggio di fine
   DBeaver 6.3 non ha "metodo di sostituzione" ne' "ignora righe duplicate" (arrivati dopo):
   i doppioni li evitano le query stesse e le chiavi primarie.

   Sostituisci dbo.Archivio, id, nome_file, blob_doc con i nomi veri della tua tabella.
*/


/* ===========================================================================
   0. TABELLE DI DESTINAZIONE IN SQL SERVER (una volta sola, sulla connessione SQL Server)
      Contengono solo i documenti convertiti bene: gli errori restano nel file del lotto
      e il lotto successivo li ripropone da soli (query 1 li esclude solo se gia' "fatti").
   =========================================================================== */

CREATE TABLE dbo.doc2md_risultati (
    chiave            NVARCHAR(100)  NOT NULL,
    tipo              VARCHAR(12)    NOT NULL,     -- pdf, docx, xlsx, doc, png, ... dal contenuto
    doc_id            VARCHAR(255)   NOT NULL,     -- base64url della chiave, come nel Markdown
    markdown          NVARCHAR(MAX)  NOT NULL,
    pagine            INT            NULL,
    metodo            NVARCHAR(200)  NULL,         -- JSON, es. {"nativa": 3, "ocr": 1}
    jev               FLOAT          NULL,         -- solo con lotto.py --jev
    jev_da_verificare INT            NULL,
    immagini          INT            NULL,
    secondi           FLOAT          NULL,
    rapporto          NVARCHAR(MAX)  NULL,         -- JSON completo: interrogabile con JSON_VALUE
    convertito        DATETIMEOFFSET(0) NULL,
    verifica          FLOAT          NULL,         -- quota del testo atteso ritrovata (0-1), se c'era "test"
    mancanti          NVARCHAR(1000) NULL,         -- parole del testo atteso che mancano piu' spesso
    completezza       FLOAT          NULL,         -- quota del testo del PDF ritrovata nel Markdown (0-1)
    mancanti_pdf      NVARCHAR(1000) NULL,         -- parole del PDF che mancano piu' spesso
    CONSTRAINT PK_doc2md_risultati PRIMARY KEY (chiave),
    CONSTRAINT UQ_doc2md_risultati_doc_id UNIQUE (doc_id),
    CONSTRAINT CK_doc2md_risultati_rapporto CHECK (rapporto IS NULL OR ISJSON(rapporto) = 1)
);

-- una riga per immagine DIVERSA (il logo ripetuto in ogni referto e' una riga sola).
-- IGNORE_DUP_KEY: un'immagine gia' importata con un lotto precedente viene saltata in
-- silenzio invece di bloccare l'importazione (DBeaver 6.3 non sa ignorare i doppioni)
CREATE TABLE dbo.doc2md_immagini (
    hash    CHAR(32)       NOT NULL,               -- SHA-256 del contenuto; nel Markdown: img/<hash>.png
    mime    VARCHAR(20)    NOT NULL,
    dati    VARBINARY(MAX) NOT NULL,
    CONSTRAINT PK_doc2md_immagini PRIMARY KEY (hash) WITH (IGNORE_DUP_KEY = ON)
);


/* ===========================================================================
   1. ESPORTAZIONE  (si scrive sulla connessione SQL Server, destinazione SQLite > "documenti")
      Prima: un file SQLite NUOVO per ogni lotto (lotto_sqlite.sql oppure python lotto.py crea).
      Impostazioni (pagina "Settings" del wizard):
        Extraction:  Extract type = By segments, Segment size = 500   (BLOB: poca memoria per volta)
                     Open new connections = si', Select row count = si' (barra di avanzamento)
        Data load:   Truncate data load table before load = NO
                       (SI' solo se ripeti un'esportazione interrotta sullo stesso file)
                     Use transactions = si', Commit after insert of = 500
                     Open table editor on finish = no
      CAST: VARBINARY(MAX) e NVARCHAR(MAX) servono anche se le colonne sono dei vecchi tipi
      image / text / ntext; NULLIF(...) trasforma un "test" vuoto in NULL (nessuna verifica).
   =========================================================================== */

-- 1a. primo lotto: i primi 20.000 documenti
SELECT TOP (20000)
       CAST(a.id AS NVARCHAR(100))                          AS chiave,
       a.nome_file                                          AS nome,
       CAST(a.blob_doc AS VARBINARY(MAX))                   AS contenuto,
       NULL                                                 AS meta,
       NULLIF(LTRIM(RTRIM(CAST(a.test AS NVARCHAR(MAX)))), '') AS atteso
FROM dbo.Archivio AS a
WHERE a.blob_doc IS NOT NULL
ORDER BY a.id;

-- 1b. lotti successivi (quando dbo.doc2md_risultati esiste e contiene i lotti gia' importati):
--     i documenti gia' convertiti non escono piu', quelli finiti in errore si' e verranno ritentati
SELECT TOP (20000)
       CAST(a.id AS NVARCHAR(100))                          AS chiave,
       a.nome_file                                          AS nome,
       CAST(a.blob_doc AS VARBINARY(MAX))                   AS contenuto,
       NULL                                                 AS meta,
       NULLIF(LTRIM(RTRIM(CAST(a.test AS NVARCHAR(MAX)))), '') AS atteso
FROM dbo.Archivio AS a
WHERE a.blob_doc IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM dbo.doc2md_risultati AS r
                  WHERE r.chiave = CAST(a.id AS NVARCHAR(100)))
ORDER BY a.id;

-- 1c. controllo dopo l'esportazione: i tre numeri devono coincidere sui due lati
--     (se i byte sono meno su SQLite, qualche BLOB e' stato troncato: fermati e dimmelo)
-- su SQL Server (stesso filtro della query usata):
SELECT COUNT(*) AS documenti, SUM(CAST(DATALENGTH(x.blob_doc) AS BIGINT)) AS byte, COUNT(x.atteso) AS con_test
FROM (SELECT TOP (20000) a.blob_doc, NULLIF(LTRIM(RTRIM(CAST(a.test AS NVARCHAR(MAX)))), '') AS atteso
      FROM dbo.Archivio AS a WHERE a.blob_doc IS NOT NULL ORDER BY a.id) AS x;
-- sul SQLite del lotto:
--   SELECT COUNT(*) AS documenti, SUM(length(contenuto)) AS byte, COUNT(atteso) AS con_test FROM documenti;


/* ===========================================================================
   2. IMPORTAZIONE  (si scrive sulla connessione SQLite del lotto convertito, destinazione SQL Server)
      Due esportazioni:
        2a -> dbo.doc2md_risultati
        2b -> dbo.doc2md_immagini   (le immagini gia' presenti vengono saltate: IGNORE_DUP_KEY)
      Impostazioni: Extract type = By segments (500), Truncate = NO, Use transactions = si'.
      Ogni lotto si importa UNA volta sola in doc2md_risultati: un secondo import dello stesso
      file si ferma sulla chiave primaria (nessun doppione, ma l'operazione va in errore).
      Si importano solo i documenti "fatti": la query 1b non li riesporta.
   =========================================================================== */

-- 2a
SELECT chiave, tipo, doc_id, markdown, pagine, metodo, jev, jev_da_verificare,
       immagini, secondi, rapporto, convertito, verifica, mancanti, completezza, mancanti_pdf
FROM risultati
WHERE stato = 'fatto';

-- 2b
SELECT hash, mime, dati FROM immagini;

-- controllo dopo l'importazione: sul SQLite conta i "fatti"...
SELECT COUNT(*) AS fatti, (SELECT COUNT(*) FROM immagini) AS immagini FROM risultati WHERE stato = 'fatto';
-- ...e su SQL Server devono esserci tutti (ultima importazione = convertito piu' recente)
SELECT COUNT(*) AS risultati, (SELECT COUNT(*) FROM dbo.doc2md_immagini) AS immagini FROM dbo.doc2md_risultati;


/* ===========================================================================
   3. USO DEI RISULTATI (SQL Server)
   =========================================================================== */

-- il Markdown accanto al record d'origine
SELECT a.id, a.nome_file, r.tipo, r.pagine, r.markdown
FROM dbo.Archivio AS a
JOIN dbo.doc2md_risultati AS r ON r.chiave = CAST(a.id AS NVARCHAR(100));

-- da un file .md all'origine: il doc_id e' nell'intestazione del Markdown (doc_id: "...")
SELECT a.*
FROM dbo.doc2md_risultati AS r
JOIN dbo.Archivio AS a ON r.chiave = CAST(a.id AS NVARCHAR(100))
WHERE r.doc_id = 'NDI';                       -- NDI = base64url di "42"

-- decodificare un doc_id senza la tabella dei risultati (chiavi ASCII; con lettere accentate
-- serve una collation _UTF8, SQL Server 2019+, oppure: python lotto.py chiave <doc_id>)
DECLARE @d VARCHAR(255) = 'UFJBVC8yMDI0IDEwMDA';
DECLARE @b VARCHAR(260) = REPLACE(REPLACE(@d, '-', '+'), '_', '/') + REPLICATE('=', (4 - LEN(@d) % 4) % 4);
SELECT CAST(CAST('' AS XML).value('xs:base64Binary(sql:variable("@b"))', 'VARBINARY(MAX)') AS VARCHAR(255)) AS chiave;
-- -> PRAT/2024 1000

-- documenti non ancora convertiti (mai esportati, o finiti in errore: il perche' e' nel file del lotto,
-- python lotto.py stato lotto_001.db  oppure  python lotto.py esporta lotto_001.db -> CSV)
SELECT COUNT(*) AS da_convertire
FROM dbo.Archivio AS a
WHERE a.blob_doc IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM dbo.doc2md_risultati r WHERE r.chiave = CAST(a.id AS NVARCHAR(100)));

-- regioni lette male secondo Jev (solo lotti convertiti con --jev)
SELECT chiave, tipo, jev, jev_da_verificare FROM dbo.doc2md_risultati
WHERE jev_da_verificare > 0 ORDER BY jev;

-- un'immagine e i documenti che la contengono (il Markdown la cita come img/<hash>.<est>)
SELECT r.chiave, r.tipo FROM dbo.doc2md_risultati AS r
WHERE r.markdown LIKE '%img/' + '31236f149026842c289a852d44e0d16e' + '.%';

-- le immagini di un documento
SELECT i.hash, i.mime, DATALENGTH(i.dati) AS byte
FROM dbo.doc2md_risultati AS r
JOIN dbo.doc2md_immagini AS i ON r.markdown LIKE '%img/' + i.hash + '.%'
WHERE r.chiave = N'IU04BNNL5B2C';

-- documenti in cui doc2md avrebbe perso testo del PDF (dovrebbero essere zero o quasi)
SELECT chiave, pagine, completezza, mancanti_pdf FROM dbo.doc2md_risultati
WHERE completezza < 0.95 ORDER BY completezza;

-- distribuzione dei tipi riconosciuti
SELECT tipo, COUNT(*) AS documenti, SUM(pagine) AS pagine, AVG(secondi) AS secondi_medi
FROM dbo.doc2md_risultati GROUP BY tipo ORDER BY documenti DESC;
