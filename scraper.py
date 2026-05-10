"""
Scraper vlastníkov z CICA portálu ÚGKK SR
https://cica.vugk.sk/VL_vyber.aspx

Používa Playwright (reálny prehliadač) pre JavaScript-renderované stránky.
Iteruje: vlastník -> Okres -> Katastrálne územie -> Prvé písmeno -> Priezvisko
Ukladá: vlastník (celé meno), obec, okres, kat_uzemie do SQLite + CSV

Inštalácia:
    pip3 install playwright
    python3 -m playwright install chromium
"""

import csv
import logging
import sqlite3
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import stealth_sync

BASE_URL = "https://cica.vugk.sk/VL_vyber.aspx"
DB_PATH = "owners.db"
CSV_PATH = "owners.csv"
LOG_PATH = "scraper.log"
DELAY = 1.0  # sekundy medzi akciami

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Databáza
# ---------------------------------------------------------------------------

def init_db(conn):
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


def is_done(conn, okres, kat, pismeno, priezvisko):
    row = conn.execute(
        "SELECT done FROM progress WHERE okres=? AND kat_uzemie=? AND pismeno=? AND priezvisko=?",
        (okres, kat, pismeno, priezvisko),
    ).fetchone()
    return bool(row and row[0])


def mark_done(conn, okres, kat, pismeno, priezvisko):
    conn.execute(
        "INSERT OR REPLACE INTO progress (okres, kat_uzemie, pismeno, priezvisko, done) VALUES (?,?,?,?,1)",
        (okres, kat, pismeno, priezvisko),
    )
    conn.commit()


def save_owner(conn, writer, vlastnik, obec, okres, kat):
    try:
        conn.execute(
            "INSERT OR IGNORE INTO owners (vlastnik, obec, okres, kat_uzemie) VALUES (?,?,?,?)",
            (vlastnik, obec, okres, kat),
        )
        conn.commit()
        writer.writerow({"vlastnik": vlastnik, "obec": obec, "okres": okres, "kat_uzemie": kat})
    except sqlite3.Error as e:
        log.error("DB chyba: %s", e)


# ---------------------------------------------------------------------------
# Playwright pomocné funkcie
# ---------------------------------------------------------------------------

def get_select_options(page, selector):
    """Vráti zoznam (value, text) pre všetky options v selecte."""
    try:
        return page.eval_on_selector(
            selector,
            """sel => Array.from(sel.options)
                .filter(o => o.value.trim() !== '')
                .map(o => [o.value, o.text.trim()])"""
        )
    except Exception:
        return []


def select_and_wait(page, selector, value):
    """Vyberie hodnotu v selecte a počká na sieťový kľud (postback)."""
    page.select_option(selector, value)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeout:
        pass
    time.sleep(DELAY)


def get_input_value(page, selector):
    """Bezpečne prečíta hodnotu textového poľa."""
    try:
        return page.input_value(selector).strip()
    except Exception:
        return ""


def find_selector(page, candidates):
    """Vráti prvý selektor zo zoznamu ktorý existuje na stránke."""
    for sel in candidates:
        try:
            if page.locator(sel).count() > 0:
                return sel
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Hlavná logika
# ---------------------------------------------------------------------------

