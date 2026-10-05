# KVM blackbox řetězení

Toto je návrh další vrstvy produktu, ne skrytá součást současného runneru.
SAS zůstává vlastníkem lifecycle, adresace, routingu a auditní stopy. QEMU/KVM
blackbox nedostává výchozí management NIC.

```text
authorized source
      │
      ▼
  SAS ingress
 czi* ↔ di0
      │
      ▼
 Smithproxy A
      │  egress blackbox link
      ▼
 QEMU/KVM blackbox
 qcow2 backing (RO) + per-run overlay (RW)
      │
      └──── return link ────► Smithproxy A ────► SAS egress
```

## Rozhraní a izolace

- Blackbox link je součást egress profilu Smithproxy, nikoli ingress nebo
  management attachment. Odchozí provoz blackboxu se routuje zpět přes tutéž
  proxy a teprve potom na SAS egress.
- Každý link má vlastní TAP/veth a vlastní malý IPv4 `/30` + IPv6 `/126` pool.
- Fyzický adaptér se do namespace nepřesouvá. SAS drží host konce a routy.
- Smithproxy i QEMU mají vlastní namespace; řetězení je explicitní route mezi
  segmenty, nikoli policy-routing magie nad jedním sdíleným uplinkem.
- QEMU má `-nodefaults`, žádnou implicitní síť a jen zařízení uvedená v profilu.
- Konzole používá Unix socket (`-serial` nebo virtio-console) přes xterm.js.
  QMP má druhý root-only Unix socket a nikdy neposlouchá na dataplane IP.

## Disky a data

```text
images/<image-id>/manifest.json      N disků + N síťovek jedné blackbox VM
images/<image-id>/disk-0.qcow2       immutable knihovní vstup
images/<image-id>/disk-N.qcow2       další system/data disky
instances/<uuid>/blackbox/disk-0.qcow2 per-run overlay
instances/<uuid>/blackbox/disk-N.qcow2 overlay každého zapisovatelného disku
instances/<uuid>/work/               explicitní výměna souborů
instances/<uuid>/captures/           exportované artefakty
```

Každý blackbox manifest má pole `disks[]` (1–16) a `nics[]` (0–16). Disk nese
`target`, `bus`, `role` a boot pořadí; NIC nese stabilní slot, emulovaný model a
účel `dataplane` nebo omezený `telemetry`. QEMU nesmí přidat implicitní disk ani
NIC. Base image se nemění. Dirty test používá disposable overlay; persistentní
výsledek musí administrátor explicitně exportovat. `/work` lze připojit jako
omezený virtiofs share nebo samostatný datový disk. Host adresáře se nepřipojují
do guestu automaticky.

### Reset do původního stavu

Importovaný QCOW2 je vždy immutable base a QEMU jej nikdy neotevírá pro zápis.
Každý start instance vytvoří pro každý zapisovatelný disk nový per-instance
QCOW2 overlay. Reset je lifecycle operace nad celou VM, nikoli nad jedním diskem:

```text
stop VM → zavřít QMP → smazat všechny overlaye → vytvořit čistou sadu → start VM
```

Původní stav znamená přesně stav všech base disků v okamžiku importu. Operace je
all-or-nothing: při chybě přípravy kteréhokoli overlaye VM zůstane zastavená a
nedostane smíšenou kombinaci starých a nových disků. Externí stav (`/work`,
captures a telemetrie) se resetem nemaže; v UI musí být uveden zvlášť. Uložený
snapshot je také konzistentní sada overlayů všech zapisovatelných disků.

## Příprava pro forenzní analýzu

Forenzní režim nesmí analyzovat ani měnit originální base image. Před resetem
nebo destrukcí instance provede `forensic seal` nad požadovanou důkazní sadou:

```text
evidence/<case-id>/<capture-id>/
├── manifest.json       identita image, VM konfigurace, UTC časy, operátor
├── manifest.sha256     hash manifestu
├── disks/              overlay každého zapisovatelného disku + hash
├── memory/             volitelný konzistentní RAM dump + hash
├── network/            PCAP pro každou dataplane NIC + hash
└── logs/               QEMU/QMP/serial lifecycle log + hash
```

- Evidence export je immutable; analýza používá samostatnou pracovní kopii.
- Manifest eviduje hash algoritmus, původní image ID, base disk cesty a hashe,
  QEMU machine/CPU konfiguraci, pořadí disků/NIC, UTC časy a auditní identitu.
- Capture se nejprve uzavře, dopíše a synchronizuje; až potom se vypočítají
  hashe. Neúplný export má stav `failed/unsealed` a nesmí být vydáván za důkaz.
- Vícediskový snapshot, RAM a síťové capture sdílejí jedno `capture-id`, aby se
  nezaměnily artefakty z různých okamžiků.
- Reset s aktivním `seal_before_reset` je odmítnut, dokud evidence seal úspěšně
  neskončí nebo jej oprávněný operátor explicitně nepřeskočí s auditním důvodem.

## Management a telemetrie

Výchozí control plane je pouze sada hostových Unix socketů. Pokud instance
potřebuje GRE, syslog nebo jinou telemetrii, dostane volitelný třetí profilovaný
kanál s allowlistem protokolů a cílových IP. Není to obecný management přístup.

```text
guest telemetry NIC ── firewall allowlist ──► GRE collector / syslog target
                     X SSH, web, arbitrary egress
```

## Lifecycle kontrakt

1. Task připraví qcow2 overlay, namespaces, linky a routy.
2. Spustí Smithproxy a blackbox jako oddělené transient systemd unity s
   navázaným failure/cleanup stavem.
3. Readiness se vyhodnotí po každém hopu; provoz se otevře až po kompletním
   sestavení řetězce.
4. TTL nebo explicitní stop zavře ingress, uloží požadované artefakty a uklidí
   TAP/veth, namespace, routy, sockets a disposable overlay.
5. Restart runneru běžící chain nezabije; po návratu jej objeví podle unit
   metadata stejně jako dnešní Smithproxy instance.

## Vazba na současné profily

Runtime profil nadále skládá build, config, cert bundle, rootfs a ingress/egress
profil. Budoucí chain profil pouze přidá uspořádané uzly a linky:

```text
runtime profile
  ingress_profile: authorized-source-dual
  nodes:
    - smithproxy: inspect-both-directions
    - qemu: malware-lab-image
  egress_profile: blackbox-return-link
```

První implementační řez je dvouuzlový `Smithproxy ⇄ QEMU`, bez obecného
grafového editoru. Egress profil vlastní link do blackboxu i explicitní
návratovou cestu přes proxy.
