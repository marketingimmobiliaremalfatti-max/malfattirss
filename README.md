# Feed RSS – Immobiliare Malfatti

Genera automaticamente un feed RSS degli annunci pubblicati su
`immobiliaremalfatti.it`, pensato per essere collegato a **Postpikr** e
pubblicare in automatico su Facebook e Instagram.

## Come funziona

- `scraper.py` scarica le pagine di elenco annunci, apre ogni pagina di
  dettaglio e legge i meta tag Open Graph (`og:title`, `og:description`,
  `og:image`) — gli stessi che il sito usa già per le anteprime sui social,
  quindi la fonte più stabile disponibile senza avere accesso al gestionale.
- `data/state.json` tiene traccia di quando ogni annuncio è stato visto la
  prima volta, così le date nel feed restano stabili e Postpikr non
  ripubblica lo stesso annuncio più volte.
- `docs/rss.xml` è il feed finale, aggiornato automaticamente.
- Il workflow GitHub Actions (`.github/workflows/generate-feed.yml`) esegue
  tutto ogni 4 ore, e pubblica il file `docs/rss.xml` con GitHub Pages.

## Setup (10 minuti)

1. **Crea un nuovo repository su GitHub** (può essere privato o pubblico,
   non importa perché GitHub Pages funziona in entrambi i casi con i piani
   gratuiti standard).
2. Carica tutti i file di questo progetto nel repository.
3. Vai su **Settings → Pages** del repository:
   - Source: `Deploy from a branch`
   - Branch: `main`, cartella `/docs`
   - Salva.
4. Vai su **Settings → Actions → General** e assicurati che i workflow
   abbiano permesso di scrittura (Read and write permissions), altrimenti
   il commit automatico del feed fallirà.
5. Fai partire il workflow manualmente la prima volta: tab **Actions** →
   "Aggiorna feed RSS Immobiliare Malfatti" → **Run workflow**.
6. Dopo 1-2 minuti, il tuo feed sarà disponibile all'indirizzo:

   ```
   https://<tuo-utente-github>.github.io/<nome-repository>/rss.xml
   ```

7. **Incolla questo URL in Postpikr** come sorgente RSS del canale che
   vuoi automatizzare (Facebook e/o Instagram).

## Nota importante sulla prima esecuzione

Il primo run marcherà **tutti** gli annunci attualmente online come "nuovi"
(stessa data di pubblicazione). Se non vuoi che Postpikr provi a pubblicarli
tutti insieme, puoi:
- Far girare il workflow una prima volta a mano *prima* di collegare Postpikr
  (così lo stato si popola), e collegare Postpikr solo al secondo run
  in poi, oppure
- Impostare in Postpikr un limite di post al giorno dal feed, così assorbe
  gradualmente il primo batch.

## Manutenzione futura

Il sito è basato su piattaforma Real Software/Realsmart. Se in futuro il
fornitore cambia la struttura delle pagine di elenco (ad es. il parametro
di paginazione), potrebbe essere necessario aggiornare la funzione
`discover_listing_urls()` in `scraper.py`. I meta tag Open Graph usati per i
dettagli sono uno standard web e molto più stabili nel tempo.
