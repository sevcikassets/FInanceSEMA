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

Nahrajte zdrojove soubory z lokalniho pocitace do `~/finance-sema-app/imports/` na serveru, napr. z Windows pres `scp` (PowerShell):

```powershell
scp "C:\TEMP\Finance SEMA.xlsm" ubuntu@<server>:~/finance-sema-app/imports/
scp "C:\TEMP\Finance RD Kvasice.xlsx" ubuntu@<server>:~/finance-sema-app/imports/
```

Pak na serveru:

```bash
cd ~/finance-sema-app
mkdir -p imports
docker compose -f docker-compose.prod.yml exec api python -m app.excel_import --finance '/imports/Finance SEMA.xlsm' --property-costs '/imports/Finance RD Kvasice.xlsx'
```

Import je bezpecne spustit i opakovane - pred naimportovanim si nejdriv smaze puvodni obsah importovanych tabulek (denni statistika, portfolio, kurzy, akciove transakce, watchlist, majetky/naklady, pohyby pujcek), takze nehrozi zdvojeni dat. Uzivatelske ucty (vcetne 2FA nastaveni) import nijak neovlivni.

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

## Reseni problemu

### 504 Gateway Timeout na `/api/*`, i kdyz kontejner `api` bezi a labely v `docker-compose.prod.yml` jsou spravne

Tenhle projekt bezi na stejnem sdilenem Traefiku jako `Activities` a `vocab-app` (spravovanem mimo tento repozitar, typicky v `/opt/proxy/docker-compose.yml` na hostu). Pokud po restartu/rebootu hosta zacne `web` router fungovat, ale `/api/*` konzistentne pada na 504, i kdyz:

- kontejner `finance-sema-app-api-1` bezi a `docker logs` ukazuje cisty start (`Uvicorn running on ...`),
- primy test mezi kontejnery funguje (`docker exec finance-sema-app-web-1 wget -qO- http://finance-sema-app-api-1:8000/health`),
- labely `traefik.http.services.finance-sema-api.loadbalancer.server.port` a `traefik.docker.network=proxy` jsou v `docker-compose.prod.yml` spravne,

zkontrolujte na hostu (ne v tomto repozitari) soubor spravujici sdileny Traefik, jestli nema vynucenou starou verzi Docker API:

```bash
docker inspect traefik --format '{{json .Config.Cmd}}'
cat /opt/proxy/docker-compose.yml   # cesta zjistitelna pres: docker inspect traefik --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'
```

Konkretne `environment: DOCKER_API_VERSION: "1.40"` u sluzby `traefik` zpusobuje, ze si Traefik u kontejneru pripojenych na vice siti (zde `default` + `proxy`) nespravne vybere IP z prvni site v abecednim poradi (`...default`) misto site urcene labelem `traefik.docker.network=proxy` - a protoze samotny Traefik na te druhe siti neni, kazdy pozadavek na dany router skonci timeoutem (504) po defaultnich ~30 s, presto ze cilovy kontejner je zdravy a dostupny odjinud.

Diagnostika (docasne zapnout podrobny log u sdileneho Traefiku, pridat do jeho `command:` a restartovat jen jeho):

```yaml
- "--accesslog=true"
- "--log.level=DEBUG"
```

V logu (`docker logs --since 1m traefik | grep -i "Creating server URL"`) je videt presnou IP:port, kterou si Traefik pro danou sluzbu vybral - pokud neodpovida IP na siti `proxy` (`docker inspect <api-kontejner> --format '{{json .NetworkSettings.Networks}}'`), je to tento problem.

Reseni: odstranit radek `environment: DOCKER_API_VERSION: "1.40"` ze sluzby `traefik` v `/opt/proxy/docker-compose.yml` a restartovat (`docker compose up -d` v `/opt/proxy`). Po overeni, ze vse funguje (vcetne ostatnich aplikaci na stejnem Traefiku), snizit `--log.level` zpet (napr. odebrat, defaultni uroven staci) - `--accesslog=true` je uzitecne nechat zapnute trvale.
