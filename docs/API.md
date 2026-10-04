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
`update_available`. Runner je obnovuje na pozadí každých pět minut. Explicitní
`POST /v1/refs/refresh` vloží `git fetch` do společné fronty úloh, vrátí task
descriptor a deduplikuje souběžný refresh pod klíčem `refs:refresh`.
Odpověď obsahuje omezený log a informaci, zda existuje spustitelná binárka.
Build log se uchovává bounded a admin konzole jej automaticky zobrazí při
selhání. Archiv buildu obsahuje výchozí config a assets, ale raw výchozí config
se automaticky nevkládá do uživatelské config knihovny.
Pole `build_type` přijímá `Release` nebo `Debug`.

## Fronta úloh a rychlý read model

Dlouhé mutace se provádějí ve workerech runneru, ne ve vlákně HTTP requestu.
Spawn, build a refresh vracejí `202 Accepted` s `task_id`; ostatní podporované
admin akce lze vložit přes `POST /v1/task-actions`. Aktivní shodná operace se
deduplikuje a stav/result je dostupný přes `GET /v1/tasks/{task_id}` a
`GET /v1/tasks/{task_id}/result`.

`GET /v1/status`, seznamy a detaily instancí čtou poslední atomicky uložený
snapshot. Nečekají na lifecycle lock a samy nespouštějí systemd discovery.
Reaper snapshot revaliduje na pozadí každé dvě sekundy, takže read model může
být nejvýše přibližně o tento interval opožděný.

## Knihovna konfigurací

```http
GET    /v1/configs
GET    /v1/configs/{id}
PUT    /v1/configs/{id}/metadata
DELETE /v1/configs/{id}
POST   /v1/configs/preview
POST   /v1/configs/commit
DELETE /v1/configs/previews/{preview_id}
```

Config knihovna přijímá pouze nativní výstup Smithproxy `save config`. Upload či
editace nejprve volá `preview` s povinným přesným `build_id`; runner použije
právě tuto archivovanou binárku, vrátí unified diff vstupu proti nativnímu výstupu a krátkodobé
`preview_id`. Teprve `commit` s `approved: true` config atomicky vytvoří nebo
aktualizuje. Přímé `POST /v1/configs` a `PUT /v1/configs/{id}` jsou zakázané.
Název a popis lze měnit přes metadata endpoint bez spuštění Smithproxy, protože
obsah ani assets konfigurace nemění.
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

Vrací administrátorem nakonfigurované IPv4/IPv6 adresy a jejich dostupnost.
Jedna source IP může být připojena k více živým instancím; konkrétní výsledná
cesta je dána per-instance nft routingem.

## Globální INPUT / FORWARD firewall

Runner spravuje pouze vlastní nft tabulku `inet capture_zone_access`; cizí
iptables/nftables pravidla neimportuje ani nepřepisuje.

```http
GET    /v1/firewall
PUT    /v1/firewall
POST   /v1/firewall/authorizations
POST   /v1/firewall/authorizations/{authorization_id}/extend
DELETE /v1/firewall/authorizations/{authorization_id}
POST   /v1/instances/{instance_id}/sources
```

Enforcement je po prvním nasazení vypnutý. `PUT` jej zapíná nezávisle:

```json
{"input_enforced":false,"forward_enforced":true}
```

Automatická autorizace z jiného Capture Zone systému:

```json
{
  "source": "198.51.100.24",
  "chains": ["forward"],
  "system": "capture-zone-portal",
  "label": "user 4711",
  "ttl_seconds": 1800,
  "register_source": true,
  "runtime_profile_id": "profile-uuid",
  "user_id": "4711"
}
```

Místo `runtime_profile_id` lze poslat `instance_id` a připojit adresu k živé
instanci; obě pole současně jsou chyba. Profilový spawn používá pouze uložený
runtime profil, nikoli volně dodanou dvojici binárka/config.

