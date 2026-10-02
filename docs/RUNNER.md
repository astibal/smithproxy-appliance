# Capture Zone Smithproxy runner

Malá root služba pro build a spuštění více časově omezených instancí Smithproxy. API
ve výchozím stavu poslouchá pouze na `127.0.0.1:9080`. Každá instance běží jako
samostatná transientní systemd service s `RuntimeMaxSec` a vlastní cgroup.

```text
portal -> loopback HTTP API -> netns + veth + nftables -> systemd -> smithproxy
```

## API

Všechny endpointy kromě `/healthz` vyžadují `Authorization: Bearer <token>`.

```http
POST /v1/instances
Content-Type: application/json

{
  "runtime_seconds": 300,
  "source_ip": "198.51.100.10",
  "user_id": "user-123",
  "build_id": "active",
  "config_id": "<configuration-uuid>",
  "config_mode": "rw",
  "rewrite_sni": "magic.example.net",
  "rewrite_sni_to": "origin.example.net",
  "parameters": {
    "socks_port": 12001,
    "http_port": 12005,
    "plaintext_port": 12002,
    "tls_port": 12003,
    "cli_port": 12004,
    "workers": 2,
    "pcap_quota_mb": 100
  }
}
```

`source_ip` a `user_id` jsou povinná tvrzení důvěryhodného portálu pro všechny
profily. Runner nedůvěřuje údajům od koncového proxy klienta. Profil se neurčuje
v requestu: je součástí vybrané immutable položky `config_id`, takže jej klient
nemůže změnit nezávisle na ověřené konfiguraci.

Runner po buildu vytvoří a přes skutečnou binárku ověří tři minimální profily:

- `magic-sni`: transparentní listener; vyžaduje `rewrite_sni` a
  `rewrite_sni_to`, které expandují pouze známé placeholdery
  `{{REWRITE_SNI}}` a `{{REWRITE_SNI_TO}}`;
- `socks`: explicitní SOCKS5 listener na `parameters.socks_port`;
- `http-proxy`: explicitní HTTP CONNECT listener na `parameters.http_port`,
  zatím bez proxy autentizace.

SOCKS a HTTP CONNECT provoz je na hostu omezen na autorizovanou source IP
instance. `user_id` zůstává identitou control plane pro audit a budoucí capture
ownership; Smithproxy jej z klientského provozu nezískává.

Každá process unit má vlastní zapisovatelný `tmp/`, `captures/` a `/run`
uvnitř svého runtime adresáře. Systemd `PrivateTmp` izoluje i explicitní cesty
pod `/tmp`, `TMPDIR` ukazuje na privátní `tmp/` a privátní `captures/` je v
mount namespace unity bindnutý na `/var/smithproxy/data`.
Ostatní instance zůstávají díky `ProtectSystem=strict` nezapisovatelné.
Smithproxy má bounding/ambient capability pouze `CAP_NET_RAW` pro GRE a packet
sockety plus `CAP_DAC_OVERRIDE` a `CAP_FOWNER` pro vlastní config/capture
soubory; `CAP_SYS_ADMIN` ani `CAP_NET_ADMIN` nedostává.

Fyzická data všech typů jsou v jediném store `instances/<uuid>/`. Adresáře
`instances/managed/` a `instances/test-drive/` obsahují pouze relativní
symlinky na odpovídající UUID adresáře; nejsou druhou kopií dat.

Alternativně spawn přijme `runtime_profile_id`. Runtime profil je atomický JSON
záznam, který váže celý commit archivované binárky na UUID konfigurace a
volitelný `cert_bundle_id`. Součástí profilu je také `ttl_seconds`; hodnota
`null` znamená unlimited runtime bez aplikačního deadline i bez systemd
`RuntimeMaxSec`. Staré profily bez tohoto pole se načtou s TTL 1800 sekund. Runner
vazbu rozbalí serverově a ignoruje klientské `build_id`/`config_id`. Profily
záměrně nepovolují pohyblivou binárku `active`; tím zůstává jejich chování
reprodukovatelné. Cestu JSON souboru určuje `CZ_RUNNER_RUNTIME_PROFILES`.

