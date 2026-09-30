# Smithproxy Runner API v1

Runner standardně poslouchá pouze na `127.0.0.1:9080`. Všechny `/v1` endpointy
vyžadují `Authorization: Bearer <CZ_RUNNER_TOKEN>`. `/healthz` je lokální
neautentizovaná kontrola procesu.

## Stav a build

```http
GET /v1/status
GET /v1/build
POST /v1/build
POST /v1/refs/refresh
Content-Type: application/json

{"ref":"master"}
```

Build je asynchronní. Stav je `idle`, `running`, `complete` nebo `failed`.
Kompilace běží v samostatném nízkoprioritním subprocessu; HTTP runner během ní
zůstává dostupný. `GET /v1/status` a `GET /v1/build` obsahují také `refs` s
prefetchnutými remote branchemi, jejich aktuálními SHA a příznakem
`update_available`. Runner je obnovuje na pozadí každých pět minut; explicitní
`POST /v1/refs/refresh` pouze naplánuje okamžitý fetch a nečeká na něj.
Odpověď obsahuje omezený log a informaci, zda existuje spustitelná binárka.
Build log se uchovává bounded a admin konzole jej automaticky zobrazí při
selhání. Archiv buildu obsahuje výchozí config a assets, ale raw výchozí config
se automaticky nevkládá do uživatelské config knihovny.
Pole `build_type` přijímá `Release` nebo `Debug`.

## Knihovna konfigurací

```http
GET    /v1/configs
GET    /v1/configs/{id}
DELETE /v1/configs/{id}
POST   /v1/configs/preview
POST   /v1/configs/commit
DELETE /v1/configs/previews/{preview_id}
```

Config knihovna přijímá pouze nativní výstup Smithproxy `save config`. Upload či
editace nejprve volá `preview`; runner použije nejnovější archivovaný master
build, vrátí unified diff vstupu proti nativnímu výstupu a krátkodobé
`preview_id`. Teprve `commit` s `approved: true` config atomicky vytvoří nebo
aktualizuje. Přímé `POST /v1/configs` a `PUT /v1/configs/{id}` jsou zakázané.
Starší položky zůstávají viditelné jako `legacy / unverified`, ale nelze je
použít pro nový profil ani spawn. Katalog nemá databázi; metadata a pending
preview jsou JSON a config bundle je před použitím ověřen SHA-256.

## CA / Cert bundle knihovna

```http
GET    /v1/cert-bundles
POST   /v1/cert-bundles
GET    /v1/cert-bundles/{id}
DELETE /v1/cert-bundles/{id}
POST   /v1/cert-bundles/{id}/certificates
GET    /v1/cert-bundles/{id}/ca.pem
```

Vytvoření bundle generuje skutečný RSA CA pár přes OpenSSL. Další certifikát
lze vygenerovat a podepsat touto CA nebo importovat jako veřejný PEM. Privátní
klíče jsou v root-only souborovém úložišti v režimu `0600`; API nikdy nevrací
jejich obsah. Bundle se váže k runtime profilu přes `cert_bundle_id`.

## Source IP pool

```http
GET /v1/sources
```

Vrací administrátorem nakonfigurované adresy a jejich dostupnost. Jedna source
IP může mít nejvýše jednu aktivní instanci.

## Instance

```http
GET /v1/instances
GET /v1/instances/{id}
POST /v1/instances
DELETE /v1/instances/{id}
POST /v1/instances/{id}/restart
```

```json
{
  "source_ip": "198.51.100.10",
  "build_id": "active",
  "config_id": "active",
  "runtime_seconds": 3600,
  "parameters": {
    "socks_port": 1080,
    "plaintext_port": 10080,
    "tls_port": 10443,
    "cli_port": 10000,
    "workers": 1,
    "pcap_quota_mb": 100
  }
}
```

Vytvoření je úspěšné až po vytvoření namespace, veth, routing pravidel a
transientní systemd jednotky. Instance se automaticky ukončí po runtime limitu.
Efektivní config se uloží vedle stavového JSON a přežije ukončení instance:

```http
GET  /v1/instances/{id}/config
POST /v1/instances/{id}/config/preview
```

První endpoint umožní download živé i zastavené nesmazané instance. Druhý na
živé RW instanci pošle Smithproxy CLI příkaz `save config` a vrátí stejný
schvalovací preview/diff jako GUI upload; bez následného explicitního `commit`
se knihovna nezmění. Smazání instance odstraní i snapshot.
Restart zachová stejnou systemd unit, namespace, veth, routing a runtime
adresář; restartuje pouze Smithproxy a obnoví TTL.

## Diagnostika

```http
GET  /v1/instances/{id}/logs?lines=300
GET  /v1/instances/{id}/diagnostics
POST /v1/instances/{id}/debug
DELETE /v1/instances/{id}/debug
POST /v1/instances/{id}/cli
Content-Type: application/json

{"command":"show status"}
```

CLI endpoint není systémový shell. Přijímá jeden omezený příkaz Smithproxy CLI.
Log endpoint vrací maximálně 2000 řádků a 256 KiB.
GDB helper je dostupný jen pro archivovaný Debug build. Běží jako samostatná
transientní unit s `CAP_SYS_PTRACE` ve stejném netns; diagnostika vrací příkazy
pro SSH tunel a připojení lokálního GDB.

Strojově čitelný základ kontraktu je dostupný jako:

```http
GET /v1/openapi.json
```