`register_source` u jedné IPv4 nebo IPv6 současně přidá adresu do spawn source poolu.
CIDR lze autorizovat ve firewallu, ale nelze jej registrovat jako jednu spawn
adresu. INPUT enforcement povoluje loopback, established/related a explicitní
INPUT zdroje. FORWARD enforcement se vztahuje jen na ingress do rozhraní
`czi*`; established návraty a egress přes `czo*` zůstávají povolené. IPv4 a
IPv6 mají samostatné nft interval sety. Nové instance dostávají dva páry
`/30` + `/126`: ingress `czi* ↔ di0` a egress `do0 ↔ czo*`.
`extend` přijímá `{"additional_seconds":3600}`. U živého záznamu přičte čas
k existující expiraci, u expirovaného počítá nový deadline od okamžiku volání.
Trvalou autorizaci bez expirace prodloužit nelze.

## Ingress / egress síťové profily

Síťový profil je samostatná, stateless JSON položka. Runtime profil skládá
binárku, Smithproxy config, volitelný cert bundle a právě jeden ingress a jeden
egress profil. Prázdná vazba znamená globální/default split-veth networking.

```http
GET    /v1/network-profiles
POST   /v1/network-profiles
GET    /v1/network-profiles/{id}
PUT    /v1/network-profiles/{id}
DELETE /v1/network-profiles/{id}
```

Jediný driver je `split-veth`: ingress vždy používá `di0`, egress vždy `do0`.
Pro ingress je nyní realizovaný `selector: source`, povinná autorizace a
dual-stack. Egress může zvolit `masquerade|routed` a host uplink. Destination
selektory a single-family položky se ukládají jako návrh s
`implemented: false`; spawn je bezpečně odmítne.

```json
{
  "kind": "ingress",
  "name": "Authorized source · dual",
  "driver": "split-veth",
  "selector": "source",
  "address_family": "dual",
  "interface_name": "di0",
  "require_authorization": true,
  "destination_cidrs": []
}
```

```json
{
  "kind": "egress",
  "name": "Routed via lab uplink",
  "driver": "split-veth",
  "mode": "routed",
  "address_family": "dual",
  "interface_name": "do0",
  "host_interface": "lab0"
}
```

Použitý síťový profil nelze smazat, dokud na něj odkazuje runtime profil.
Změna katalogové položky se týká až nových spawnů; běžící instance drží svůj
síťový snapshot.

## Volitelný Smithproxy rootfs

Runtime profil má `filesystem_mode: "host"|"rootfs"`. Výchozí `host` zachovává
dosavadní systemd filesystem sandbox. `rootfs` připraví pro zvolený archivovaný
build minimální immutable adresářový strom a spustí Smithproxy s
`RootDirectory=`. Jde o opt-in variantu; existující profily se automaticky
nepřevádějí.

Dependency closure vzniká pouze z ELF metadat přes `readelf` a host linker
cache; analyzovaná binárka se při balení nespouští. Rootfs obsahuje vlastní
`/usr/bin/smithproxy`, dynamický loader, transitivní knihovny, CA trust a
OpenSSL provider moduly. Manifest je svázán SHA-256 hashem archivované binárky.
Změna binárky tedy rootfs automaticky zneplatní.

Stávající runtime mountpointy zůstávají stejné:

```text
per-instance run      -> /run
per-instance workspace-> /work
per-instance capture  -> /var/smithproxy/data
workspace             -> původní absolutní runtime cesta
build directory       -> původní absolutní read-only cesta
```

Rootfs se vytvoří při uložení rootfs runtime profilu nebo líně uvnitř
background spawn tasku. Běžící host-sandbox instance tím nejsou ovlivněné.
Lze jej také připravit předem jako deduplikovanou asynchronní úlohu:

```http
POST /v1/task-actions
{"method":"POST","path":"/v1/builds/<build-id>/rootfs","payload":{},"kind":"build-rootfs","label":"Prepare rootfs"}
```

Přímý `POST /v1/builds/{id}/rootfs` je idempotentní runner operace. Konzole jej
vždy volá přes frontu, takže balení ELF closure neblokuje API thread.

## Instance

```http
GET /v1/instances
GET /v1/instances/{id}
POST /v1/instances
DELETE /v1/instances/{id}
POST /v1/instances/{id}/restart
POST /v1/instances/cleanup
```

