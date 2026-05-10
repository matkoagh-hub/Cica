"""
Scraper vlastníkov z CICA portálu ÚGKK SR
https://cica.vugk.sk/VL_vyber.aspx

Iteruje: vlastník → Okres → Katastrálne územie → Prvé písmeno → Priezvisko
Ukladá:  vlastník (celé meno), obec, okres, kat_uzemie → SQLite + CSV

Inštalácia (Windows / macOS / Linux):
    pip install playwright playwright-stealth
    python -m playwright install chromium
    (odporúčané: mať nainštalovaný Google Chrome pre channel="chrome")
"""

import csv
import logging
import sqlite3
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout


# ---------------------------------------------------------------------------
# Konfigurácia
# ---------------------------------------------------------------------------

BASE_URL        = "https://cica.vugk.sk/VL_vyber.aspx"
DB_PATH         = "owners.db"
CSV_PATH        = "owners.csv"
LOG_PATH        = "scraper.log"
DELAY           = 1.2    # sekundy čakania po postbacku
STUCK_THRESHOLD = 4      # rovnaký výsledok N-krát za sebou = WAF blok

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
# playwright-stealth (toleruje viaceré verzie)
# ---------------------------------------------------------------------------

def _apply_stealth(page):
    for attempt in [
        lambda: __import__("playwright_stealth", fromlist=["Stealth"]).Stealth().apply_stealth_sync(page),
        lambda: __import__("playwright_stealth", fromlist=["stealth_sync"]).stealth_sync(page),
        lambda: __import__("playwright_stealth.sync", fromlist=["stealth_sync"]).stealth_sync(page),
    ]:
        try:
            attempt()
            return
        except Exception:
            pass
    log.warning("playwright-stealth nedostupný — pokračujem bez stealth")


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
        "SELECT done FROM progress"
        " WHERE okres=? AND kat_uzemie=? AND pismeno=? AND priezvisko=?",
        (okres, kat, pismeno, priezvisko),
    ).fetchone()
    return bool(row and row[0])


def mark_done(conn, okres, kat, pismeno, priezvisko):
    conn.execute(
        "INSERT OR REPLACE INTO progress"
        " (okres, kat_uzemie, pismeno, priezvisko, done) VALUES (?,?,?,?,1)",
        (okres, kat, pismeno, priezvisko),
    )
    conn.commit()


def unmark_done(conn, okres, kat, pismeno, priezvisko_list):
    """Zmaže progress záznamy — použité pri WAF obnove."""
    for priezv in priezvisko_list:
        conn.execute(
            "DELETE FROM progress"
            " WHERE okres=? AND kat_uzemie=? AND pismeno=? AND priezvisko=?",
            (okres, kat, pismeno, priezv),
        )
    conn.commit()
    log.info("Progress vymazaný pre %d priezvísk (WAF obnova)", len(priezvisko_list))


def save_owner(conn, writer, vlastnik, obec, okres, kat):
    try:
        conn.execute(
            "INSERT OR IGNORE INTO owners (vlastnik, obec, okres, kat_uzemie)"
            " VALUES (?,?,?,?)",
            (vlastnik, obec, okres, kat),
        )
        conn.commit()
        writer.writerow({"vlastnik": vlastnik, "obec": obec,
                         "okres": okres, "kat_uzemie": kat})
    except sqlite3.Error as e:
        log.error("DB chyba: %s", e)


# ---------------------------------------------------------------------------
# Playwright pomocné funkcie
# ---------------------------------------------------------------------------

def get_select_options(page, selector):
    """Vráti [(value, text), ...] pre všetky neprázdne options."""
    try:
        return page.eval_on_selector(
            selector,
            "el => Array.from(el.options)"
            "       .filter(o => o.value.trim() !== '')"
            "       .map(o => [o.value, o.text.trim()])"
        )
    except Exception:
        return []


def get_selected_value(page, selector):
    try:
        return page.eval_on_selector(selector, "el => el.value")
    except Exception:
        return None


def get_element_id(page, selector):
    try:
        return page.eval_on_selector(selector, "el => el.id || el.name || ''")
    except Exception:
        return ""


