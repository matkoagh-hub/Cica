"""
Scraper vlastníkov z CICA portálu ÚGKK SR
https://cica.vugk.sk/VL_vyber.aspx

Iteruje: Okres -> Katastrálne územie -> Prvé písmeno -> Priezvisko
Ukladá: vlastník (celé meno), obec, okres, kat_uzemie do SQLite + CSV
"""

import csv
import logging
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://cica.vugk.sk/VL_vyber.aspx"
DB_PATH = "owners.db"
CSV_PATH = "owners.csv"
LOG_PATH = "scraper.log"
DEBUG_HTML = "debug_page.html"
DELAY = 1.2  # sekundy medzi requestmi

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": BASE_URL,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS owners (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            vlastnik   TEXT NOT NULL,
            obec       TEXT,
            okres      TEXT,
            kat_uzemie TEXT,
            scraped_at TEXT DEFAULT (datetime('now')),
            UNIQUE(vlastnik, obec, okres, kat_uzemie)
        );

        CREATE TABLE IF NOT EXISTS progress (
            okres      TEXT,
            kat_uzemie TEXT,
            pismeno    TEXT,
            priezvisko TEXT,
            done       INTEGER DEFAULT 0,
            PRIMARY KEY (okres, kat_uzemie, pismeno, priezvisko)
        );
    """)
    conn.commit()


def is_done(conn: sqlite3.Connection, okres: str, kat: str, pismeno: str, priezvisko: str) -> bool:
    row = conn.execute(
        "SELECT done FROM progress WHERE okres=? AND kat_uzemie=? AND pismeno=? AND priezvisko=?",
        (okres, kat, pismeno, priezvisko),
    ).fetchone()
    return bool(row and row[0])


def mark_done(conn: sqlite3.Connection, okres: str, kat: str, pismeno: str, priezvisko: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO progress (okres, kat_uzemie, pismeno, priezvisko, done) VALUES (?,?,?,?,1)",
        (okres, kat, pismeno, priezvisko),
    )
    conn.commit()


def save_owner(conn: sqlite3.Connection, writer: csv.DictWriter, vlastnik: str, obec: str, okres: str, kat: str) -> None:
    try:
        conn.execute(
            "INSERT OR IGNORE INTO owners (vlastnik, obec, okres, kat_uzemie) VALUES (?,?,?,?)",
            (vlastnik, obec, okres, kat),
        )
        conn.commit()
        writer.writerow({"vlastnik": vlastnik, "obec": obec, "okres": okres, "kat_uzemie": kat})
    except sqlite3.Error as e:
        log.error("DB chyba pri ukladaní '%s': %s", vlastnik, e)


def parse_hidden(soup: BeautifulSoup) -> Dict[str, str]:
    hidden = {}
    for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION", "__EVENTTARGET", "__EVENTARGUMENT"):
        tag = soup.find("input", {"name": name})
        hidden[name] = tag["value"] if tag and tag.get("value") else ""
    return hidden


def select_options(soup: BeautifulSoup, select_id: str) -> List[Tuple[str, str]]:
    sel = soup.find("select", {"id": select_id}) or soup.find("select", {"name": select_id})
    if not sel:
        return []
    return [
        (opt.get("value", ""), opt.get_text(strip=True))
        for opt in sel.find_all("option")
        if opt.get("value", "").strip()
    ]


def get_text_field(soup: BeautifulSoup, field_id: str) -> str:
    tag = soup.find("input", {"id": field_id}) or soup.find("input", {"name": field_id})
    if tag:
        return tag.get("value", "").strip()
    tag = soup.find("span", {"id": field_id})
    if tag:
        return tag.get_text(strip=True)
    return ""


def do_request(session: requests.Session, payload: Dict, retries: int = 3) -> Optional[BeautifulSoup]:
    for attempt in range(1, retries + 1):
        try:
            resp = session.post(BASE_URL, data=payload, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            return BeautifulSoup(resp.content, "lxml")
        except requests.RequestException as e:
            wait = 2 ** attempt
            log.warning("Pokus %d/%d zlyhal: %s — čakám %ds", attempt, retries, e, wait)
            if attempt < retries:
                time.sleep(wait)
    log.error("Všetky pokusy zlyhali")
    return None


def debug_selects(soup: BeautifulSoup) -> None:
    """Vypíše všetky select elementy nájdené na stránke pre diagnostiku."""
    selects = soup.find_all("select")
    log.info("=== DEBUG: Nájdených %d select elementov ===", len(selects))
    for sel in selects:
        sid = sel.get("id", "")
        sname = sel.get("name", "")
        opts = sel.find_all("option")
        log.info("  SELECT id='%s' name='%s' — %d možností", sid, sname, len(opts))
        if opts:
            samples = [o.get_text(strip=True) for o in opts[:3]]
            log.info("    Ukážka: %s", samples)
    inputs = soup.find_all("input", {"type": ["text", "readonly"]})
    log.info("=== DEBUG: Nájdených %d text input polí ===", len(inputs))
    for inp in inputs:
        log.info("  INPUT id='%s' name='%s' value='%s'",
                 inp.get("id", ""), inp.get("name", ""), inp.get("value", "")[:50])


def discover_field_ids(soup: BeautifulSoup) -> Dict[str, str]:
    """Nájde skutočné ID/name atribúty formulárových polí."""
    debug_selects(soup)

    ids = {}
    selects = soup.find_all("select")

    for sel in selects:
        sid = sel.get("id", "")
        sname = sel.get("name", "")
        name = sname or sid
        sid_lower = sid.lower()
        sname_lower = sname.lower()

        # Hľadáme podľa ID/name atribútov
        if any(k in sid_lower or k in sname_lower for k in ["okres", "district"]) and \
           not any(k in sid_lower or k in sname_lower for k in ["kat", "uzem"]):
            ids["okres"] = name
        elif any(k in sid_lower or k in sname_lower for k in ["katastr", "uzem", "ku"]):
            ids["kat_uzemie"] = name
        elif any(k in sid_lower or k in sname_lower for k in ["pismen", "letter", "initial"]):
            ids["pismeno"] = name
        elif any(k in sid_lower or k in sname_lower for k in ["priezv", "surname", "lastname"]):
            ids["priezvisko"] = name
        elif any(k in sid_lower or k in sname_lower for k in ["lv", "list"]):
            ids["lv"] = name

        # Ak nenájdeme podľa ID, skúsime label
        if not ids.get("okres"):
            text_above = sel.find_previous(["label", "td", "th", "span", "div"])
            label = text_above.get_text(strip=True).lower() if text_above else ""
            if "okres" in label and "kat" not in label:
                ids["okres"] = name
            elif "katastr" in label:
                ids["kat_uzemie"] = name
            elif "písmen" in label or "pismen" in label:
                ids["pismeno"] = name
            elif "priezv" in label:
                ids["priezvisko"] = name

    for inp in soup.find_all("input"):
        itype = inp.get("type", "text").lower()
        if itype in ("hidden", "submit", "button", "checkbox", "radio"):
            continue
        iid = inp.get("id", "").lower()
        iname = inp.get("name", "").lower()
        fname = inp.get("name", "") or inp.get("id", "")
        if any(k in iid or k in iname for k in ["vlastnik", "owner", "vlastn"]):
            ids["vlastnik"] = fname
        elif any(k in iid or k in iname for k in ["obec", "municip", "village"]):
            ids["obec"] = fname

    log.info("Nájdené polia: %s", ids)
    return ids


def build_payload(hidden: Dict, event_target: str, extra: Dict) -> Dict:
    payload = {**hidden, "__EVENTTARGET": event_target, "__EVENTARGUMENT": ""}
    payload.update(extra)
    return payload


def select_vlastnik_type(session: requests.Session, soup: BeautifulSoup) -> BeautifulSoup:
    """
    Stránka môže mať na začiatku výber typu: vlastník / správca.
    Nájde radio button alebo link pre 'vlastník' a klikne naň cez postback.
    Vráti aktualizovanú soup (s formulárom pre vlastníka).
    """
    hidden = parse_hidden(soup)

    # Hľadáme radio buttony
    radios = soup.find_all("input", {"type": "radio"})
    log.info("DEBUG: Nájdených %d radio buttonov", len(radios))
    for r in radios:
        rval = r.get("value", "").lower()
        rname = r.get("name", "")
        rid = r.get("id", "").lower()
        log.info("  RADIO name='%s' id='%s' value='%s'", rname, rid, rval)
        if any(k in rval or k in rid for k in ["vlastn", "owner", "vl"]):
            log.info("Vyberám typ 'vlastník': name='%s' value='%s'", rname, r.get("value", ""))
            payload = {**hidden, "__EVENTTARGET": rname, "__EVENTARGUMENT": "", rname: r.get("value", "")}
            result = do_request(session, payload)
            if result:
                with open(DEBUG_HTML, "wb") as f:
                    f.write(result.encode() if isinstance(result, str) else b"")
                return result

    # Hľadáme linky / buttony s textom "vlastník"
    for tag in soup.find_all(["a", "button", "input"]):
        text = tag.get_text(strip=True).lower()
        href = tag.get("href", "")
        onclick = tag.get("onclick", "")
        if "vlastn" in text or "vlastn" in onclick.lower():
            log.info("Nájdený link/button pre vlastníka: '%s'", tag.get_text(strip=True))
            # Skúsime extrahovať __doPostBack parametre
            import re
            match = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", onclick)
            if match:
                target, argument = match.group(1), match.group(2)
                payload = {**hidden, "__EVENTTARGET": target, "__EVENTARGUMENT": argument}
                result = do_request(session, payload)
                if result:
                    return result

    log.info("Výber vlastník/správca nenájdený — pokračujem s aktuálnou stránkou")
    return soup


def run_scraper() -> None:
    session = requests.Session()
    session.headers.update(HEADERS)

    # --- Inicializácia stránky ---
    log.info("Načítavam hlavnú stránku...")
    try:
        resp = session.get(BASE_URL, timeout=30)
        resp.raise_for_status()
        log.info("HTTP status: %d, veľkosť odpovede: %d bajtov", resp.status_code, len(resp.content))
    except requests.RequestException as e:
        log.error("Nepodarilo sa načítať stránku: %s", e)
        return

    # Uložiť HTML pre diagnostiku
    with open(DEBUG_HTML, "wb") as f:
        f.write(resp.content)
    log.info("HTML uložený do %s — môžeš ho otvoriť v prehliadači pre kontrolu", DEBUG_HTML)

    soup = BeautifulSoup(resp.content, "lxml")
    title = soup.find("title")
    log.info("Nadpis stránky: %s", title.get_text(strip=True) if title else "N/A")

    # Vyber typ "vlastník" ak stránka vyžaduje výber vlastník/správca
    soup = select_vlastnik_type(session, soup)
    time.sleep(DELAY)

    field_ids = discover_field_ids(soup)

    # Fallback na bežné ASP.NET WebForms ID pre tento portál
    fld_okres = field_ids.get("okres", "ctl00$ContentPlaceHolder1$ddlOkres")
    fld_kat = field_ids.get("kat_uzemie", "ctl00$ContentPlaceHolder1$ddlKatastrUzem")
    fld_pismeno = field_ids.get("pismeno", "ctl00$ContentPlaceHolder1$ddlPrvePismeno")
    fld_priezvisko = field_ids.get("priezvisko", "ctl00$ContentPlaceHolder1$ddlPriezvisko")
    fld_vlastnik = field_ids.get("vlastnik", "ctl00$ContentPlaceHolder1$txtVlastnik")
    fld_obec = field_ids.get("obec", "ctl00$ContentPlaceHolder1$txtObec")

    log.info("Používam polia: okres='%s', kat='%s', pismeno='%s', priezvisko='%s'",
             fld_okres, fld_kat, fld_pismeno, fld_priezvisko)

    okresy = select_options(soup, fld_okres)
    if not okresy:
        # Skúsime nájsť najväčší select (pravdepodobne Okres)
        best = None
        best_count = 0
        for sel in soup.find_all("select"):
            sid = sel.get("name", "") or sel.get("id", "")
            opts = [o for o in sel.find_all("option") if o.get("value", "").strip()]
            if len(opts) > best_count:
                best_count = len(opts)
                best = sid
        if best and best_count > 2:
            log.info("Fallback: používam select '%s' s %d možnosťami ako Okres", best, best_count)
            fld_okres = best
            okresy = select_options(soup, fld_okres)

    if not okresy:
        log.error(
            "Nepodarilo sa nájsť dropdown Okres.\n"
            "Otvor súbor '%s' v prehliadači a skontroluj či sa stránka načítala správne.\n"
            "Možné príčiny: server vrátil chybovú stránku, vyžaduje cookies, alebo zmenil štruktúru.",
            DEBUG_HTML
        )
        return

    log.info("Nájdených %d okresov.", len(okresy))

    csv_file_exists = Path(CSV_PATH).exists()
    csv_fh = open(CSV_PATH, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(csv_fh, fieldnames=["vlastnik", "obec", "okres", "kat_uzemie"])
    if not csv_file_exists:
        writer.writeheader()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    try:
        hidden = parse_hidden(soup)

        for okres_val, okres_name in okresy:
            log.info("=== OKRES: %s ===", okres_name)

            payload = build_payload(hidden, fld_okres, {fld_okres: okres_val})
            soup2 = do_request(session, payload)
            if soup2 is None:
                continue
            hidden = parse_hidden(soup2)
            time.sleep(DELAY)

            katy = select_options(soup2, fld_kat)
            if not katy:
                log.warning("  Žiadne katastrálne územia pre %s", okres_name)
                continue

            for kat_val, kat_name in katy:
                log.info("  KAT.ÚZEMIE: %s", kat_name)

                payload = build_payload(hidden, fld_kat, {
                    fld_okres: okres_val,
                    fld_kat: kat_val,
                })
                soup3 = do_request(session, payload)
                if soup3 is None:
                    continue
                hidden = parse_hidden(soup3)
                time.sleep(DELAY)

                obec = get_text_field(soup3, fld_obec)
                pismena = select_options(soup3, fld_pismeno)
                if not pismena:
                    log.warning("    Žiadne písmená pre %s / %s", okres_name, kat_name)
                    continue

                for pism_val, pism_name in pismena:
                    log.info("    PÍSMENO: %s", pism_name)

                    payload = build_payload(hidden, fld_pismeno, {
                        fld_okres: okres_val,
                        fld_kat: kat_val,
                        fld_pismeno: pism_val,
                    })
                    soup4 = do_request(session, payload)
                    if soup4 is None:
                        continue
                    hidden = parse_hidden(soup4)
                    time.sleep(DELAY)

                    priezviска = select_options(soup4, fld_priezvisko)
                    if not priezviска:
                        log.info("      Žiadne priezviská pre písmeno %s", pism_name)
                        continue

                    for priezv_val, priezv_name in priezviска:
                        if is_done(conn, okres_name, kat_name, pism_name, priezv_name):
                            log.debug("      SKIP (hotové): %s", priezv_name)
                            continue

                        payload = build_payload(hidden, fld_priezvisko, {
                            fld_okres: okres_val,
                            fld_kat: kat_val,
                            fld_pismeno: pism_val,
                            fld_priezvisko: priezv_val,
                        })
                        soup5 = do_request(session, payload)
                        if soup5 is None:
                            continue
                        hidden = parse_hidden(soup5)
                        time.sleep(DELAY)

                        vlastnik = get_text_field(soup5, fld_vlastnik)
                        if vlastnik:
                            log.info("      %s → %s", priezv_name, vlastnik)
                            save_owner(conn, writer, vlastnik, obec, okres_name, kat_name)
                        else:
                            log.warning("      %s → vlastník nenájdený", priezv_name)

                        mark_done(conn, okres_name, kat_name, pism_name, priezv_name)

                time.sleep(2.0)

    except KeyboardInterrupt:
        log.info("Skript prerušený (Ctrl+C). Progress je uložený, môžete pokračovať.")
    finally:
        conn.close()
        csv_fh.close()
        log.info("Hotovo. Výsledky: %s | %s", DB_PATH, CSV_PATH)


if __name__ == "__main__":
    run_scraper()
