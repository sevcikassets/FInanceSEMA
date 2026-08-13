# Excel Migration

## Zdrojove soubory

- `C:\TEMP\Finance SEMA.xlsm`
- `C:\TEMP\Finance RD Kvasice.xlsx`
- VBA zdroje: `C:\GIT\FinanceSEMA-xlsx`

## Zmapovane listy

`Finance SEMA.xlsm` obsahuje 16 listu. K migraci jsou relevantni:

- `Půjčky Pohyby` - 131 radku, pohyby pujcek a mezisouctove radky,
- `Investice` - majetek, vlastnik, uverove parametry, rocni urokovy plan 2026-2055,
- `Byt Ghegova 3` a podobne listy - naklady k jednotlivym nemovitostem,
- `Akcie` - 553 radku, transakce, dividendy, watchlist a vypocty v CZK,
- `Sledované akcie` - watchlist a limity,
- `Akcie statistika` - denni casova rada portfolia,
- `Graf` - zdrojova rada pro graf portfolia,
- `Portfolio` - aktualni portfolio a vyhodnoceni zisku/ztraty,
- `Kurzy` - kurzy CNB,
- `Akcie popis` - ticker, nazev, ISIN a popis cinnosti.

`Finance RD Kvasice.xlsx` obsahuje:

- `Finance` - nakladova evidence s poli datum, platce, dodavatel, polozka, kategorie, castka, poznamka,
- `Rekapitulace kategorie` - kontingencni rekapitulace podle kategorii a platcu.

## Mapovani

| Excel | Databaze | Poznamka |
|---|---|---|
| `Půjčky Pohyby` | `loan_movements` | radky s mesicnim textem zustavaji jako `period_label` |
| `Investice` | `assets` | rocni sloupce 2026-2055 jsou `annual_interest_plan` |
| bytove nakladove listy | `asset_costs` | vazba na majetek podle nazvu listu a nazvu majetku |
| `Finance RD Kvasice.xlsx/Finance` | `asset_costs` | vazba na asset `RD-KVASICE` |
| `Akcie` | `stock_transactions` | importuje hodnoty vcetne vypoctenych castku z Excel cache |
| `Kurzy` | `exchange_rates` | kurz k CZK podle data a meny |
| `Akcie popis` | `ticker_descriptions` | popisy firem |
| `Portfolio` | `portfolio_positions` | prvni verze prevezme Excel vystup |
| `Akcie statistika` | `daily_statistics` | prvni verze prevezme Excel vystup |

## Logika z VBA, ktera se ma prenest

- `ImportAkcie.bas` - import brokerskych obchodu, detekce tickeru podle ISIN, detekce duplicit.
- `KurzyCNB.bas` - stazeni a doplneni kurzu CNB.
- `AktualizujHodnotu.bas` - aktualni ceny tickeru, popisy firem, alerty watchlistu.
- `AkcieStatistika.bas` - denni statistika portfolia, portfolio sheet, graf a dividendy.
- `TickerHistory.bas` - historicka analyza tickeru.

## Kontrolni soucty z prvni inventury

- `Půjčky Pohyby`: 527 neprázdných buněk, 29 vzorců.
- `Investice`: 126 neprázdných buněk, 44 vzorců.
- `Akcie`: 3896 neprázdných buněk, 824 vzorců.
- `Portfolio`: 2677 neprázdných buněk, 340 vzorců.
- `Akcie statistika`: 1080 neprázdných buněk, 56 vzorců.
- `Kurzy`: 1276 neprázdných buněk, 318 vzorců.

## Spusteni importu

```powershell
New-Item -ItemType Directory -Force -Path C:\GIT\FinanceSEMA-app\imports
Copy-Item 'C:\TEMP\Finance SEMA.xlsm' 'C:\GIT\FinanceSEMA-app\imports\Finance SEMA.xlsm'
Copy-Item 'C:\TEMP\Finance RD Kvasice.xlsx' 'C:\GIT\FinanceSEMA-app\imports\Finance RD Kvasice.xlsx'
docker compose up -d --build
docker compose exec api python -m app.excel_import --finance '/imports/Finance SEMA.xlsm' --property-costs '/imports/Finance RD Kvasice.xlsx'
```

## Kurzy po migraci

Import z listu `Kurzy` zaklada historicke kurzy do tabulky `exchange_rates`.
Aplikace nad nimi poskytuje:

- denni kurzovni listek podle posledniho data,
- historii vsech importovanych kurzu po dnech ve sloupcich EUR a USD,
- rucni doplneni/opraveni kurzu,
- stazeni denniho kurzovniho listku CNB pro zvolene datum.

## Majetkove agendy

Kazdy majetek ma v aplikaci vlastni agendu. Pod majetkem se zobrazuji jeho
naklady a souhrny kategorii. `RD Kvasice` prevezme radky z workbooku
`Finance RD Kvasice.xlsx`; `Byt Brno Ghegova 3` prevezme radky z listu
`Byt Ghegova 3` v hlavnim workbooku.