```text
GET    /v1/instances
GET    /v1/instances/<uuid>
DELETE /v1/instances/<uuid>
DELETE /v1/instances/<uuid>/record
POST   /v1/instances/cleanup
GET    /v1/sources
GET    /v1/build
POST   /v1/build
GET    /v1/runtime-profiles
POST   /v1/runtime-profiles
DELETE /v1/runtime-profiles/<uuid>
GET    /v1/cert-bundles
POST   /v1/cert-bundles
POST   /v1/cert-bundles/<uuid>/certificates
DELETE /v1/cert-bundles/<uuid>
GET    /v1/test-drives
POST   /v1/test-drives
POST   /v1/test-drives/<uuid>/upgrade
POST   /v1/test-drives/<uuid>/extend
POST   /v1/test-drives/<uuid>/restart
DELETE /v1/test-drives/<uuid>
POST   /v1/instances/<uuid>/cli
GET    /healthz
```

Spawn klient nemůže poslat příkaz, cestu ani raw libconfig. Server přijímá jen
allowlist číselných parametrů a známých textových polí, validuje je a dosadí je
do administrátorem spravované šablony. Neznámý nebo chybějící placeholder spawn
odmítne. `{{RUNTIME_DIR}}` vytváří výhradně runner.

Uživatelské placeholdery se posílají v objektu `template_values`, například
`{"REWRITE_SNI":"magic.example.net"}`. Metadata knihovny obsahují jejich seznam;
serverové placeholdery (`RUNTIME_DIR` a číselné runtime parametry) se v něm
nezobrazují a klient je nesmí přepisovat.

## Instalace

See `docs/SYSTEMD.md` for the separate `sas-runner.service` and optional
`sas-console.service`, their service identities, environment files and install
commands. The units are templates for an appliance deployment and are not
installed by this repository.

Před startem je nutné:

1. vyplnit skutečné autorizované adresy v `source-ips.json`; výchozí soubor je
   záměrně prázdný a runner bez něj žádnou instanci nenabídne;
2. vygenerovat dlouhý náhodný bearer token;
3. vytvořit plnou šablonu z configu odpovídajícího instalované verzi Smithproxy;
4. zkontrolovat porty a policies; každá instance má vlastní namespace, takže interní porty se mohou opakovat;
5. podle reálného provozu upravit `MemoryMax`, `TasksMax` a zapisovatelné cesty.

## Životní cyklus

Systemd ukončí celou cgroup po uplynutí budgetu i při pádu runneru. Runner každé
dvě sekundy synchronizuje stav. Po ukončení odstraní adresář v `/run` včetně
configu; malé stavové JSON metadata ponechá v `/var/lib` pro stavové API.
Nalezenou běžící portal-owned unit bez stavového záznamu automaticky nezabíjí:
zařadí ji jako `orphaned`, ukáže PID/RSS/unit a čeká na explicitní Stop.
SIGINT/SIGTERM runneru zavře pouze API, WebSockety a CLI transporty. Běžící
systemd unity, namespace, nftables pravidla a `/30` lease zůstávají zachované a
nový runner je po startu znovu revaliduje. Úplný teardown je explicitní opt-in
přes `CZ_RUNNER_STOP_INSTANCES_ON_EXIT=1`; běžný restart jej nesmí nastavovat.
Výpis instance obsahuje `pid` a aktuální `rss_bytes` hlavního procesu. Endpoint
`DELETE .../record` smaže metadata až po nové kontrole, že unit neběží a její
dočasné prostředky byly uklizeny.

Vzdálené vystavení zatím není podporované. Další etapa má přidat TLS/mTLS a
omezení zdrojových adres; samotný bearer token pro veřejně dostupný port nestačí.

## Portal a administrátor

