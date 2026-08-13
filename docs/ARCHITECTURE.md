# Architecture

FinanceSEMA je rozdelena stejne jako aplikace Activities:

- `api/` - FastAPI aplikace, PostgreSQL datovy model, import z Excelu,
- `web/` - Next.js pracovni rozhrani,
- `docker-compose.yml` - lokalni vyvoj s vlastni PostgreSQL databazi,
- `docker-compose.prod.yml` - produkcni beh za sdilenym Traefik proxy.

## Datove oblasti

### Pujcky

Zdroj: list `Půjčky Pohyby`.

Tabulka `loan_movements` uchovava jednotlive pohyby vcetne veritele, dluznika, castky, urokove sazby, periody uroku a planovaneho ukonceni. Mezisutove radky typu `Leden 2023` se importuji jako `period_label`, aby bylo mozne rekonstruovat puvodni Excel pohled.

### Majetek

Zdroj: list `Investice` a nakladove listy v obou workboocich.

Tabulka `assets` uchovava kod, vlastnika, typ, nazev, celkovou hodnotu, vlastni zdroje, pujcenou castku a parametry uveru. Rocni plan uroku z let 2026-2055 se uklada jako JSON `annual_interest_plan`, protoze v Excelu jde o sirokou matici let.

Tabulka `asset_costs` uchovava naklady k majetku. Naklady z `Finance RD Kvasice.xlsx` se pri importu vazou na automaticky zalozeny majetek `RD-KVASICE`. Endpoint `GET /assets/agendas` vraci pro kazdy majetek samostatnou agendu vcetne nakladu, souctu nakladu a rekapitulace kategorii.

### Akcie

Zdroj: listy `Akcie`, `Akcie popis`, `Kurzy`, `Portfolio`, `Akcie statistika`.

Tabulky:

- `stock_transactions` - nakupy, prodeje, dividendy, watchlistove a planovane radky z listu `Akcie`,
- `exchange_rates` - kurzy CNB pro EUR/USD a dalsi meny,
- `ticker_descriptions` - popisy firem,
- `portfolio_positions` - aktualni portfolio prevzate z listu `Portfolio`,
- `daily_statistics` - historicka denni statistika prevzata z listu `Akcie statistika`.

## Vypoctova strategie

Prvni verze importovala i Excelove vystupy (`Portfolio`, `Akcie statistika`), aby bylo mozne rychle overit shodu s puvodnim sesitem. Vypocty jsou nyni v backendu:

1. prepocet cen a poplatku do CZK podle nejblizsiho predchoziho kurzu - `stock_services.refresh_current_prices`,
2. portfolio podle tickeru vcetne nakupu/prodeju/dividend - `stock_services.recalculate_stocks`,
3. denni statistika podle logiky `AkcieStatistika.bas` - `stock_services.recalculate_stocks`,
4. uverove a hypotecni rozvrhy podle parametru z `Investice` - `loan_calc.project_annual_interest`.

Bod 4 stoji za poznamku: v `Finance SEMA.xlsm` byl vzorec pro rocni projekci uroku
(`LET` + `CUMIPMT`) rozpracovany jen v nekolika testovacich bunkach a pro skutecnou
hypotéku nebyl nikdy dopocitany. `loan_calc.py` implementuje stejnou amortizacni
logiku (fixni splatka, CUMIPMT) primo v Pythonu, takze aplikace dokaze projekci
spocitat pro kazdy majetek s uverovymi parametry bez ohledu na to, jestli byla
excelova bunka vyplnena.

Dale jsou doplneny agendy, ktere v puvodni aplikaci chybely uplne:

- **Alerty** (`stock_services.compute_alerts`, endpoint `GET /stocks/alerts`) -
  pragmaticka nahrada alertoveho systemu z `AktualizujHodnotu.bas`/`AkcieStatistika.bas`:
  sledovane tituly pod limitni cenou a portfoliove pozice s poklesem nad zadany
  prah. Denni zmeny ceny nad prah hlasi primo `refresh_current_prices` (pole
  `movers`), protoze jen tam je jeste znama cena pred aktualizaci.
- **Historie tickeru** (`stock_services.build_ticker_history`, endpoint
  `GET /stocks/ticker-history`) - port `TickerHistory.bas`: denni cena z Yahoo
  Finance kombinovana s nakupy z `stock_transactions` (kumulativni pocet kusu,
  hodnota a zisk/ztrata v obchodni mene i CZK).

## API endpointy

Zakladni cteci a pracovni endpointy:

- `GET /summary` - souhrnne KPI a pocty zaznamu,
- `GET /assets/agendas` - samostatne agendy majetku vcetne nakladu,
- `GET /stocks/portfolio` - importovane aktualni portfolio,
- `GET /stocks/transactions` - importovane akciove pohyby,
- `GET /stocks/statistics` - denni statistika portfolia,
- `GET /stocks/overview` - agregace akciovych pohybu a men,
- `GET /rates` - historie kurzovniho listku,
- `GET /rates/latest` - posledni dostupny kurzovni listek,
- `POST /rates` - rucni ulozeni kurzu,
- `POST /rates/fetch-cnb?rate_date=YYYY-MM-DD` - stazeni denniho kurzovniho listku CNB,
- `GET /assets/{id}/interest-projection` - dopoctena rocni projekce uroku uveru,
- `GET /stocks/alerts?threshold_pct=10` - sledovane tituly pod limitem a propady portfolia,
- `GET /stocks/ticker-history?ticker=&date_from=&date_to=` - historie ceny tickeru s nakupy.

## Bezpecnost

API pouziva jednoduche prihlaseni pres promenne `APP_USERNAME` a `APP_PASSWORD` uvnitr kontejneru; Compose je plni z projektovych promennych `FINANCE_APP_USERNAME`, `FINANCE_APP_PASSWORD` a `FINANCE_APP_TOKEN_SECRET`. Po prihlaseni vraci JWT token platny 12 hodin.