def run_scraper():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    csv_exists = Path(CSV_PATH).exists()
    csv_fh = open(CSV_PATH, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(csv_fh, fieldnames=["vlastnik", "obec", "okres", "kat_uzemie"])
    if not csv_exists:
        writer.writeheader()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="sk-SK",
            timezone_id="Europe/Bratislava",
        )
        page = context.new_page()
        stealth_sync(page)  # maskuje Playwright pred botdetekciou
        page.set_default_timeout(20000)

        log.info("Otváram stránku: %s", BASE_URL)
        page.goto(BASE_URL)
        page.wait_for_load_state("networkidle")
        time.sleep(3)  # extra čas pre JS

        # Screenshot pre diagnostiku
        page.screenshot(path="debug_screenshot.png", full_page=True)
        log.info("Screenshot uložený do debug_screenshot.png")

        # Vypíš celý text stránky pre diagnostiku
        body_text = page.inner_text("body")[:500]
        log.info("Text stránky (prvých 500 znakov): %s", body_text)

        # --- Krok 1: Kliknúť na "vlastník" ---
        log.info("Hľadám výber vlastník / správca...")
        vlastnik_clicked = False

        # Skúsime rôzne spôsoby ako nájsť a kliknúť na "vlastník"
        for locator_expr in [
            "text=vlastník",
            "text=Vlastník",
            "text=VLASTNÍK",
            "input[type=radio][value*='vlastn' i]",
            "input[type=radio][id*='vlastn' i]",
            "a:has-text('vlastník')",
            "a:has-text('Vlastník')",
            "label:has-text('vlastník')",
            "button:has-text('vlastník')",
            "button:has-text('Vlastník')",
        ]:
            try:
                loc = page.locator(locator_expr).first
                if loc.count() > 0 or page.locator(locator_expr).count() > 0:
                    log.info("Klikám na: %s", locator_expr)
                    page.locator(locator_expr).first.click()
                    page.wait_for_load_state("networkidle")
                    time.sleep(DELAY)
                    vlastnik_clicked = True
                    break
            except Exception:
                continue

        if not vlastnik_clicked:
            log.warning("Výber vlastník/správca nenájdený — pokračujem bez kliknutia")

        # --- Krok 2: Nájsť selektory formulára ---
        log.info("Hľadám polia formulára...")

        # Vypíš všetky selecty pre diagnostiku
        all_selects = page.eval_on_selector_all(
            "select",
            "sels => sels.map(s => ({id: s.id, name: s.name, opts: s.options.length}))"
        )
        log.info("Nájdené selecty: %s", all_selects)

        # Nájdi selector pre Okres
        sel_okres = find_selector(page, [
            "select[name*='Okres' i]", "select[id*='Okres' i]",
            "select[name*='ddlOkres']", "select[id*='ddlOkres']",
        ])
        sel_kat = find_selector(page, [
            "select[name*='Katastr' i]", "select[id*='Katastr' i]",
            "select[name*='KatastrUzem']", "select[id*='ddlKatastr']",
            "select[name*='Uzem' i]", "select[id*='Uzem' i]",
        ])
        sel_pismeno = find_selector(page, [
            "select[name*='Pismen' i]", "select[id*='Pismen' i]",
            "select[name*='Prvé' i]", "select[id*='Prve' i]",
            "select[name*='Letter' i]",
        ])
        sel_priezvisko = find_selector(page, [
            "select[name*='Priezv' i]", "select[id*='Priezv' i]",
            "select[name*='Surname' i]",
        ])
        sel_vlastnik = find_selector(page, [
            "input[name*='Vlastnik' i]", "input[id*='Vlastnik' i]",
            "input[name*='txtVlastnik']", "input[id*='txtVlastnik']",
        ])
        sel_obec = find_selector(page, [
            "input[name*='Obec' i]", "input[id*='Obec' i]",
            "input[name*='txtObec']",
        ])

        log.info("Polia: okres=%s kat=%s pismeno=%s priezvisko=%s vlastnik=%s obec=%s",
                 sel_okres, sel_kat, sel_pismeno, sel_priezvisko, sel_vlastnik, sel_obec)

        if not sel_okres:
            # Posledný pokus: vezmi select s najviac options
            if all_selects:
                best = max(all_selects, key=lambda s: s["opts"])
                sid = best.get("id") or best.get("name")
                if sid:
                    sel_okres = f"select[id='{sid}']" if best.get("id") else f"select[name='{sid}']"
                    log.info("Fallback Okres selector: %s", sel_okres)

        if not sel_okres:
            log.error("Nepodarilo sa nájsť dropdown Okres. Skript končí.")
            browser.close()
            conn.close()
            csv_fh.close()
            return

        # --- Krok 3: Iterácia ---
        okresy = get_select_options(page, sel_okres)
        log.info("Nájdených %d okresov", len(okresy))

        try:
            for okres_val, okres_name in okresy:
                log.info("=== OKRES: %s ===", okres_name)
                select_and_wait(page, sel_okres, okres_val)

                # Znovu nájdi kat. územie selector (stránka sa mohla zmeniť)
                kat_sel = sel_kat or find_selector(page, [
                    "select[name*='Katastr' i]", "select[id*='Katastr' i]",
                    "select[name*='Uzem' i]", "select[id*='Uzem' i]",
                ])
                if not kat_sel:
                    log.warning("  Nenašiel som selector pre kat. územie")
                    continue

                katy = get_select_options(page, kat_sel)
                if not katy:
                    log.warning("  Žiadne katastrálne územia")
                    continue

                for kat_val, kat_name in katy:
                    log.info("  KAT: %s", kat_name)
                    select_and_wait(page, sel_okres, okres_val)
                    select_and_wait(page, kat_sel, kat_val)

                    obec = get_input_value(page, sel_obec) if sel_obec else ""

                    pism_sel = sel_pismeno or find_selector(page, [
                        "select[name*='Pismen' i]", "select[id*='Pismen' i]",
                    ])
                    if not pism_sel:
                        log.warning("    Nenašiel som selector pre písmeno")
                        continue

                    pismena = get_select_options(page, pism_sel)
                    if not pismena:
                        log.warning("    Žiadne písmená")
                        continue

                    for pism_val, pism_name in pismena:
                        log.info("    PÍSMENO: %s", pism_name)
                        select_and_wait(page, sel_okres, okres_val)
                        select_and_wait(page, kat_sel, kat_val)
                        select_and_wait(page, pism_sel, pism_val)

                        priezv_sel = sel_priezvisko or find_selector(page, [
                            "select[name*='Priezv' i]", "select[id*='Priezv' i]",
                        ])
                        if not priezv_sel:
                            log.info("      Žiadny selector pre priezvisko")
                            continue

                        priezviска = get_select_options(page, priezv_sel)
                        if not priezviска:
                            log.info("      Žiadne priezviská pre %s", pism_name)
                            continue

                        for priezv_val, priezv_name in priezviска:
                            if is_done(conn, okres_name, kat_name, pism_name, priezv_name):
                                continue

                            select_and_wait(page, sel_okres, okres_val)
                            select_and_wait(page, kat_sel, kat_val)
                            select_and_wait(page, pism_sel, pism_val)
                            select_and_wait(page, priezv_sel, priezv_val)

                            vlastnik_val = ""
                            if sel_vlastnik:
                                vlastnik_val = get_input_value(page, sel_vlastnik)

                            if vlastnik_val:
                                log.info("      %s → %s", priezv_name, vlastnik_val)
                                save_owner(conn, writer, vlastnik_val, obec, okres_name, kat_name)
                            else:
                                log.warning("      %s → vlastník nenájdený", priezv_name)

                            mark_done(conn, okres_name, kat_name, pism_name, priezv_name)

                    time.sleep(2.0)

        except KeyboardInterrupt:
            log.info("Prerušené (Ctrl+C). Progress uložený.")

        browser.close()

    conn.close()
    csv_fh.close()
    log.info("Hotovo. Výsledky: %s | %s", DB_PATH, CSV_PATH)


if __name__ == "__main__":
    run_scraper()
