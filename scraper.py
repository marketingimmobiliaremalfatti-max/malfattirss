#!/usr/bin/env python3
"""
Scraper per Immobiliare Malfatti -> genera un feed RSS compatibile con Postpikr
per la pubblicazione automatica su Facebook/Instagram.

Come funziona:
1. Scarica le pagine di elenco annunci del sito pubblico.
2. Per ogni annuncio trovato, apre la pagina di dettaglio e legge i meta tag
   Open Graph (og:title, og:description, og:image) che il sito già espone
   per le anteprime social -> è la fonte più stabile possibile, perché non
   dipende dalla struttura grafica interna della pagina.
3. Mantiene uno stato persistente (data/state.json) con la data di "prima
   vista" di ogni annuncio, così il feed RSS ha date stabili nel tempo e
   Postpikr non ripubblica lo stesso annuncio più volte.
4. Scrive il feed finale in docs/rss.xml (servito poi da GitHub Pages).
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from PIL import Image

BASE_URL = "https://www.immobiliaremalfatti.it/"

# URL pubblico dove GitHub Pages pubblica questo repository (docs/).
# Va aggiornato se cambi nome utente/repository.
PAGES_BASE_URL = "https://marketingimmobiliaremalfatti-max.github.io/malfattirss/"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = "claude-sonnet-4-6"

AGENCY_NAME = "Immobiliare Malfatti"
AGENCY_PHONE = "0746 497000"

TEMPLATE_PATH = Path(__file__).parent / "assets" / "template_vendita.png"
IMAGES_DIR = Path(__file__).parent / "docs" / "images"
# Area (sinistra, alto, destra, basso) del riquadro trasparente nel template
# dove va inserita la foto dell'immobile, in pixel sul canvas 1080x1080.
PHOTO_AREA = (0, 222, 1080, 1080)

# Categorie di annunci da includere. I codici tipoOfferta sono quelli usati
# dal motore di ricerca del sito (Real Software / Realsmart). Se il sito
# aggiunge altre categorie, basta aggiungere altre voci qui.
# NB: "n" = risultati per pagina, "p" = numero di pagina (parte da 1).
RESULTS_PER_PAGE = 20
LIST_URLS = [
    f"elenco.aspx?tipoOfferta=33&prezzo=0&ric_libera=&n={RESULTS_PER_PAGE}&ord=0&contratto=0&comune=0",  # vendita
]

# Parole chiave (cercate nello slug dell'URL) che identificano tipi di
# immobile da escludere dal feed: negozi, terreni, garage/box, affitti.
EXCLUDE_KEYWORDS = ("negozio", "affitto", "affitasi", "terreno", "garage", "box")

STATE_FILE = Path(__file__).parent / "data" / "state.json"
OUTPUT_FILE = Path(__file__).parent / "docs" / "rss.xml"
MAX_ITEMS_IN_FEED = 60  # numero massimo di annunci mantenuti nel feed

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MalfattiRSSBot/1.0; +https://www.immobiliaremalfatti.it/)"
}

# Pattern che identifica gli URL delle pagine di dettaglio annuncio,
# es: "Villa-in-vendita-roma-Rieti-T521.aspx"
DETAIL_URL_PATTERN = re.compile(r"[A-Za-z0-9\-]+-T\d+\.aspx$")


def is_excluded_listing(url):
    """Restituisce True se l'annuncio va escluso in base al tipo (negozio,
    terreno, garage/box, affitto), individuato dalle parole chiave nello slug."""
    slug = url.lower()
    return any(keyword in slug for keyword in EXCLUDE_KEYWORDS)


def title_from_slug(url):
    """Costruisce un titolo leggibile a partire dallo slug dell'URL, es:
    'Attico---Mansarda-in-vendita-roma-Cittaducale-T345.aspx'
    -> 'Attico / Mansarda in vendita Cittaducale'
    Serve perché il tag og:title del sito è generico (nome agenzia) e non
    specifico per annuncio.
    """
    slug = url.rstrip("/").split("/")[-1]
    slug = re.sub(r"\.aspx$", "", slug, flags=re.IGNORECASE)
    slug = re.sub(r"-{2,}", " / ", slug)  # doppi/tripli trattini -> slash (es. Attico/Mansarda)
    slug = slug.replace("-", " ")
    tokens = [t for t in slug.split(" ") if t]

    # Rimuove il riferimento numerico finale (es. T345)
    if tokens and re.match(r"^T\d+$", tokens[-1], flags=re.IGNORECASE):
        tokens.pop()

    # Rimuove il token "roma" (placeholder fisso del sito, non è la località reale)
    tokens = [t for t in tokens if t.lower() != "roma"]

    if not tokens:
        return None

    title = " ".join(tokens)
    return title[0].upper() + title[1:]


def fetch(url, retries=3, pause=2):
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as e:
            print(f"  [!] errore su {url} (tentativo {attempt+1}/{retries}): {e}", file=sys.stderr)
            time.sleep(pause)
    return None


def discover_listing_urls():
    """Scansiona le pagine di elenco e restituisce l'insieme di URL di dettaglio trovati."""
    urls = set()
    for list_url in LIST_URLS:
        full_url = urljoin(BASE_URL, list_url)
        page = 1
        while True:
            paged_url = f"{full_url}&p={page}"
            html = fetch(paged_url)
            if not html:
                break

            soup = BeautifulSoup(html, "html.parser")
            page_urls = set()
            raw_count = 0
            excluded_count = 0
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if not DETAIL_URL_PATTERN.search(href):
                    continue
                full = urljoin(BASE_URL, href)
                raw_count += 1
                if is_excluded_listing(full):
                    excluded_count += 1
                    continue
                page_urls.add(full)

            new_urls = page_urls - urls
            urls |= page_urls

            print(
                f"  pagina {page} ({paged_url}): {raw_count} annunci sulla pagina, "
                f"{excluded_count} esclusi (negozio/terreno/garage/affitto), "
                f"{len(new_urls)} validi nuovi"
            )

            # Si ferma quando la pagina non ha annunci, o ne ha meno del numero
            # massimo per pagina (ultima pagina) -- il conteggio "grezzo", non
            # quello filtrato, per non fermarsi prima solo perché una pagina
            # piena aveva molti annunci esclusi.
            if raw_count == 0 or raw_count < RESULTS_PER_PAGE:
                break
            page += 1
            if page > 30:  # limite di sicurezza anti-loop infinito
                break

    return sorted(urls)


