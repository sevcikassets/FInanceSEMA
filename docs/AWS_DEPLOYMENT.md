# AWS Deployment

Produkce je navrzena pro stejne prostredi jako `Activities` a `vocab-app`: samostatna aplikace a databaze v Docker Compose, vystaveni pres sdilenou sit `proxy` a Traefik.

## Predpoklady na serveru

- Docker + Docker Compose plugin.
- Sdilena externi sit `proxy`, na ktere uz bezi Traefik s `letsencrypt` certresolverem (stejna sit jako pro `Activities`/`vocab-app`). Pokud jeste neexistuje:

```bash
docker network create proxy
```

- DNS zaznam `DOMAIN` (viz nize) uz miri na tento server.

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
FINANCE_APP_TOKEN_SECRET=<nahodny-token, napr. `openssl rand -hex 32`>
```

`docker-compose.prod.yml` odmitne nastartovat, pokud nektera z techto promennych (vcetne `DOMAIN`) chybi - `API_CORS_ORIGINS` a `NEXT_PUBLIC_API_URL` se pro produkci dopocitavaji z `DOMAIN` automaticky, nemusi se nastavovat rucne.

Spusteni:

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

nebo pomoci `./deploy-production.sh`.

## Po prvnim nasazeni - zabezpeceni uctu

Aplikace obsahuje citliva financni data, proto hned po prvnim prihlaseni:

1. Prihlaste se jako `FINANCE_APP_USERNAME`/`FINANCE_APP_PASSWORD` z `.env`.
2. Na zalozce **Uzivatele** zapnete dvoufazove overeni (2FA) - naskenujte QR kod aplikaci typu Google Authenticator/Authy/1Password a potvrdte kodem.
3. Pokud ma pristup vice lidi, zalozte jim samostatne ucty (misto sdileni admin uctu) a nastavte jim jen agendy, ktere skutecne potrebuji.
4. Ztrata zarizeni s autenticatorem se resi tlacitkem "Resetovat 2FA" v administraci (uzivatel si pak 2FA znovu nastavi) - neni tu zadna emailova/SMS zaloha.

## Import dat

Nahrajte zdrojove soubory do `~/finance-sema-app/imports/`:

```bash
mkdir -p imports
docker compose -f docker-compose.prod.yml exec api python -m app.excel_import --finance '/imports/Finance SEMA.xlsm' --property-costs '/imports/Finance RD Kvasice.xlsx'
```

## Zalohovani databaze

Databaze zustava v Docker volume `finance-sema-db`. Doporucujeme pravidelny dump mimo server, napr. cron job:

```bash
docker compose -f docker-compose.prod.yml exec -T db pg_dump -U finance finance_sema | gzip > backup-$(date +%F).sql.gz
```

## Aktualizace

```bash
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

nebo pomoci `./update-finance-sema-app.sh`. Databaze pri aktualizaci zustava zachovana (volume `finance-sema-db`); nove sloupce v databazi (napr. pro 2FA) se pri startu API doplni automaticky.
