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
# playwright-stealth má rôzne API podľa verzie
def _apply_stealth(page):
    """Aplikuje stealth patches na page; toleruje rôzne verzie knižnice."""
    try:
        # v2+: trieda Stealth
        from playwright_stealth import Stealth
        Stealth().apply_stealth_sync(page)
        return
    except Exception:
        pass
    try:
        # v1.x: stealth_sync funkcia
        from playwright_stealth import stealth_sync
        stealth_sync(page)
        return
    except Exception:
        pass
    try:
        # v2+ alternatíva
        from playwright_stealth.sync import stealth_sync
        stealth_sync(page)
        return
    except Exception:
        pass
    log.warning("playwright-stealth nedostupný — pokračujem bez stealth")

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


def select_and_wait(page, selector, value, expect_child_selector=None):
    """
    Vyberie hodnotu v selecte. Podporuje:
    - AJAX postback (čakáme na networkidle)
    - Full page reload (čakáme na load)
    - Čakanie kým child dropdown sa naplní
    """
    page.select_option(selector, value)

    # Pokus o explicitné dispatchnutie change eventu
    try:
        page.dispatch_event(selector, "change")
    except Exception:
        pass

    # Čakaj buď na navigation alebo na networkidle
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
    except PlaywrightTimeout:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except PlaywrightTimeout:
        pass

    time.sleep(DELAY)

    # Ak vieme aký child selector sa má naplniť, počkajme na to
    if expect_child_selector:
        wait_for_options(page, expect_child_selector, timeout=10000)


def wait_for_options(page, selector, timeout=10000):
    """Počká kým daný select dropdown má aspoň jednu reálnu (non-empty) možnosť."""
    if not selector:
        return False
    try:
        page.wait_for_function(
            """([sel]) => {
                const el = document.querySelector(sel);
                if (!el) return false;
                return Array.from(el.options).filter(o => o.value.trim() !== '').length > 0;
            }""",
            arg=[selector],
            timeout=timeout,
        )
        return True
    except PlaywrightTimeout:
        log.warning("Timeout pri čakaní na možnosti v %s", selector)
        return False


