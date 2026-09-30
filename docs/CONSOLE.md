# Smithproxy Appliance Console

Tenká admin konzole odvozená vzhledem z Capture Zone. Neobsahuje DNS,
uživatelskou samoobsluhu ani PCAP viewer. Veškeré operace deleguje na Smithproxy
runner API v1; konzole nemá root oprávnění.

UI je rozdělené na `Runtime`, `Binárky`, `Konfigurace`, `Profily` a
`Certifikáty`. Runtime může
vybrat pevný runtime profil, nebo ručně zkombinovat binárku a config. Z metadat
zvoleného configu vždy dynamicky vytvoří pole pro uživatelské `{{PLACEHOLDERY}}`.
Při spawnu administrátor volí, zda bude lokální config appliance `RO`, nebo `RW`.
Konfigurační tab umí aktuální či zachovaný lokální config instance stáhnout.
Import z živé RW instance nejprve provede Smithproxy `save config`, zobrazí diff
a do centrální knihovny jej zapíše až po explicitním schválení administrátorem.

Runtime je responzivní master/detail workspace. Levý panel poskytuje živé
vyhledávání a filtry instancí, pravý panel odděluje přehled, xterm konzoli, logy
a diagnostiku. Polling aktualizuje jen dotčené prvky (PID, RSS, TTL a stav),
nikoli celou stránku; na úzkém displeji se seznam a detail skládají pod sebe.
Spawn formulář je v samostatném dialogu, aby běžný dohled nezabíral místo.

Konfigurace se upravují ve vendorizovaném CodeMirror 6 editoru. Editor má
řádky, hledání, folding, bracket matching, undo/redo, Tab indentation, volitelné
zalomení a změnu velikosti písma. Upload i editace vždy projdou nejnovějším
archivovaným master buildem a jeho `save config`; UI zobrazí requested→native
diff a vyžádá explicitní souhlas. Teprve poté se atomicky uloží celý nativní
config+assets bundle. Legacy položky jsou pouze ke čtení/migraci a nelze je
použít pro nový profil ani spawn. Runtime nepoužívá CDN ani Node.js; JS bundle je v
`static/vendor/codemirror/`. Rebuild pro vývoj: `npm run build:editor`.

Certifikační tab generuje CA páry a další CA-podepsané certifikáty přes OpenSSL
nebo přijímá veřejný PEM. Privátní klíče konzole nestahuje; runtime profil pouze
odkazuje na root-only bundle v runneru.

## Spuštění

Konzole běží bez root oprávnění. Launcher vytvoří chybějící sdílený runner
token a session secret v `/tmp` se způsobem `0600` a pak spustí Flask server:

```bash
screen -dmS smithproxy-appliance-console \
  /home/astib/Documents/Capture.Zone/src/smithproxy-appliance/run-console.sh
```

Root runner se spouští zvlášť z autorizovaného root shellu až poté, co existuje
`/tmp/capture-zone-runtime.env`:

```bash
/home/astib/Documents/Capture.Zone/src/smithproxy-appliance/run-root-runtime.sh
```

Prvního administrátora lze vytvořit interaktivně:

```bash
set -a
source /tmp/capture-zone-runtime.env
source /tmp/smithproxy-appliance-console-runtime.env
set +a
console/.venv/bin/python -m flask \
  --app console/app.py:create_app create-admin --email admin@example.com
```

Konzole nepoužívá SQLite. Účty jsou v JSON souboru nastaveném přes
`SMITHPROXY_APPLIANCE_CONSOLE_ADMINS` (výchozí `console/instance/admins.json`, režim
`0600`) a auditní události jdou do procesu/journalu. Runtime stav zůstává pouze
v session cookie a v runner API.

## UI a xterm pravidla

Současný vzhled konzole je referenční: střídmý tmavý toolbar s čitelnými
textovými akcemi, černý terminál s vnitřním odsazením, výrazná modrá selection
a logy v samostatném panelu pod terminálem.

- Xterm se smí inicializovat až po odkrytí terminálového panelu. Inicializace
  uvnitř elementu s `hidden` rozbije měření znaků, změnu fontu i selection.
- `style-src` musí obsahovat `'unsafe-inline'`, protože tato vendored verze
  xtermu za běhu vytváří `<style>` bloky a řádkové styly pro glyphy a selection.
  `script-src` zůstává omezené na `'self'`.
- Změna fontu musí nastavit `term.options.fontSize`, vyčistit texture atlas,
  spustit fit a překreslit řádky.
- Neměnit toolbar zpět na ikonky bez popisků ani na globální zelený button styl.
