# AWS Deployment

Produkce je navrzena pro stejne prostredi jako `Activities` a `vocab-app`: samostatna aplikace a databaze v Docker Compose, vystaveni pres sdilenou sit `proxy` a Traefik.

## Prvni nasazeni

Na serveru:

```bash
git clone <repo-url> ~/finance-sema-app
cd ~/finance-sema-app
cp .env.example .env
```

V `.env` nastavte minimalne:

```env
DOMAIN=finance.sevcikassets.cz
POSTGRES_DB=finance_sema
POSTGRES_USER=finance
POSTGRES_PASSWORD=<silne-heslo>
FINANCE_APP_USERNAME=<uzivatel>
FINANCE_APP_PASSWORD=<silne-heslo>
FINANCE_APP_TOKEN_SECRET=<nahodny-token>
```

Spusteni:

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

## Import dat

Nahrajte zdrojove soubory do `~/finance-sema-app/imports/`:

```bash
mkdir -p imports
docker compose -f docker-compose.prod.yml exec api python -m app.excel_import --finance '/imports/Finance SEMA.xlsm' --property-costs '/imports/Finance RD Kvasice.xlsx'
```

## Aktualizace

```bash
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

Databaze zustava v Docker volume `finance-sema-db`.