def debug_dropdown(page, selector, label):
    """Vypíše obsah dropdownu pre diagnostiku."""
    if not selector:
        log.info("DEBUG %s: selector=None", label)
        return
    try:
        info = page.eval_on_selector(
            selector,
            """sel => ({
                exists: !!sel,
                disabled: sel.disabled,
                optionCount: sel.options.length,
                nonEmpty: Array.from(sel.options).filter(o => o.value.trim() !== '').length,
                first3: Array.from(sel.options).slice(0, 3).map(o => o.text.trim())
            })"""
        )
        log.info("DEBUG %s [%s]: %s", label, selector, info)
    except Exception as e:
        log.info("DEBUG %s [%s]: nedá sa prečítať (%s)", label, selector, e)


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
        # Persistent kontext + skutočný Chrome (channel="chrome") = obchádza WAF
        user_data = str(Path.home() / ".cica_scraper_profile")
        Path(user_data).mkdir(exist_ok=True)

        try:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=user_data,
                channel="chrome",  # skutočný nainštalovaný Chrome
                headless=False,
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="sk-SK",
                timezone_id="Europe/Bratislava",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
                ignore_default_args=["--enable-automation"],
            )
        except Exception as e:
            log.warning("Chrome channel nedostupný (%s), používam Chromium", e)
            context = pw.chromium.launch_persistent_context(
                user_data_dir=user_data,
                headless=False,
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="sk-SK",
                timezone_id="Europe/Bratislava",
                args=["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
            )

        page = context.pages[0] if context.pages else context.new_page()
        _apply_stealth(page)
        page.set_default_timeout(20000)
        browser = context.browser  # pre kompatibilitu s neskorším browser.close()

        log.info("Otváram stránku: %s", BASE_URL)
        page.goto(BASE_URL)
        page.wait_for_load_state("networkidle")
        time.sleep(3)  # extra čas pre JS

        def has_form_ready():
            """True ak stránka má aspoň jeden select s možnosťami (formulár je zobrazený)."""
            try:
                count = page.eval_on_selector_all(
                    "select",
                    "sels => sels.filter(s => s.options.length > 1).length"
                )
                return count > 0
            except Exception:
                return False

        # Ak nie je vidieť formulár → manuálny režim
        if not has_form_ready():
            log.warning("=" * 70)
            log.warning("FORMULÁR NIE JE VIDITEĽNÝ (WAF blok alebo iná stránka).")
            log.warning("RUČNE v okne prehliadača:")
            log.warning("  1. Ak vidíš WAF blok, klikni 'Go Back' alebo refresh (Cmd+R)")
            log.warning("  2. Klikni na možnosť 'vlastník' (nie 'správca')")
            log.warning("  3. Počkaj kým sa zobrazia dropdowny okres, kat. územie, atď.")
            log.warning("  4. Potom v termináli stlač Enter")
            log.warning("=" * 70)
            while True:
                input(">>> Stlač Enter keď vidíš formulár s dropdownmi... ")
                time.sleep(1)
                if has_form_ready():
                    log.info("Formulár nájdený, pokračujem.")
                    break
                log.warning("Stále nevidím dropdowny. Skús to ešte raz.")

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

        # PEVNÉ ID dropdownov objavené z DOM-u stránky
        sel_okres = "select#DropDownList_okres"
        sel_kat = "select#DropDownList_ku"
        sel_pismeno = "select#DropDownList_ABC"
        sel_priezvisko = "select#DropDownList_VL_PRI"
        sel_vlastnik = "select#DropDownList_VL"
        sel_obec = "select#DropDownList_obec"

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
            context.close()
            conn.close()
            csv_fh.close()
            return

        # --- Krok 3: Iterácia ---
        okresy = get_select_options(page, sel_okres)
        log.info("Nájdených %d okresov", len(okresy))

        try:
            # ===== OKRES =====
            log.info(">>> Začínam od prvého okresu: %s (%d celkom)",
                     okresy[0][1] if okresy else "?", len(okresy))
            for i_okres, (okres_val, okres_name) in enumerate(okresy, 1):
                log.info("=== OKRES [%d/%d]: %s ===", i_okres, len(okresy), okres_name)
                select_and_wait(page, sel_okres, okres_val,
                                expect_child_selector=sel_kat)
                debug_dropdown(page, sel_kat, "kat_uzemie po výbere okresu")
                katy = get_select_options(page, sel_kat)
                log.info("  Nájdených %d katastrálnych území", len(katy))
                if not katy:
                    continue

                # ===== KAT. ÚZEMIE =====
                if not katy:
                    log.warning("  Žiadne kat. územia, preskakujem okres")
                    continue
                log.info("  >>> Začínam od prvého kat. územia: %s (%d celkom)",
                         katy[0][1], len(katy))
                for i_kat, (kat_val, kat_name) in enumerate(katy, 1):
                    log.info("  KAT.ÚZEMIE [%d/%d]: %s", i_kat, len(katy), kat_name)
                    select_and_wait(page, sel_kat, kat_val,
                                    expect_child_selector=sel_pismeno)

                    # Obec je dropdown s 1 možnosťou (auto-naplnená)
                    obec_opts = get_select_options(page, sel_obec)
                    obec = obec_opts[0][1] if obec_opts else ""

                    pismena = get_select_options(page, sel_pismeno)
                    log.info("    Nájdených %d písmen", len(pismena))
                    if not pismena:
                        continue

                    # ===== PRVÉ PÍSMENO =====
                    if not pismena:
                        log.warning("    Žiadne písmená")
                        continue
                    log.info("    >>> Začínam od prvého písmena: %s (%d celkom)",
                             pismena[0][1], len(pismena))
                    for i_pism, (pism_val, pism_name) in enumerate(pismena, 1):
                        log.info("    PÍSMENO [%d/%d]: %s", i_pism, len(pismena), pism_name)
                        select_and_wait(page, sel_pismeno, pism_val,
                                        expect_child_selector=sel_priezvisko)

                        priezviska_opts = get_select_options(page, sel_priezvisko)
                        log.info("      Nájdených %d priezvísk pre písmeno %s",
                                 len(priezviska_opts), pism_name)
                        if not priezviska_opts:
                            continue

                        # ===== PRIEZVISKO =====
                        if not priezviska_opts:
                            continue
                        log.info("      >>> Začínam od prvého priezviska: %s (%d celkom)",
                                 priezviska_opts[0][1], len(priezviska_opts))
                        for i_pr, (priezv_val, priezv_name) in enumerate(priezviska_opts, 1):
                            if is_done(conn, okres_name, kat_name, pism_name, priezv_name):
                                continue

                            log.info("      PRIEZVISKO [%d/%d]: %s",
                                     i_pr, len(priezviska_opts), priezv_name)
                            select_and_wait(page, sel_priezvisko, priezv_val,
                                            expect_child_selector=sel_vlastnik)

                            # ===== VLASTNÍK (dropdown so všetkými ľuďmi) =====
                            vlastnici = get_select_options(page, sel_vlastnik)

                            log.info("        → %d vlastníkov", len(vlastnici))

                            for _, vlastnik_name in vlastnici:
                                if vlastnik_name:
                                    log.info("        ULOŽ: %s", vlastnik_name)
                                    save_owner(conn, writer, vlastnik_name,
                                               obec, okres_name, kat_name)

                            mark_done(conn, okres_name, kat_name, pism_name, priezv_name)
                            time.sleep(0.3)

                    time.sleep(1.0)

        except KeyboardInterrupt:
            log.info("Prerušené (Ctrl+C). Progress uložený.")

        context.close()

    conn.close()
    csv_fh.close()
    log.info("Hotovo. Výsledky: %s | %s", DB_PATH, CSV_PATH)


if __name__ == "__main__":
    run_scraper()
