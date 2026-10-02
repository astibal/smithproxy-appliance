# Smithproxy Appliance

Control plane a servisní konzole pro provoz izolovaných Smithproxy instancí na
SAS appliance serveru.

```text
Capture Zone Portal
        │  privátní API; pro vzdálený provoz mTLS
        ▼
sas-runner.service (root, headless control plane)
        ├── task queue, build/config/cert store
        ├── systemd transient units
        ├── network namespaces, veth a policy routing
        └── Smithproxy instance

sas-console.service (unprivileged, optional admin UI)
        └── používá stejné Runner API
```

## Struktura

```text
runner/       privilegovaný appliance agent a API
console/      neprivilegovaná Flask admin konzole
sas_client/   znovupoužitelný Python klient runner API
sasctl        headless CLI pro operátory a automatizaci
config/       výchozí konfigurace source IP
deploy/       systemd, sysctl a environment příklady
docs/         API a provozní dokumentace
tests/        rootless unit/integration testy runneru
```

Runtime stav, buildy, konfigurace a certifikáty nejsou součástí repozitáře.
Lokální launcher je ukládá pod `/tmp/capture-zone-runtime`; produkční systemd
nasazení používá `/var/lib/smithproxy-appliance` a `/run/smithproxy-appliance`.
Instance mají jeden plochý canonical store a typové symlink indexy:

```text
instances/<uuid>/
instances/managed/<uuid>    -> ../<uuid>/
instances/test-drive/<uuid> -> ../<uuid>/
```

## Lokální spuštění

Konzole nejprve vytvoří sdílený bearer token a vlastní session secret:

```bash
screen -dmS smithproxy-appliance-console ./run-console.sh
```

Runner spusť z autorizovaného root shellu:

```bash
./run-root-runtime.sh
```

Běžný restart runneru nezastavuje aktivní Smithproxy instance, jejich namespaces
ani routing. Úplný teardown je explicitní přes
`CZ_RUNNER_STOP_INSTANCES_ON_EXIT=1`.

## Testy

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
node --check console/static/app.js
```

Podrobnosti jsou v [Runner dokumentaci](docs/RUNNER.md),
[Console dokumentaci](docs/CONSOLE.md), [systemd deploymentu](docs/SYSTEMD.md)
[CLI dokumentaci](docs/SASCTL.md) a [API přehledu](docs/API.md).