def extract_meta(soup, prop):
    tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    return tag["content"].strip() if tag and tag.get("content") else None


def extract_jsonld(soup):
    """Restituisce il primo oggetto JSON-LD trovato nella pagina, se presente."""
    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict):
            return data
    return None


def extract_description(soup, jsonld):
    for prop in ("og:description", "twitter:description"):
        val = extract_meta(soup, prop)
        if val:
            return val
    tag = soup.find("meta", attrs={"name": "description"})
    if tag and tag.get("content"):
        return tag["content"].strip()
    if jsonld and jsonld.get("description"):
        return jsonld["description"]

    # Ultimo fallback: nessun meta-tag disponibile -> cerca nel testo visibile
    # della pagina il blocco più lungo e verosimilmente descrittivo,
    # escludendo footer/cookie/copyright e testi troppo corti.
    BLOCKLIST = (
        "cookie", "privacy", "copyright", "tutti i diritti",
        "p.iva", "partita iva", "iscriviti alla newsletter",
    )
    candidates = []
    for tag_name in ("p", "div", "span"):
        for el in soup.find_all(tag_name):
            # Salta i contenitori che hanno figli con lo stesso tag (evita di
            # prendere blocchi troppo grandi che includono l'intera pagina)
            if el.find(tag_name):
                continue
            text = el.get_text(" ", strip=True)
            if len(text) < 80:
                continue
            if any(b in text.lower() for b in BLOCKLIST):
                continue
            candidates.append(text)

    if candidates:
        return max(candidates, key=len)

    return ""


