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

{"ref":"master","build_type":"Release","adopt":true}
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
selhání. Pole `build_type` přijímá `Release` nebo `Debug`.

`adopt` je volitelný boolean a pro kompatibilitu API má výchozí hodnotu `true`.
Adopt po úspěšné kompilaci archivuje binárku, jednou importuje její nativní
default config a připraví rootfs image. `adopt:false` provede pouze build a
archivaci. Adopt záměrně nemění runtime profily, cert bundle ani běžící
instance; tyto zásahy vyžadují samostatné rozhodnutí administrátora.

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
  "protocol": "tcp",
  "destination": "10.10.20.0/24",
  "ports": ["443", "8000-8010"],
  "register_source": true,
  "runtime_profile_id": "profile-uuid",
  "user_id": "4711"
}
```

Místo `runtime_profile_id` lze poslat `instance_id` a připojit adresu k živé
instanci; obě pole současně jsou chyba. Profilový spawn používá pouze uložený
runtime profil, nikoli volně dodanou dvojici binárka/config.

Volitelné selektory `protocol` (`any`, `tcp`, `udp`), `destination` (IP/CIDR)
a `ports` (čísla nebo rozsahy) se promítnou do explicitních nft pravidel.
Source a destination musí být ze stejné IP rodiny; porty vyžadují TCP nebo UDP.

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

Přímý driver `tuntom` je jednostranný a lze jej zvolit nezávisle pro ingress
i egress. `tuntom-via` je naopak duplexní profil a spotřebuje oba sloty:

```text
split-veth     di0 → Smithproxy → do0 → SAS uplink
on-a-stick     di0 → Smithproxy → di0 → SAS host
tuntom IN      remote peer ⇄ tuntom ⇄ di0 → Smithproxy
tuntom OUT     Smithproxy → do0 ⇄ tuntom ⇄ remote peer
tuntom-via     VIA switch ⇄ encrypted relay ⇄ adapter ⇄ di0/Smithproxy/do0
blackbox-link  di0 → Smithproxy → do0 → blackbox → Smithproxy → SAS egress
```

Produkčně realizované jsou `split-veth`, `on-a-stick` a duplexní `tuntom-via` s ingress
`selector: source`, povinnou autorizací a dual-stackem. On-a-stick nevytváří
`do0/czo*`; proxy-originated provoz používá default route přes `di0` a hostový
peer ingressu. `blackbox-link` zůstává uložitelný návrhový egress profil s
`implemented:false`, dokud nebude hotový QEMU/TAP lifecycle. Blackbox není
management attachment: jeho datový link je součást egressu a návrat se vede
zpět přes Smithproxy. Egress může zvolit `masquerade|routed` a host uplink.

Oba Tuntom drivery vyžadují `mode: routed`. Přímý `tuntom` zatím zůstává
uložitelný návrhový jednostranný driver a očekává při startu
`local_ip`, `peer_ip`, `peer_host` a jednorázový 32hex `secret`; profil je
neukládá. VIA naopak očekává `headless_endpoint_id`. Unikátní endpoint package
obsahuje Fabric port, switch IP, tunnel ID a secret; runner jej před vytvořením
namespace atomicky rezervuje a po startu trvale sváže s ID jediné instance.
Runner alokuje adresu `fabric0`, relay socket a attachment identity.
Relay i adapter běží přímo v namespace instance; adapter vytvoří oba TUNy
`di0/do0`. Secret nesmí být
součástí profilu, instance JSON, task labelu ani logu.

Transportní `fabric0` je `ipvlan` nad globálně nakonfigurovaným Fabric parent
interfacem. Namespace firewall povoluje pouze UDP k vybranému switch IP a
nezbytné ICMP/ICMPv6. V rootfs režimu se immutable Tuntom binárky připojí
read-only do `/opt/sas/bin`; relay socket je v privátním `/run/sas` a secret
předává systemd credential.

Smithproxy, relay i adapter jsou členy jedné systemd Slice; API vrací `slice_unit`,
souhrnné `slice_rss_bytes` a pole `members` s rolí, unitou, PID a RSS každého
procesu. Stejný model je připravený pro další členy, například QEMU.

Komponenty se nevybírají cestou na hostu. Network profil odkazuje na immutable
build z Tuntom knihovny. Runner sleduje branche a sestavuje `tuntom` i
`tuntom-divert-adapter`
asynchronně přes:

```http
GET    /v1/tuntom/build
POST   /v1/tuntom/build
POST   /v1/tuntom/refs/refresh
DELETE /v1/tuntom/builds/{id}
```

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
  "name": "Tuntom VIA service link",
  "driver": "tuntom-via",
  "mode": "routed",
  "address_family": "dual",
  "interface_name": "do0",
  "tuntom_build_id": "0123456789abcdef0123456789abcdef01234567-release",
  "tuntom_in_prefix": "proxy-in-",
  "tuntom_out_prefix": "proxy-out-",
  "tuntom_admission": "immediate",
  "tuntom_mtu": 1500
}
```