```json
{
  "source_ip": "198.51.100.10",
  "build_id": "active",
  "config_id": "active",
  "persistent": false,
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

`persistent: true` chrání zastavený záznam před automatickým i ručním
úklidem. `POST /v1/instances/cleanup` okamžitě revaliduje všechny záznamy,
gzipne dostupné config snapshoty a odstraní pouze plně zastavené
neperzistentní instance. Běžících, orphaned a persistentních instancí se
nedotkne. Pro konzoli se tato operace volá přes společnou task queue.

Vytvoření je úspěšné až po vytvoření namespace, veth, routing pravidel a
transientní systemd jednotky. Instance se automaticky ukončí po runtime limitu.
Při použití `runtime_profile_id` určuje profil také TTL a přepisuje
`runtime_seconds` z požadavku. Profil s `"ttl_seconds": null` je unlimited a
instance proto nemá aplikační deadline ani systemd `RuntimeMaxSec`.
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

## Test Drive

```http
GET    /v1/test-drives
POST   /v1/test-drives
GET    /v1/test-drives/{id}
DELETE /v1/test-drives/{id}
POST   /v1/test-drives/{id}/upgrade
POST   /v1/test-drives/{id}/extend
POST   /v1/test-drives/{id}/restart
POST   /v1/test-drives/{id}/config-mode
POST   /v1/test-drives/{id}/config/preview
GET    /v1/test-drives/{id}/logs
GET    /v1/test-drives/{id}/files
POST   /v1/test-drives/{id}/files
WS     /v1/test-drives/{id}/cli
WS     /v1/test-drives/{id}/shell
```

Test Drive je krátkodobý lab vybrané archivované binárky. Má dva nezávislé
veth páry a spotřebuje dva `/30` lease: routovatelný `di0` ingress a `do0`
egress se samostatným SNAT. Source route uvnitř namespace vrací odpovědi
přijatých spojení přes `di0`; nové outbound spojení proxy používá `do0`.
Odpověď API uvádí obě strany obou párů, namespace, workspace, config, PID a
deadline. CLI zůstává jen na loopbacku namespace a je dostupné přes WebSocket.
Lab shell je neprivilegovaný sandbox nad zapisovatelným `/work`. Po TTL nebo
`DELETE` se odstraní unit, oba veth páry, namespace, lease i workspace.
Smithproxy používá systemd `PrivateTmp`, per-lab `TMPDIR` a per-lab capture
adresář bindnutý pouze v mount namespace jeho unity na `/var/smithproxy/data`.
Stejná izolace platí i
pro běžné instance; žádná unit nemá zapisovatelný runtime jiné instance. GRE
raw socket je povolen explicitně přes `CAP_NET_RAW`, bez `CAP_SYS_ADMIN` a
`CAP_NET_ADMIN`.

Nový Test Drive je standardně `config_mode: "ro"`; při vytvoření lze zvolit
`"rw"`. Endpoint `config-mode` přijímá `{"config_mode":"ro|rw"}`. U běžícího
labu restartuje pouze Smithproxy process unit, zatímco namespace, oba vethy,
workspace a deadline zachová. `config/preview` vezme aktuální config a vytvoří
standardní schvalovací preview. U běžícího RW labu nejdřív provede skutečné CLI
`save config`; u RO nebo zastaveného labu použije izolovaný native-save se
stejným buildem. Do knihovny se config dostane až přes běžný `/v1/configs/commit`.

`POST .../upgrade` přijme pouze `build_id`. Jde záměrně o dirty upgrade:
zastaví původní process unit a spustí vybranou archivovanou binárku se stejným
configem, `/work`, namespace, `di0`/`do0` a původním deadline. Neprovádí
native-save, diff ani migraci assets. Po ručním Smithproxy CLI příkazu
`execute shutdown` zůstane Test Drive ve stavu `stopped` a jeho appliance obal
je až do TTL připravený pro stejný upgrade.

Po dosažení TTL se Test Drive nově nemaže okamžitě. Smithproxy skončí, stav je
`expired`, ale config, `/work`, namespace a networking zůstanou tři hodiny.
`extend` posune deadline; u expirovaného labu jej vrátí do `stopped`. Následný
`restart` spustí stejnou binárku nad aktuálním configem; u expirovaného labu
současně nastaví nový výchozí TTL. U běžícího labu lze
prodloužit deadline bez restartu. Po recovery lhůtě runner lab definitivně
odstraní.

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