def select_and_wait(page, selector, value):
    """
    Vyberie hodnotu v ASP.NET dropdowne a čaká na full-page postback.

    ASP.NET dropdowny s AutoPostBack=True majú:
        onchange="javascript:setTimeout('__doPostBack(id,\"\")', 0)"
    Preto select_option() → change event → __doPostBack → form POST → navigácia.

    DÔLEŽITÉ: __doPostBack NIKDY nevoláme manuálne po select_option —
    to by spustilo DRUHÝ postback a zresetovalo formulár do pôvodného stavu.
    Manuálne ho voláme IBA keď je hodnota rovnaká (change event nevznikne).
    """
    cur = get_selected_value(page, selector)

    try:
        if cur != value:
            # Iná hodnota → change event → onchange → __doPostBack → navigácia
            with page.expect_navigation(wait_until="load", timeout=30000):
                page.select_option(selector, value)
        else:
            # Tá istá hodnota → change event nevznikne → manuálny postback
            elem_id = get_element_id(page, selector)
            if elem_id:
                js = (
                    "() => { if (typeof __doPostBack === 'function') {"
                    f" __doPostBack('{elem_id}', ''); }} }}"
                )
                with page.expect_navigation(wait_until="load", timeout=30000):
                    page.evaluate(js)
    except PlaywrightTimeout:
        log.warning("Postback timeout: %s = %r", selector, value)

    # Počkaj kým sa dokončia prípadné AJAX doťahovania
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except PlaywrightTimeout:
        pass

    time.sleep(DELAY)

    new = get_selected_value(page, selector)
    if new != value:
        log.warning("Hodnota sa nenastavila! Chcel: %r, Dostal: %r (predtým: %r)",
                    value, new, cur)


def has_form(page):
    """True keď je na stránke aspoň jeden select s viac ako 1 option."""
    try:
        return page.eval_on_selector_all(
            "select",
            "sels => sels.filter(s => s.options.length > 1).length"
        ) > 0
    except Exception:
        return False


def wait_for_form_manual(page):
    """Čaká na manuálne odblokovanie (WAF blok alebo výber vlastník/správca)."""
    log.warning("=" * 70)
    log.warning("FORMULÁR NIE JE DOSTUPNÝ (WAF blok alebo výber vlastník/správca).")
    log.warning("V okne prehliadača:")
    log.warning("  1. Ak vidíš WAF chybu: klikni [Go Back] alebo F5")
    log.warning("  2. Klikni na 'vlastník' (nie správca)")
    log.warning("  3. Počkaj kým sa zobrazia dropdowny")
    log.warning("  4. Stlač Enter tu v termináli")
    log.warning("=" * 70)
    while True:
        input(">>> Stlač Enter keď je formulár viditeľný... ")
        time.sleep(2)
        if has_form(page):
            log.info("Formulár OK.")
            return
        log.warning("Stále nevidím dropdowny — skúste znovu.")


