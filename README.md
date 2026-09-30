# Smithproxy Appliance

Control plane a servisní konzole pro provoz izolovaných Smithproxy instancí na
SAS appliance serveru.

```text
Capture Zone Portal
        │  privátní API; pro vzdálený provoz mTLS
        ▼
Smithproxy Appliance Runner (root)
        ├── task queue, build/config/cert store
        ├── systemd transient units
        ├── network namespaces, veth a policy routing
        └── Smithproxy instance

Smithproxy Appliance Console (unprivileged admin UI)
        └── používá stejné Runner API
```

## Struktura

```text
runner/       privilegovaný appliance agent a API
console/      neprivilegovaná Flask admin konzole
config/       výchozí konfigurace source IP
deploy/       systemd, sysctl a environment příklady
docs/         API a provozní dokumentace
tests/        rootless unit/integration testy runneru
```

Runtime stav, buildy, konfigurace a certifikáty nejsou součástí repozitáře.
Lokální launcher je ukládá pod `/tmp/capture-zone-runtime`; produkční systemd
nasazení používá `/var/lib/capture-zone-runner` a `/run/capture-zone-runner`.

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
[Console dokumentaci](docs/CONSOLE.md) a [API přehledu](docs/API.md).
