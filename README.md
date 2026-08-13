# FinanceSEMA

Samostatna aplikace pro evidenci osobnich a firemnich financnich agend, ktera nahrazuje Excel `Finance SEMA.xlsm`.

## Oblasti

- pujcky a pohyby mezi veriteli a dluzniky,
- majetek, vlastnici, naklady a hypotecni/urokove prehledy,
- akcie, nakupy, prodeje, dividendy, watchlist a kurzy,
- denni statistika portfolia vcetne zisku, ztraty a dividend,
- import historickych dat z Excelu.

Aktualni stav aplikace umi zobrazit importovane portfolio, akciove transakce,
denni statistiku vcetne varovani, kurzovni listek po dnech ve sloupcich EUR/USD,
pujcky, majetek a samostatne agendy nakladu pod jednotlivymi majetky. Kurzy lze
rucne doplnit nebo nacist pres API z denniho kurzovniho listku CNB.

## Rychly start

```powershell
docker compose up --build
```

Aplikace:

- frontend: http://localhost:3010
- API: http://localhost:8010
- PostgreSQL: localhost:5440

Vychozi lokalni prihlaseni je `admin` / `finance`. Pro produkci nastavte `.env` podle `.env.example`.

## Import z Excelu

Zdrojove soubory:

- `C:\TEMP\Finance SEMA.xlsm`
- `C:\TEMP\Finance RD Kvasice.xlsx`

Po startu databaze a API:

```powershell
docker compose exec api python -m app.excel_import --finance "/imports/Finance SEMA.xlsm" --property-costs "/imports/Finance RD Kvasice.xlsx"
```

Pro lokalni import je potreba soubory zkopirovat nebo namountovat do kontejneru. Vychozi compose mapuje slozku `./imports` na `/imports`.

## Dopocty, ktere uz nejsou jen z Excelu

- `GET /assets/{id}/interest-projection` a pole `computed_interest_plan` v `/assets`
  a `/assets/agendas` - rocni projekce uroku uveru 2026-2055 dopoctena primo v aplikaci
  (nahrazuje rozpracovany a nikdy nedokonceny CUMIPMT vzorec z listu `Investice`).
- `GET /stocks/alerts` - sledovane tituly pod limitni cenou a portfoliove pozice
  s poklesem nad zadany prah (vychozi 10 %).
- `POST /stocks/refresh-prices` vraci navic `movers` - tituly s vyraznou dennni
  zmenou ceny oproti hodnote pred aktualizaci.
- `GET /stocks/ticker-history` - historie ceny tickeru (Yahoo Finance) kombinovana
  s nakupy z `stock_transactions`, obdoba `TickerHistory.bas`.

## Testy

```powershell
docker compose exec api pip install -r requirements-dev.txt
docker compose exec api pytest
```

Testy pracujici s databazi (`api/tests/test_api_smoke.py`) se automaticky preskoci,
pokud neni dostupne `DATABASE_URL`/`TEST_DATABASE_URL` - cistě vypoctove testy
(`test_loan_calc.py`, `test_stock_services_pure.py`) bezi vzdy.

## Dokumentace

- `docs/ARCHITECTURE.md` - architektura a datovy model
- `docs/EXCEL_MIGRATION.md` - mapovani Excelu a migracni postup
- `docs/AWS_DEPLOYMENT.md` - nasazeni vedle ostatnich aplikaci pres sdileny Traefik