def restore_cascade(page, sel_okres, sel_kat, sel_pismeno,
                    okres_val, kat_val, pism_val,
                    okres_name, kat_name, pism_name):
    """Po WAF obnove znovu nastaví kaskádu na aktuálnu pozíciu."""
    log.info("Obnovujem kaskádu: %s / %s / %s", okres_name, kat_name, pism_name)
    select_and_wait(page, sel_okres, okres_val)
    select_and_wait(page, sel_kat, kat_val)
    select_and_wait(page, sel_pismeno, pism_val)
    log.info("Kaskáda obnovená.")


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
        user_data = str(Path.home() / ".cica_scraper_profile")
        Path(user_data).mkdir(parents=True, exist_ok=True)

        common = dict(
            user_data_dir=user_data,
            headless=False,
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="sk-SK",
            timezone_id="Europe/Bratislava",
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )

        try:
            context = pw.chromium.launch_persistent_context(channel="chrome", **common)
            log.info("Spustený Google Chrome")
        except Exception as e:
            log.warning("Chrome nedostupný (%s), použijem Chromium", e)
            context = pw.chromium.launch_persistent_context(**common)

        page = context.pages[0] if context.pages else context.new_page()
        _apply_stealth(page)
        page.set_default_timeout(20000)

        log.info("Otváram: %s", BASE_URL)
        page.goto(BASE_URL)
        page.wait_for_load_state("networkidle")
        time.sleep(3)

        if not has_form(page):
            wait_for_form_manual(page)

        page.screenshot(path="debug_screenshot.png", full_page=True)
        log.info("Screenshot: debug_screenshot.png")

        # Pevné ID z DOM-u (overené diagnostikou pri vývoji)
        sel_okres      = "select#DropDownList_okres"
        sel_kat        = "select#DropDownList_ku"
        sel_pismeno    = "select#DropDownList_ABC"
        sel_priezvisko = "select#DropDownList_VL_PRI"
        sel_vlastnik   = "select#DropDownList_VL"
        sel_obec       = "select#DropDownList_obec"

        if page.locator(sel_okres).count() == 0:
            log.error("Dropdown #DropDownList_okres sa nenašiel. Koniec.")
            context.close(); conn.close(); csv_fh.close()
            return

        # =====================================================================
        # ITERÁCIA
        # =====================================================================
        okresy = get_select_options(page, sel_okres)
        log.info("Celkovo %d okresov", len(okresy))

        try:
            for i_okres, (okres_val, okres_name) in enumerate(okresy, 1):
                log.info("=== OKRES [%d/%d]: %s ===", i_okres, len(okresy), okres_name)
                select_and_wait(page, sel_okres, okres_val)

                katy = get_select_options(page, sel_kat)
                log.info("  %d katastrálnych území", len(katy))
                if not katy:
                    continue

                for i_kat, (kat_val, kat_name) in enumerate(katy, 1):
                    log.info("  KAT [%d/%d]: %s", i_kat, len(katy), kat_name)
                    select_and_wait(page, sel_kat, kat_val)

                    obec_opts = get_select_options(page, sel_obec)
                    obec = obec_opts[0][1] if obec_opts else ""

                    pismena = get_select_options(page, sel_pismeno)
                    log.info("    %d písmen", len(pismena))
                    if not pismena:
                        continue

                    for i_pism, (pism_val, pism_name) in enumerate(pismena, 1):
                        log.info("    PÍSMENO [%d/%d]: %s", i_pism, len(pismena), pism_name)
                        select_and_wait(page, sel_pismeno, pism_val)

                        priezviska = get_select_options(page, sel_priezvisko)
                        log.info("      %d priezvísk", len(priezviska))
                        if not priezviska:
                            continue

                        # Sledujeme posledné výsledky pre detekciu WAF bloku
                        recent_results = []   # [(priezv_name, frozenset(vlastnik_names))]

                        for i_pr, (priezv_val, priezv_name) in enumerate(priezviska, 1):
                            if is_done(conn, okres_name, kat_name, pism_name, priezv_name):
                                log.info("      [skip] %s", priezv_name)
                                recent_results.clear()   # reset pri skip (nerelevantné)
                                continue

                            log.info("      PRIEZVISKO [%d/%d]: %s",
                                     i_pr, len(priezviska), priezv_name)
                            select_and_wait(page, sel_priezvisko, priezv_val)

                            vlastnici = get_select_options(page, sel_vlastnik)
                            sig = frozenset(v for _, v in vlastnici if v)

                            # ── WAF detekcia ──────────────────────────────
                            recent_results.append((priezv_name, sig))
                            if len(recent_results) > STUCK_THRESHOLD + 1:
                                recent_results.pop(0)

                            if len(recent_results) >= STUCK_THRESHOLD:
                                last_sigs = [s for _, s in recent_results[-STUCK_THRESHOLD:]]
                                if len(set(last_sigs)) == 1 and last_sigs[0]:
                                    stuck_names = [n for n, _ in recent_results[-STUCK_THRESHOLD:]]
                                    log.warning(
                                        "WAF BLOK DETEKOVANÝ! Rovnaký výsledok %dx: %s",
                                        STUCK_THRESHOLD, list(last_sigs[0])
                                    )
                                    log.warning("Postihnuté priezviská: %s", stuck_names)

                                    # Zmaž nesprávne progress záznamy
                                    unmark_done(conn, okres_name, kat_name,
                                                pism_name, stuck_names)

                                    # Čakaj na manuálne odblokovanie
                                    wait_for_form_manual(page)

                                    # Obnov kaskádu na aktuálnu pozíciu
                                    restore_cascade(
                                        page,
                                        sel_okres, sel_kat, sel_pismeno,
                                        okres_val, kat_val, pism_val,
                                        okres_name, kat_name, pism_name,
                                    )

                                    recent_results.clear()

                                    # Tento priezvisko znovu spracuj
                                    # (continue pustí nasledujúci iteráciu; preskočíme ho neskôr
                                    # keď bude is_done=False a kaskáda je na správnom mieste)
                                    continue
                            # ── Koniec WAF detekcie ───────────────────────

                            log.info("        → %d vlastníkov", len(vlastnici))
                            for _, vlastnik_name in vlastnici:
                                if vlastnik_name:
                                    log.info("        ULOŽ: %s | %s | %s | %s",
                                             vlastnik_name, obec, okres_name, kat_name)
                                    save_owner(conn, writer, vlastnik_name,
                                               obec, okres_name, kat_name)

                            mark_done(conn, okres_name, kat_name, pism_name, priezv_name)

        except KeyboardInterrupt:
            log.info("Prerušené (Ctrl+C). Progress uložený.")

        context.close()

    conn.close()
    csv_fh.close()
    log.info("Hotovo. Výsledky: %s | %s", DB_PATH, CSV_PATH)


if __name__ == "__main__":
    run_scraper()
