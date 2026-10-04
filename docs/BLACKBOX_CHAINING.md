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
      │  samostatný L3 point-to-point segment
      ▼
 QEMU/KVM blackbox
 qcow2 backing (RO) + per-run overlay (RW)
      │  volitelný druhý L3 segment
      ▼
 Smithproxy B (volitelný egress observer)
      │
      ▼
  SAS egress
 do0 ↔ czo*
```

## Rozhraní a izolace

- Každý hop má vlastní TAP/veth a vlastní malý IPv4 `/30` + IPv6 `/126` pool.
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
2. Spustí Smithproxy A, blackbox a případně Smithproxy B jako oddělené
   transient systemd unity s navázaným failure/cleanup stavem.
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
    - smithproxy: inspect-in
    - qemu: malware-lab-image
    - smithproxy: inspect-out   # optional
  egress_profile: routed-lab-uplink
```

První implementační řez má být dvouuzlový `Smithproxy → QEMU`, bez obecného
grafového editoru. Teprve ověřený datový model má dostat třetí uzel a UI.