def extract_image(soup, jsonld, page_url):
    for prop in ("og:image", "twitter:image", "twitter:image:src"):
        val = extract_meta(soup, prop)
        if val:
            return urljoin(page_url, val)

    if jsonld:
        img = jsonld.get("image")
        if isinstance(img, list) and img:
            img = img[0]
        if isinstance(img, dict):
            img = img.get("url")
        if img:
            return urljoin(page_url, img)

    # Fallback: prima immagine "vera" nella pagina (esclude loghi/icone)
    for img in soup.find_all("img", src=True):
        src = img["src"]
        if any(x in src.lower() for x in ("logo", "icon", "favicon", "sprite")):
            continue
        return urljoin(page_url, src)

    return None


def parse_technical_fields(raw_description):
    """Estrae i campi tecnici (Prezzo, Metratura, Camere, Bagni, ecc.) dalla
    descrizione grezza tipo 'Prezzo: € 239.000 Metratura: 290 mq Camere: 4 ...'"""
    labels = [
        "Stato interno", "Classe energetica", "Spese condominiali",
        "Metratura", "Riscaldamento", "Terrazzo", "Balconi", "Ascensore",
        "Cucinotto", "Cucina", "Bagni", "Camere", "Piano", "Anno", "Prezzo", "IPE",
    ]
    pattern = "|".join(re.escape(l) for l in labels)
    matches = list(re.finditer(rf"({pattern}):\s*", raw_description))

    fields = {}
    for i, m in enumerate(matches):
        label = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw_description)
        value = raw_description[start:end].strip(" .;")
        if value:
            fields[label] = value
    return fields