Runner se připojuje do existujícího Capture Zone portálu přes loopback. Oběma
službám nastav stejný `CZ_RUNNER_TOKEN`. Prvního administrátora vytvoř:

```bash
cd /opt/capture-zone-portal
.venv/bin/flask --app app:create_app create-admin \
  --email admin@example.com --name Admin
```

Heslo se zadává interaktivně a musí mít alespoň 12 znaků. Po přihlášení je
správa instancí v `Admin -> Smithproxy`.

Build používá pevný repository URL z `CZ_RUNNER_REPOSITORY`; web přijímá pouze
validovaný git ref. Výsledná binárka se atomicky instaluje do cesty
`CZ_RUNNER_SMITHPROXY`. Každý úspěšný commit se současně uloží do knihovny
`builds/<commit-id>/` vedle aktivní binárky; API vrací commit, zadaný ref, čas
buildu a velikost. Archiv obsahuje také odpovídající config a runtime assets.
Spawn request vybírá binárku přes `build_id` (`active` nebo celý commit SHA),
takže nelze podstrčit libovolnou cestu. Konfiguraci lze nezávisle vybrat přes
`config_id`; knihovna obsahuje přesný `smithproxy.cfg` a assets uchované při
úspěšném buildu. Opakovaný build stejného commitu jeho položku atomicky nahradí.

Admin konzole dovoluje nahrát samostatný UTF-8 `.cfg`. Runner jej před uložením
ověří vybranou Smithproxy binárkou přes `--config-check-only`; assets se explicitně
převezmou z vybrané knihovní položky. Efektivní config každé nové instance je
snapshotovaný ve stavovém adresáři, takže jej lze stáhnout nebo uložit zpět do
knihovny i po zastavení instance, dokud není její záznam smazán.

Knihovna konfigurací nepoužívá databázi. Každá položka je samostatný
adresář s `smithproxy.cfg`, `smithproxy.assets/` a `metadata.json`. Metadata
obsahují UUID, jméno, původní commit/ref, čas, velikost a SHA-256 celého bundle.
Před spawnem se checksum znovu ověřuje. Cestu určuje `CZ_RUNNER_CONFIG_LIBRARY`.

CA/Cert knihovna je rovněž pouze souborová. Každý bundle má `metadata.json`,
CA certifikát, CA klíč a volitelné další certifikáty. CA i leaf certifikáty se
generují skutečným OpenSSL; veřejné PEM lze importovat. Privátní klíče mají
režim `0600` a runner je přes API nevydává. Při spawnu se bundle překryje do
privátních assets instance a v efektivním configu se nastaví prázdné heslo
nešifrovaného, root-only CA klíče. Cestu určuje `CZ_RUNNER_CERT_LIBRARY`.

Spawn request volí `config_mode`: `ro` nebo `rw`. Týká se výhradně lokálního
`smithproxy.cfg` konkrétní instance. V režimu `ro` jej systemd mount namespace
vynutí jako read-only; v režimu `rw` jej může Smithproxy přes CLI uložit. Download
běžící instance čte přímo její aktuální soubor. Při stopu, pádu nebo TTL runner
lokální config nejprve atomicky zachová do stavového snapshotu a teprve potom
smaže dočasný runtime adresář. Snapshot lze později stáhnout nebo importovat do
knihovny, dokud administrátor nesmaže záznam instance.

Kompilace používá všechna CPU dostupná runner procesu. Počet paralelních úloh lze
omezit přes `CZ_RUNNER_BUILD_JOBS`, například na `4`, pokud by kompilace měla
příliš vysokou paměťovou režii.

Routing MVP podporuje IPv4. TCP/443 jde na TLS listener, ostatní TCP na plaintext
listener. Source IP musí být uvedená v `source-ips.json` a současně může patřit
jen jedné aktivní instanci. UDP a IPv6 zatím runner záměrně neroutuje.

## Testy

Testy nevyžadují root, systemd ani Smithproxy:

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
```
