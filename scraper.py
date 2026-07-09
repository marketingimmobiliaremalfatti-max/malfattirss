#!/usr/bin/env python3
"""
Scraper per Immobiliare Malfatti -> genera un feed RSS compatibile con Postpikr
per la pubblicazione automatica su Facebook/Instagram.

Come funziona:
1. Scarica le pagine di elenco annunci del sito pubblico.
2. Per ogni annuncio trovato, apre la pagina di dettaglio e legge i meta tag
   (Open Graph, standard SEO, o dati strutturati JSON-LD) per estrarre
   titolo, descrizione e immagine, con vari livelli di fallback.
3. Mantiene uno stato persistente (data/state.json) con la data di "prima
   vista" di ogni annuncio, così il feed RSS ha date stabili nel tempo e
   Postpikr non ripubblica lo stesso annuncio più volte.
4. Scrive il feed finale in docs/rss.xml (servito poi da GitHub Pages).
"""

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator

BASE_URL = "https://www.immobiliaremalfatti.it/"

RESULTS_PER_PAGE = 20
LIST_URLS = [
    f"elenco.aspx?tipoOfferta=33&prezzo=0&ric_libera=&n={RESULTS_PER_PAGE}&ord=0&contratto=0&comune=0",  # vendita residenziale
    f"elenco.aspx?tipoOfferta=34&prezzo=0&ric_libera=&n={RESULTS_PER_PAGE}&ord=0&contratto=0&comune=0",  # affitto residenziale
]

STATE_FILE = Path(__file__).parent / "data" / "state.json"
OUTPUT_FILE = Path(__file__).parent / "docs" / "rss.xml"
MAX_ITEMS_IN_FEED = 60

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MalfattiRSSBot/1.0; +https://www.immobiliaremalfatti.it/)"
}

DETAIL_URL_PATTERN = re.compile(r"[A-Za-z0-9\-]+-T\d+\.aspx$")


def title_from_slug(url):
    slug = url.rstrip("/").split("/")[-1]
    slug = re.sub(r"\.aspx$", "", slug, flags=re.IGNORECASE)
    slug = re.sub(r"-{2,}", " / ", slug)
    slug = slug.replace("-", " ")
    tokens = [t for t in slug.split(" ") if t]

    if tokens and re.match(r"^T\d+$", tokens[-1], flags=re.IGNORECASE):
        tokens.pop()

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
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if DETAIL_URL_PATTERN.search(href):
                    page_urls.add(urljoin(BASE_URL, href))

            new_urls = page_urls - urls
            urls |= page_urls

            print(f"  pagina {page} ({paged_url}): {len(page_urls)} annunci unici sulla pagina, {len(new_urls)} nuovi")

            if len(page_urls) == 0 or len(page_urls) < RESULTS_PER_PAGE or len(new_urls) == 0:
                break
            page += 1
            if page > 30:
                break

    return sorted(urls)


def extract_meta(soup, prop):
    tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    return tag["content"].strip() if tag and tag.get("content") else None


def extract_jsonld(soup):
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

    for img in soup.find_all("img", src=True):
        src = img["src"]
        if any(x in src.lower() for x in ("logo", "icon", "favicon", "sprite")):
            continue
        return urljoin(page_url, src)

    return None


def scrape_listing(url):
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
    fg.load_extension("media")
    fg.title("Immobiliare Malfatti - Annunci")
    fg.link(href=BASE_URL, rel="alternate")
    fg.description("Feed automatico degli annunci pubblicati su immobiliaremalfatti.it")
    fg.language("it")

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

        desc_html = item["description"] or ""
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

        if listing_id in state:
            first_seen = state[listing_id]["first_seen"]
        else:
            first_seen = now_iso
            print(f"  [NUOVO] {data['title']} ({url})")

        state[listing_id] = {
            "first_seen": first_seen,
            "url": data["url"],
            "title": data["title"],
        }

        listings_with_dates.append({**data, "first_seen": first_seen})

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