def generate_narrative(title, raw_description, url):
    """Chiede a Claude di scrivere solo la sezione narrativa 'DESCRIZIONE',
    nello stile di un annuncio immobiliare caldo e professionale. Restituisce
    None se manca la API key o la chiamata fallisce."""
    if not ANTHROPIC_API_KEY or not raw_description:
        return None

    prompt = f"""Sei un copywriter immobiliare italiano. Scrivi SOLO la sezione
narrativa "DESCRIZIONE" di un annuncio, nello stile di questo esempio:

---
Nel cuore del centro storico della frazione Villa Colapietro di Leonessa,
proponiamo in vendita un'abitazione indipendente luminosa, sviluppata su tre
livelli, con giardino privato.
Al piano terra si trova una comoda e ampia cantina, ideale per il rimessaggio
o come spazio di servizio aggiuntivo. Al primo piano si accede al soggiorno
con angolo cottura, un bagno e un balcone da cui godere dell'atmosfera
tranquilla del borgo. Al secondo piano sono ricavate la camera da letto e la
cameretta, perfette per una coppia o una piccola famiglia.
La soluzione su più livelli garantisce una distribuzione funzionale degli
spazi, mentre la presenza del giardino rappresenta un plus di grande valore,
ideale per trascorrere momenti all'aperto in totale relax.
Un'opportunità concreta per chi cerca una prima casa, una residenza
secondaria o un investimento in un contesto autentico e tranquillo
dell'entroterra reatino, a un prezzo davvero accessibile.
Per maggiori informazioni o per fissare una visita, non esitare a
contattarci. Saremo felici di accompagnarti nella scoperta di questa
proprietà.
---

Regole:
- 3-5 brevi paragrafi, tono caldo e professionale, come nell'esempio
- Usa SOLO i dati tecnici forniti, NON inventare dettagli non presenti
  (es. non inventare piani, stanze o caratteristiche se non sono nei dati)
- Se i dati disponibili sono pochi, scrivi una descrizione più breve ma
  comunque coerente: meglio corta e accurata che lunga e inventata
- Chiudi con un invito a contattare l'agenzia per informazioni o una visita,
  simile all'ultimo paragrafo dell'esempio
- NON includere hashtag, NON includere il titolo, NON scrivere l'intestazione
  "DESCRIZIONE:" (viene aggiunta separatamente) -- scrivi solo il testo

Titolo annuncio: {title}
Dati tecnici disponibili: {raw_description}"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        parts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        narrative = "\n".join(parts).strip()
        return narrative or None
    except requests.RequestException as e:
        print(f"  [!] Errore generazione narrativa AI per {url}: {e}", file=sys.stderr)
        return None


def build_full_caption(title, fields, narrative, listing_url):
    """Assembla il post finale nel formato: titolo, caratteristiche
    principali (dati reali), descrizione narrativa, contatti agenzia."""
    lines = [title, "", "CARATTERISTICHE PRINCIPALI:"]

    if fields.get("Prezzo"):
        lines.append(f"Prezzo: {fields['Prezzo']}")
    if fields.get("Metratura"):
        lines.append(f"Superficie: {fields['Metratura']}")
    if fields.get("Camere"):
        lines.append(f"Camere: {fields['Camere']}")
    if fields.get("Bagni"):
        lines.append(f"Bagni: {fields['Bagni']}")

    if narrative:
        lines += ["", "DESCRIZIONE:", narrative]

    lines += ["", listing_url, AGENCY_NAME, AGENCY_PHONE]

    return "\n".join(lines)


def compose_branded_image(photo_url, listing_id):
    """Scarica la foto dell'annuncio e la inserisce nel template brandizzato
    (fascia rossa in alto, foto nell'area sottostante). Restituisce l'URL
    pubblico dell'immagine composta, o None se qualcosa va storto (in tal
    caso il feed userà comunque la foto originale)."""
    if not photo_url:
        return None

    out_filename = f"{listing_id}.jpg"
    out_path = IMAGES_DIR / out_filename

    # Se l'abbiamo già generata in un run precedente, la riusiamo senza
    # riscaricare/ricomporre nulla.
    if out_path.exists():
        return urljoin(PAGES_BASE_URL, f"images/{out_filename}")

    if not TEMPLATE_PATH.exists():
        print(f"  [!] Template non trovato in {TEMPLATE_PATH}", file=sys.stderr)
        return None

    try:
        resp = requests.get(photo_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        photo = Image.open(BytesIO(resp.content)).convert("RGB")
    except Exception as e:
        print(f"  [!] Errore scaricando la foto per il template ({listing_id}): {e}", file=sys.stderr)
        return None

    template = Image.open(TEMPLATE_PATH).convert("RGBA")
    canvas_w, canvas_h = template.size
    area_left, area_top, area_right, area_bottom = PHOTO_AREA
    area_w = area_right - area_left
    area_h = area_bottom - area_top

    # Ridimensiona la foto per riempire l'area mantenendo le proporzioni
    # (crop centrato, tipo "object-fit: cover" del CSS).
    photo_ratio = photo.width / photo.height
    area_ratio = area_w / area_h
    if photo_ratio > area_ratio:
        new_height = area_h
        new_width = int(new_height * photo_ratio)
    else:
        new_width = area_w
        new_height = int(new_width / photo_ratio)

    photo_resized = photo.resize((new_width, new_height), Image.LANCZOS)
    left = (new_width - area_w) // 2
    top = (new_height - area_h) // 2
    photo_cropped = photo_resized.crop((left, top, left + area_w, top + area_h))

    canvas = Image.new("RGBA", (canvas_w, canvas_h), (255, 255, 255, 255))
    canvas.paste(photo_cropped, (area_left, area_top))
    canvas.alpha_composite(template)

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out_path, "JPEG", quality=88)

    return urljoin(PAGES_BASE_URL, f"images/{out_filename}")


def scrape_listing(url):
    """Estrae i dati di un singolo annuncio dalla sua pagina di dettaglio."""
    html = fetch(url)
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")

    slug_title = title_from_slug(url)
    og_title = extract_meta(soup, "og:title")
    title = slug_title or og_title or (soup.title.string.strip() if soup.title else url)

    jsonld = extract_jsonld(soup)
    description = extract_description(soup, jsonld)
    image = extract_image(soup, jsonld, url)
    canonical = extract_meta(soup, "og:url") or url

    # Estrae un identificativo stabile dall'URL, es: T521
    m = re.search(r"-T(\d+)\.aspx", url)
    listing_id = m.group(1) if m else url

    return {
        "id": listing_id,
        "url": canonical,
        "title": title,
        "description": description,
        "image": image,
    }


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def build_feed(listings_with_dates):
    fg = FeedGenerator()
    fg.load_extension("media")  # per media:content, alcuni lettori RSS lo preferiscono all'enclosure
    fg.title("Immobiliare Malfatti - Annunci")
    fg.link(href=BASE_URL, rel="alternate")
    fg.description("Feed automatico degli annunci pubblicati su immobiliaremalfatti.it")
    fg.language("it")

    # Ordina dal più recente al più vecchio
    listings_with_dates.sort(key=lambda x: x["first_seen"], reverse=True)

    for item in listings_with_dates[:MAX_ITEMS_IN_FEED]:
        fe = fg.add_entry()
        fe.id(item["url"])
        fe.title(item["title"])
        fe.link(href=item["url"])
        fe.guid(item["url"], permalink=True)

        pub_date = datetime.fromisoformat(item["first_seen"])
        if pub_date.tzinfo is None:
            pub_date = pub_date.replace(tzinfo=timezone.utc)
        fe.pubDate(pub_date)

        raw_caption = item.get("caption") or item["description"] or ""
        desc_html = raw_caption.replace("\n", "<br/>")
        if item.get("image"):
            desc_html = f'<img src="{item["image"]}" /><br/>{desc_html}'
        fe.description(desc_html)

        if item.get("image"):
            try:
                fe.enclosure(item["image"], 0, "image/jpeg")
            except Exception:
                pass

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    fg.rss_file(str(OUTPUT_FILE), pretty=True)


def main():
    print("== Scoperta annunci ==")
    listing_urls = discover_listing_urls()
    print(f"Totale annunci unici trovati: {len(listing_urls)}")

    state = load_state()
    now_iso = datetime.now(timezone.utc).isoformat()

    listings_with_dates = []
    seen_ids = set()

    print("== Estrazione dettagli annunci ==")
    for url in listing_urls:
        data = scrape_listing(url)
        if not data:
            continue

        listing_id = data["id"]
        seen_ids.add(listing_id)
        previous = state.get(listing_id, {})

        if listing_id in state:
            first_seen = previous["first_seen"]
        else:
            first_seen = now_iso
            print(f"  [NUOVO] {data['title']} ({url})")

        # Genera solo la parte narrativa una volta per annuncio (cache), poi
        # la riusa nei run successivi (risparmia chiamate API e mantiene
        # coerenza). Il resto del post (caratteristiche, contatti) viene
        # sempre riassemblato con i dati più recenti.
        narrative = previous.get("narrative")
        if not narrative:
            narrative = generate_narrative(data["title"], data["description"], data["url"])
            if narrative:
                print(f"  [AI] Descrizione narrativa generata per {data['title']}")

        fields = parse_technical_fields(data["description"])
        caption = build_full_caption(data["title"], fields, narrative, data["url"])

        branded_image = compose_branded_image(data["image"], listing_id)
        image_for_feed = branded_image or data["image"]

        state[listing_id] = {
            "first_seen": first_seen,
            "url": data["url"],
            "title": data["title"],
            "narrative": narrative,
        }

        listings_with_dates.append({
            **data,
            "image": image_for_feed,
            "first_seen": first_seen,
            "caption": caption,
        })

    # Rimuove dallo stato gli annunci non più presenti sul sito (venduti/rimossi)
    removed = set(state.keys()) - seen_ids
    for rid in removed:
        print(f"  [RIMOSSO] {state[rid]['title']}")
        del state[rid]

    save_state(state)

    print("== Generazione feed RSS ==")
    build_feed(listings_with_dates)
    print(f"Feed scritto in: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