Odpověď profilu obsahuje `consumes:["ingress","egress"]` a
`start_parameters:["headless_endpoint_id"]`. Přímý Tuntom má
`consumes` pouze pro svou stranu a startovací kontrakt obsahuje lokální IP,
peer IP, peer host a secret.

### Unikátní headless endpoint packages

```http
GET    /v1/headless-endpoints
POST   /v1/headless-endpoints
GET    /v1/headless-endpoints/{id}
DELETE /v1/headless-endpoints/{id}
```

Import kontroluje unikátnost `package_id`, `fabric_port_id` i dvojice
`switch_ip + tunnel_id`. Stavový automat je `available → reserved → bound`.
`reserved` chrání souběžné spawn requesty; `bound` zůstává navázaný i po stopu
instance. List a detail API nikdy nevracejí secret. BOUND package nelze smazat
ani automaticky recyklovat pro jiný Slice.

```json
{
  "package_id": "10ea565a-8c05-41d4-989c-b6927cdf21ac",
  "kind": "tuntom-via",
  "name": "Fabric proxy port 17",
  "fabric_port_id": "switch-a/proxy-17",
  "switch_ip": "2001:db8::10",
  "tunnel_id": 17,
  "secret": "00112233445566778899aabbccddeeff"
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
VIA switch a jeho budoucí Fabric UI jsou host-level subsystem; tato verze
spravuje build a lifecycle per-Slice adapteru, nikoli samotný switch.
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

## Appliance Export

```http
GET    /v1/appliance-exports
POST   /v1/appliance-exports
GET    /v1/appliance-exports/{id}
GET    /v1/appliance-exports/{id}/download
DELETE /v1/appliance-exports/{id}
```

Vytvoření patří do `/v1/task-actions`; přijímá `name`, `build_id`, `config_id`,
`filesystem_mode` (`plain` nebo `rootfs`) a mapu `parameters` pro vlastní
placeholdery konfigurace. Výsledný `.tar.gz` má jediný kořenový adresář a
obsahuje `start.sh`, `stop.sh`, `status.sh`, `route.sh`, manifest, binárku,
nativní config a assets. Síťový bootstrap vytvoří dva adresované veth páry s
`di0`/`do0`; host routing, host firewall, SAS autorizace a NAT záměrně nemění.
Transparentní TProxy pravidla jsou pouze uvnitř nového namespace.

Součástí je `repack-from-binary.sh`. Z nové ELF binárky přebalí stejnou kostru,
rekurzivně přidá loader a knihovny dohledatelné pomocí `ldd` a označí manifest
jako repack. Nejde o reprodukovaný build: samotná binárka nepopisuje build-time
assets ani moduly načítané dynamicky přes `dlopen()`.

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
