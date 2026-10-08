import {identity, completed, changed, filtered, buildChoices, countdown, resourceScope, buildFreshness} from './model.js';
import {workflows} from './workflows.js';
import {bindRowSelection} from './row-selection.js';
import {terminalWorkspace} from './terminals.js';
import {renderDiagnostics} from './diagnostics.js';
import {fieldLabel,choiceLabel} from './field-labels.js';
import {usagePanel} from './usage.js';

const $ = id => document.getElementById(id);
let csrf = document.querySelector('meta[name="csrf-token"]').content;
let lang = localStorage.getItem('sas-next-language') || document.documentElement.lang;
const words = {
  administration:['Administrace','Administration','Administration'],
  terminals:['Terminály','Terminals','Terminaux'],refresh:['Obnovit','Refresh','Actualiser'],
  oldCode:['Starší kód','Older code','Code plus ancien'],newerAvailable:['Novější build dostupný','Newer build available','Build plus récent disponible'],certContents:['Certifikáty v bundlu','Bundle certificates','Certificats du bundle'],privateKey:['S privátním klíčem','With private key','Avec clé privée'],
  logout:['Odhlásit','Sign out','Se déconnecter'],
  accountState:['Stav účtu','Account status','État du compte'],created:['Vytvořen','Created','Créé'],passwordChanged:['Změna hesla','Password changed','Mot de passe modifié'],
  enabled:['Aktivní','Active','Actif'],disabled:['Deaktivovaný','Disabled','Désactivé'],
  ingress_ip:['Ingress IP','Ingress IP','IP d’entrée'],egress_ip:['Egress IP','Egress IP','IP de sortie'],ingress_interface:['Ingress rozhraní','Ingress interface','Interface d’entrée'],egress_interface:['Egress rozhraní','Egress interface','Interface de sortie'],ingress_host_ip:['Ingress IP hosta','Host ingress IP','IP d’entrée de l’hôte'],ingress_host_interface:['Ingress rozhraní hosta','Host ingress interface','Interface d’entrée de l’hôte'],host_ip:['IP hosta','Host IP','IP de l’hôte'],host_interface:['Rozhraní hosta','Host interface','Interface de l’hôte'],workspace:['Pracovní adresář','Workspace','Répertoire de travail'],config_path:['Cesta konfigurace','Configuration path','Chemin de configuration'],binary_path:['Cesta binárky','Binary path','Chemin du binaire'],config_mode:['Režim konfigurace','Configuration mode','Mode de configuration'],
  created_at:['Vytvořeno','Created','Créé'],built_at:['Sestaveno','Built','Compilé'],deadline:['Konec platnosti','Deadline','Échéance'],source_ip:['Zdrojová IP','Source IP','IP source'],build_id:['ID buildu','Build ID','ID du build'],config_id:['ID konfigurace','Configuration ID','ID de configuration'],filesystem_mode:['Souborový systém','Filesystem','Système de fichiers'],rootfs_variant:['Varianta rootfs','Rootfs variant','Variante rootfs'],commit_id:['Commit','Commit','Commit'],ref:['Větev / ref','Branch / ref','Branche / référence'],build_type:['Typ buildu','Build type','Type de build'],work_dir:['Pracovní adresář','Working directory','Répertoire de travail'],namespace:['Namespace','Namespace','Namespace'],pid:['PID','PID','PID'],native:['Nativní config','Native config','Configuration native'],
  admins:['Administrátoři','Administrators','Administrateurs'],
  qemu:['QEMU obrazy','QEMU images','Images QEMU'],preferences:['Preference','Preferences','Préférences'],
  done:['Dokončeno','Completed','Terminé'],
  result:['Výsledek','Result','Résultat'],dirty:['Zahodit rozepsané změny?','Discard unsaved changes?','Abandonner les modifications ?'],
  useNewer:['Použít novější build','Use newer build','Utiliser le build plus récent'],usage:['Používají profily','Used by profiles','Utilisé par les profils'],
  unlimited:['Bez limitu','Unlimited','Illimité'],validity:['Platnost','Validity','Validité'],source:['Zdroj','Source','Source'],selector:['Selector','Selector','Sélecteur'],buildAge:['Build / stáří kódu','Build / code age','Build / âge du code'],ports:['Porty / zapojení','Ports / connections','Ports / connexions'],
  all:['Vše','All','Tous'],custom:['Vlastní configy','Custom configs','Configurations personnalisées'],builtin:['Výchozí šablony','Built-in templates','Modèles intégrés'],buildDefaults:['Defaulty buildů','Build defaults','Configurations des builds'],otherPrograms:['Ostatní programy','Other programs','Autres programmes'],running:['Běžící','Running','En cours'],problems:['Problémy / orphaned','Problems / orphaned','Problèmes / orphelins'],stopped:['Zastavené','Stopped','Arrêtées'],
  firewall:['Firewall','Firewall','Pare-feu'],settings:['Nastavení','Settings','Paramètres'],
  instances:['Instance','Instances','Instances'], profiles:['Profily','Profiles','Profils'], programs:['ELF storage','ELF storage','Stockage ELF'],
  binaries:['Smithproxy binárky','Smithproxy binaries','Binaires Smithproxy'], tuntom:['Tuntom binárky','Tuntom binaries','Binaires Tuntom'], configs:['Konfigurace','Configurations','Configurations'],
  networks:['Síťové profily','Network profiles','Profils réseau'], wiring:['Wiring','Wiring','Câblage'], certificates:['Certifikáty','Certificates','Certificats'], endpoints:['Fabric endpointy','Fabric endpoints','Endpoints Fabric'],
  'test-drives':['Test Drives','Test Drives','Test Drives'], exports:['Exporty','Exports','Exports'], tasks:['Úlohy','Tasks','Tâches'],
  runtime:['Provoz','Runtime','Exécution'], library:['Knihovny','Libraries','Bibliothèques'], network:['Síť','Network','Réseau'], adhoc:['AdHoc','AdHoc','AdHoc'],
  search:['Hledat…','Search…','Rechercher…'], empty:['Žádné položky','No items','Aucun élément'], loading:['Načítám…','Loading…','Chargement…'],
  name:['Název / identita','Name / identity','Nom / identité'], state:['Stav / typ','State / type','État / type'], reference:['Verze / síť','Version / network','Version / réseau'], details:['Podrobnosti','Details','Détails'],
  refreshed:['Aktualizováno','Updated','Actualisé'], offline:['Obnova selhala','Refresh failed','Échec de l’actualisation'], preview:['Nová konzole · ověřovací provoz. Původní konzole zůstává dostupná na portu 5000.','New console · validation rollout. The original console remains available on port 5000.','Nouvelle console · déploiement de validation. La console originale reste disponible sur le port 5000.'],
  add:['Přidat','Add','Ajouter'], save:['Uložit','Save','Enregistrer'], edit:['Upravit','Edit','Modifier'], close:['Zavřít','Close','Fermer'],
  spawn:['Spustit','Start','Démarrer'], stop:['Zastavit','Stop','Arrêter'], restart:['Restartovat','Restart','Redémarrer'], delete:['Smazat','Delete','Supprimer'],
  confirm:['Opravdu provést tuto operaci?','Perform this operation?','Effectuer cette opération ?'], queued:['Zařazeno do fronty','Queued','Mis en file'],
  idle:['Klid','Idle','Au repos'], active:['běžících / čekajících','running / pending','actives / en attente'], logs:['Logy','Logs','Journaux'], diag:['Diagnostika','Diagnostics','Diagnostic'],
  title:['Název','Name','Nom'], application:['Program','Application','Programme'], artifact:['Artefakt','Artifact','Artefact'], config:['Konfigurace','Configuration','Configuration'],
  args:['Argumenty (JSON pole)','Arguments (JSON array)','Arguments (tableau JSON)'], ttl:['TTL v sekundách · prázdné = bez limitu','TTL seconds · empty = unlimited','TTL en secondes · vide = illimité'],
  exit:['on-exit · úspěšné ukončení','on-exit · successful exit','on-exit · sortie réussie'], failure:['on-failure · chyba/pád','on-failure · error/crash','on-failure · erreur/crash'],
  path:['Cesta na originu','Path on origin','Chemin sur l’origine'], file:['Nebo nahrát ELF · max. 16 MiB','Or upload ELF · max. 16 MiB','Ou importer ELF · 16 Mio max.'], version:['Verze / popisek','Version / label','Version / libellé'],
  terminalBusy:['Nahradit otevřený terminál?','Replace the open terminal?','Remplacer le terminal ouvert ?'], invalid:['Neplatná odpověď serveru','Invalid server response','Réponse serveur invalide'],
  expired:['Relace vypršela. Přihlas se znovu v novém panelu; zde zůstávají rozepsaná data.','Session expired. Sign in in a new tab; unsaved data stays here.','Session expirée. Reconnectez-vous dans un nouvel onglet ; les modifications restent ici.'],
};
const t = key => (words[key] || [key,key,key])[Math.max(0,['cs','en','fr'].indexOf(lang))];
const text = (node, value) => { const s=String(value ?? '—'); if(node.textContent!==s) node.textContent=s; };
function el(tag, attrs={}, ...children) { const node=document.createElement(tag); for(const [key,value] of Object.entries(attrs)) { if(key==='class') node.className=value; else node.setAttribute(key,value); } node.append(...children); return node; }
function button(label, action, className='') {const node=el('button',{type:'button',class:className},label);if(label==='×')node.setAttribute('aria-label',t('close')); node.addEventListener('click',action); return node;}
const selection = () => Boolean(window.getSelection()?.toString());
async function copyValue(value) {
  if(navigator.clipboard?.writeText)return navigator.clipboard.writeText(value);
  const input=el('textarea',{'aria-label':'Copy'});input.value=value;document.body.append(input);input.select();
  try{if(!document.execCommand('copy'))throw Error('Clipboard unavailable');}finally{input.remove();}
}
let noticeTask=null;
function notice(message,error=false) {noticeTask=null;text($('notice'),message); $('notice').hidden=false; $('notice').classList.toggle('error',error);$('notice').removeAttribute('aria-busy');}
async function request(url, options={}) {
  const controller=new AbortController(); const timer=setTimeout(()=>controller.abort(),20000);
  try {
    const response=await fetch(url,{...options,signal:controller.signal,cache:'no-store',headers:{Accept:'application/json','X-CSRF-Token':csrf,...options.headers}});
    if(response.status===401 || response.redirected && new URL(response.url).pathname==='/login') throw Error(t('expired'));
    if(!response.headers.get('Content-Type')?.includes('application/json')) throw Error(t('invalid'));
    const data=await response.json(); if(!response.ok) throw Error(data.error || `HTTP ${response.status}`); return data;
  } finally { clearTimeout(timer); }
}
const cache=new Map(), flights=new Map(), views=new Map();
let current='instances', selectedTaskStates=null, lastTaskRender='';
async function fetchItems(resource) {
  if(flights.has(resource)) return flights.get(resource);
  const operation=request(resource==='admins'?'/next-api/admins':'/next-api/catalog/'+resource).then(data=>{
    if(['binaries','tuntom'].includes(resource))data.items=buildFreshness(data.items);
    const view=views.get(resource);if(view)view.health.hidden=true;
    cache.set(resource,data.items); if(current===resource) paint(resource);
    text($('connection'),`${t('refreshed')} ${new Date().toLocaleTimeString(lang)}`); $('connection').classList.remove('error');
    if(resource==='tasks') taskUpdate(data.items);
    return data.items;
  }).catch(error=>{text($('connection'),`${t('offline')}: ${error.message}`); $('connection').classList.add('error');const view=views.get(resource);if(view){view.health.hidden=false;text(view.health,`${t('offline')}: ${error.message}`);if(!cache.has(resource))text(view.empty,t('offline'));} throw error;}).finally(()=>flights.delete(resource));
  flights.set(resource,operation); return operation;
}
const groups=[['runtime',['instances','profiles']],['library',['programs','binaries','tuntom','configs','certificates','qemu']],['network',['wiring','networks','firewall','endpoints','settings']],['adhoc',['test-drives','exports']],['administration',['tasks','preferences','admins']]];
const icons={instances:'▦',profiles:'◇',programs:'⬡',binaries:'▤',tuntom:'⇆',configs:'≡',certificates:'♧',qemu:'▣',preferences:'⚙',admins:'♙',wiring:'⌁',networks:'⇄',firewall:'⊞',settings:'⚙',endpoints:'◎','test-drives':'▷',exports:'↗',tasks:'☷'};
function navigation() {
  const scroll=$('navigation').scrollTop;
  $('navigation').replaceChildren(...groups.flatMap(([group,items])=>[el('p',{class:'nav-label'},t(group)),...items.map(key=>el('a',{href:'#'+key,'data-route':key,class:current===key?'active':''},el('span',{'aria-hidden':'true'},icons[key]),t(key)))]));
  $('navigation').scrollTop=scroll;
  $('refresh').setAttribute('aria-label',t('refresh'));$('refresh').title=t('refresh');
  if($('terminal-toggle'))text($('terminal-toggle'),`${t('terminals')} ${$('terminal-toggle').dataset.count||0}`);
  text($('task-label'),t('tasks')); text($('editor-save'),t('save'));
  const logout=document.querySelector('form[action="/logout"] button');if(logout)text(logout,t('logout'));
  document.querySelectorAll('[data-language]').forEach(control=>control.setAttribute('aria-pressed',String(control.dataset.language===lang)));
}
function createView(resource) {
  const section=el('section',{class:'resource-view','data-view':resource});
  const search=el('input',{type:'search',placeholder:t('search'),'aria-label':t('search')});
  const count=el('span',{class:'count'}), toolbar=el('div',{class:'toolbar'},search,count);
  if(['profiles','programs','wiring'].includes(resource)) toolbar.append(button('+ '+t('add'),()=>openEditor(resource), 'primary'));
  workflow.toolbar(resource,toolbar);
  const scopes={configs:[['all','all'],['custom','custom'],['build','buildDefaults'],['builtin','builtin']],profiles:[['all','all'],['smithproxy','Smithproxy'],['programs','otherPrograms']],instances:[['all','all'],['active','running'],['problems','problems'],['stopped','stopped']]};
  const filters=el('div',{class:'scope-filters',role:'group','aria-label':t('search')});
  for(const [scope,label]of scopes[resource]||[]) {const control=button(t(label),()=>{views.get(resource).scope=scope;paint(resource);});control.dataset.scope=scope;filters.append(control);}
  const headerKeys={admins:['Email','accountState','created','passwordChanged'],firewall:['source','Chain','selector','validity'],binaries:['name','state','Commit','buildAge'],tuntom:['name','state','Commit','buildAge'],wiring:['name','state','ports','details'],endpoints:['name','state','Switch / tunnel','Instance']};
  const headers=(headerKeys[resource]||['name','state','reference','details']).map(key=>el('th',{scope:'col'},t(key)));
  const body=el('tbody'), table=el('table',{},el('thead',{},el('tr',{},...headers)),body);
  const empty=el('p',{class:'empty'},t('loading'));
  const health=el('p',{class:'error',role:'status',hidden:''});
  const inspector=el('aside',{class:'inspector',hidden:''});
  section.append(toolbar,health,filters,el('div',{class:'resource-layout'},el('div',{class:'table-scroll'},table,empty),inspector));
  $('views').append(section);
  const view={section,search,count,body,empty,health,inspector,filters,scope:'all',rows:new Map(),selected:null,lastDetail:null}; views.set(resource,view);
  search.addEventListener('input',()=>paint(resource)); return view;
}
function columns(resource,item) {
  const date=value=>{const parsed=new Date(value);return Number.isFinite(parsed.valueOf())?new Intl.DateTimeFormat(lang,{dateStyle:'medium',timeStyle:'short'}).format(parsed):value||'';};
  if(resource==='admins')return [item.email,t(item.disabled?'disabled':'enabled'),item.created_at?new Date(item.created_at).toLocaleString(): '—',item.password_changed_at?new Date(item.password_changed_at).toLocaleString(): '—'];
  const ttl=deadline=>countdown(deadline)===null?t('unlimited'):'◷ '+countdown(deadline);
  if(resource==='firewall')return [item.source,(item.chains||[]).join(' + '),[item.destination,item.protocol,item.ports].filter(Boolean).join(' · '),ttl(item.expires_at)];
  if(resource==='configs')return [item.name,item.native?t('native'):'—',(item.normalized_build_id||item.source_commit||'').slice(0,12),date(item.updated_at||item.created_at)];
  if(resource==='test-drives')return [identity(item).slice(0,12),item.state,`${item.ingress_ip||'—'} → ${item.egress_ip||'—'}`,ttl(item.deadline)];
  if(['binaries','tuntom'].includes(resource)){const age=key=>item[key]?Math.max(0,Math.floor((Date.now()-Date.parse(item[key]))/86400000))+' d':'?';return [`${item.ref||'detached'} · ${item.build_type||''}`,`${item.rootfs_ready?'rootfs ✓':'ELF'}${item.newer_build_available?' · ⚠ '+t('oldCode'):''}`,(item.commit_id||identity(item)).slice(0,12),`${item.built_at?new Date(item.built_at).toLocaleDateString(lang):'—'} · build ${age('built_at')} / code ${age('commit_at')}`];}
  if(resource==='wiring')return [item.name,`${item.kind} · ${item.state}`,(item.endpoints||[]).length+(item.kind==='virtual-cable'?' / 2':''),item.error||item.namespace];
  if(resource==='endpoints')return [item.name,item.state,`${item.switch_ip} · ${item.tunnel_id}`,item.bound_instance_id||'—'];
  const name=item.name || item.alias || item.label || item.email || item.source || identity(item);
  const state=[item.state || item.application || item.kind || item.build_type || '',item.available===false?'unavailable':item.implemented===false?'unsupported':''].filter(Boolean).join(' · ');
  const ref=resource==='instances'?[item.source_ip,item.namespace].filter(Boolean).join(' · '):item.source_ip || item.ref || item.version || item.config_name || item.namespace || item.commit_id || item.rootfs_variant || '';
  const pids=(item.members||[]).filter(m=>m.pid).map(m=>m.pid);
  if(item.pid&&!pids.includes(item.pid))pids.push(item.pid);
  const detail=resource==='instances' ? `${pids.length?'PID '+pids.join(', '):'—'} · ${((item.slice_rss_bytes || item.rss_bytes || 0)/1048576).toFixed(1)} MiB · ${ttl(item.deadline)}` : resource==='test-drives'?ttl(item.deadline):item.description || date(item.created_at || item.updated_at);
  return [name,state+(resource==='profiles'&&item.newer_build_available?' · ⚠ '+t('newerAvailable'):''),ref,detail];
}
function paint(resource) {
  const view=views.get(resource); if(!view || current!==resource || selection()) return;
  const data=cache.get(resource); if(!data) return;
  const items=filtered(resourceScope(data,resource,view.scope),view.search.value), wanted=new Set(items.map(identity));
  for(const control of view.filters.children)control.setAttribute('aria-pressed',String(control.dataset.scope===view.scope));
  text(view.count,`${items.length} / ${data.length}`); view.empty.hidden=items.length>0; text(view.empty,t('empty'));
  for(const [id,row] of view.rows) if(!wanted.has(id)){ row.remove(); view.rows.delete(id); }
  items.forEach((item,index)=>{
    const id=identity(item); let row=view.rows.get(id);
    if(!row) {
      row=el('tr',{'data-id':id});
      const activate=()=>{if(selection())return;view.selected=id;view.lastDetail=null;history.replaceState(null,'','#'+resource+'/'+encodeURIComponent(id));paint(resource);if(matchMedia('(max-width:1100px)').matches)view.inspector.scrollIntoView({block:'start'});};
      const select=button('',activate);select.tabIndex=-1;
      row.append(el('td',{},select),el('td'),el('td'),el('td'));
      bindRowSelection(row,activate,selection);
      view.rows.set(id,row);
    }
    const values=columns(resource,item); text(row.children[0].firstChild,values[0]); for(let n=1;n<4;n++) text(row.children[n],values[n]);
    row.classList.toggle('selected',view.selected===id); row.classList.toggle('inactive',['stopped','expired','failed'].includes(item.state)||item.available===false||item.implemented===false);
    row.setAttribute('aria-selected',String(view.selected===id));
    if(view.body.children[index]!==row) view.body.insertBefore(row,view.body.children[index] || null);
  });
  const item=data.find(item=>identity(item)===view.selected);
  if(item && changed(view.lastDetail,item)) { detail(resource,item,view); view.lastDetail=structuredClone(item); }
  else if(!item) { view.inspector.hidden=true; view.lastDetail=null; }
}
async function action(resource,command,id='',payload={}) {
  const result=await request('/next-api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({resource,action:command,id,payload})});
  notice(`${t(result.task_id?'queued':'done')} · ${result.task_id || result.id || command}`);noticeTask=result.task_id||null;if(noticeTask)$('notice').setAttribute('aria-busy','true');fetchItems('tasks').catch(()=>{});if(!result.task_id){fetchItems(current).catch(()=>{});window.dispatchEvent(new Event('sas:task-completed'));}return result;
}
function actionButton(resource,command,item,danger=false) {
  const control=button(t(command),async()=>{if(danger && !confirm(t('confirm')))return; control.disabled=true; try{await action(resource,command,identity(item));}catch(error){notice(error.message,true);}finally{control.disabled=false;}},danger?'danger':''); return control;
}
function detail(resource,item,view) {
  view.inspector.hidden=false;
  if(view.inspector.dataset.identity!==identity(item)) {
    view.inspector.querySelector('.inspection-output')?.remove();
    view.inspector.dataset.identity=identity(item);
    if(resource==='instances'&&view.inspectionKind)inspectRequest(item,view.inspectionKind);
  }
  if(!view.inspector.firstChild) {
    view.inspector.append(el('header',{},el('h2'),button('×',()=>{view.selected=null;view.inspector.hidden=true;})),el('div',{class:'actions'}),el('dl',{class:'summary-fields'}),el('details',{},el('summary',{},'JSON'),el('pre',{class:'object-detail'})));
  }
  text(view.inspector.querySelector('h2'),item.name || item.alias || item.email || identity(item).slice(0,12));
  const actions=view.inspector.querySelector('.actions');
  // Only rebuild controls when identity/state changes, never replace an open terminal.
  const sig=JSON.stringify([identity(item),item.state,item.updated_at,item.newer_build_id,item.debug_unit,item.system_start_enabled,item.usage,item.available,item.application,item.name,item.email,lang]);
  if(actions.dataset.signature!==sig) {
    actions.dataset.signature=sig; actions.replaceChildren();
    if(resource==='instances') {
      if(['running','starting','orphaned'].includes(item.state)) actions.append(actionButton(resource,'stop',item,true));
      if(item.state==='running'){
        actions.append(actionButton(resource,'restart',item),button('NetNS',()=>terminal(item,'netns')));
        if((item.application||'smithproxy')==='smithproxy')actions.append(button('CLI',()=>terminal(item,'cli')),button('GDB',()=>terminal(item,'gdb')));
      }
      for(const [key,path] of [['logs','logs'],['diag','diagnostics']]) actions.append(button(t(key),()=>inspectRequest(item,path)));
      if(['stopped','expired','failed'].includes(item.state)) actions.append(actionButton(resource,'delete',item,true));
    }
    if(resource==='profiles') {actions.append(button(t('spawn'),()=>workflow.startInstance(item)),button(t('edit'),()=>openEditor(resource,item)),actionButton(resource,'delete',item,true));if(item.newer_build_available&&item.newer_build_id)actions.append(button(t('useNewer')+' · '+item.newer_build_id.slice(0,12),()=>openEditor(resource,{...item,build_id:item.newer_build_id})));}
    const usage=usagePanel(item,{el,language:lang,instances:!['binaries','tuntom'].includes(resource)});if(usage)actions.append(usage);
    if(resource==='wiring' && !(item.endpoints || []).length) actions.append(actionButton(resource,'delete',item,true));
    workflow.details(resource,item,actions);
  }
  text(view.inspector.querySelector('pre'),JSON.stringify(item,null,2));
  let extra=view.inspector.querySelector('.library-extra');
  const extraData=JSON.stringify([resource==='certificates'?item.certificates:null,item.newer_build_available,item.newest_commit_id,lang]);
  if(!extra){extra=el('section',{class:'library-extra'});view.inspector.append(extra);}
  if(extra.dataset.signature!==extraData){extra.dataset.signature=extraData;extra.replaceChildren();
    if(['binaries','tuntom'].includes(resource)&&item.newer_build_available)extra.append(el('p',{class:'warning'},`⚠ ${t('newerAvailable')} · ${item.newest_commit_id?.slice(0,12)}`));
    if(resource==='certificates'){
      extra.append(el('h3',{},t('certContents')));
      for(const cert of item.certificates||[]){const hash=button(cert.sha256||'SHA-256',()=>copyValue(cert.sha256||'').then(()=>notice('✓')).catch(e=>notice(e.message,true)));
        extra.append(el('article',{class:'certificate-entry'},el('strong',{},cert.name||cert.filename),el('code',{},[cert.filename,cert.key_filename].filter(Boolean).join(' + ')),el('small',{},`${cert.kind} · ${cert.subject||''}${cert.has_private_key?' · '+t('privateKey'):''}`),el('small',{},`${cert.notbefore||'—'} → ${cert.notafter||'—'}`),hash));
      }
    }
  }
  const summary=view.inspector.querySelector('.summary-fields');
  const fields=['state','application','source_ip','namespace','pid','build_id','config_id','filesystem_mode','rootfs_variant','deadline','created_at','built_at','commit_id','ref','build_type','path','work_dir'];
  if(resource==='test-drives')fields.push('ingress_ip','ingress_interface','ingress_host_ip','ingress_host_interface','egress_ip','egress_interface','host_ip','host_interface','workspace','config_path','binary_path','config_mode');
  for(const key of fields){
    const value=item[key];let row=summary.querySelector(`[data-field="${key}"]`);
    if(value===undefined||value===null||value===''){row?.remove();continue;}
    if(!row){const copy=button('',()=>copyValue(String(copy.dataset.value)).then(()=>notice('✓')).catch(e=>notice(e.message,true)));row=el('div',{'data-field':key},el('dt',{},t(key)),el('dd',{},copy));summary.append(row);}
    const copy=row.querySelector('button');copy.dataset.value=String(value);text(copy,value);
  }
}
async function inspectRequest(item,kind) {
  const view=views.get('instances');if(!view)return;view.inspectionKind=kind;const key=item.id+'/'+kind;if(view.inspectionFlight===key)return;view.inspectionFlight=key;
  try {const data=await request(`/api/instances/${encodeURIComponent(item.id)}/${kind}`); if(view.selected!==item.id||view.inspectionKind!==kind||selection())return;
    let output=view.inspector.querySelector('.inspection-output');if(output&&output.dataset.kind!==kind){output.remove();output=null;}
    if(!output){output=el(kind==='logs'?'pre':'div',{class:'inspection-output','data-kind':kind});view.inspector.append(output);}
    if(kind==='diagnostics')renderDiagnostics(output,data,{el,button,copy:value=>copyValue(value).then(()=>notice('✓')).catch(e=>notice(e.message,true)),language:lang});
    else text(output,typeof data.output==='string'?data.output:typeof data.log==='string'?data.log:JSON.stringify(data,null,2));
  } catch(error){if(view.selected===item.id)notice(error.message,true);}finally{if(view.inspectionFlight===key)view.inspectionFlight=null;}
}
function taskUpdate(tasks) {
  workflow.tasks(tasks);
  if(noticeTask){const task=tasks.find(task=>task.task_id===noticeTask);if(task&&['succeeded','failed','cancelled'].includes(task.state))notice(`${task.state==='succeeded'?t('done'):task.state} · ${task.label||task.kind}${task.error?' · '+task.error:''}`,task.state!=='succeeded');}
  if(editorPending){const task=tasks.find(task=>task.task_id===editorPending.id);if(task&&['succeeded','failed','cancelled'].includes(task.state)){const pending=editorPending;editorPending=null;if(pending.generation===editorGeneration){$('editor-save').disabled=false;text($('editor-error'),task.state==='succeeded'?t('done'):task.error||task.state);if(task.state==='succeeded'&&pending.revision===editorRevision)editorDirty=false;}}}
  const done=completed(selectedTaskStates,tasks); selectedTaskStates=new Map(tasks.map(task=>[task.task_id,task.state]));
  const active=tasks.filter(task=>['pending','running'].includes(task.state)); text($('task-count'),active.length); text($('task-summary'),active.length?t('active'):t('idle'));
  if(!selection()) {const signature=JSON.stringify(tasks.slice(0,30)); if(signature!==lastTaskRender){lastTaskRender=signature; const list=$('task-list');
    const old=new Map([...list.children].map(row=>[row.dataset.id,row]));
    const rows=tasks.slice(0,30).map(task=>{const row=old.get(task.task_id)||el('div',{'data-id':task.task_id,class:'task-row'},el('b'),el('span'),el('small'),button(t('result'),()=>workflow.result(task).catch(e=>notice(e.message,true)))); text(row.children[0],task.state);text(row.children[1],task.label);text(row.children[2],task.error || task.kind);row.children[3].hidden=task.state!=='succeeded';return row;});
    for(const row of [...list.children])if(!rows.includes(row))row.remove(); rows.forEach((row,n)=>{if(list.children[n]!==row)list.insertBefore(row,list.children[n]||null);});
  }}
  if(done){if(current!=='tasks')fetchItems(current).catch(()=>{});window.dispatchEvent(new Event('sas:task-completed'));}
}
function route() {
  const key=location.hash.slice(1).split('/')[0] || 'instances';
  current=groups.some(([,items])=>items.includes(key))?key:'instances';
  const view=views.get(current)||createView(current); for(const other of views.values())other.section.hidden=other!==view;
  const selected=location.hash.slice(1).split('/')[1];if(selected){try{view.selected=decodeURIComponent(selected);}catch{view.selected=null;}view.lastDetail=null;}
  navigation(); text($('page-title'),t(current));text($('breadcrumb'),t(current)); setMenu(false); paint(current); fetchItems(current).catch(()=>{});
}
const terminalToggle=button(t('terminals'),()=>terminals.reveal());terminalToggle.id='terminal-toggle';terminalToggle.hidden=true;document.querySelector('.topbar').append(terminalToggle);
const terminals=terminalWorkspace({root:$('terminal-dock'),el,button,csrf:()=>csrf,language:()=>lang,notice,onCount:count=>{terminalToggle.hidden=count===0;terminalToggle.dataset.count=count;text(terminalToggle,`${t('terminals')} ${count}`);}});
const terminal=(...args)=>terminals.open(...args);
$('task-toggle').onclick=()=>{const open=$('task-list').hidden;$('task-list').hidden=!open;$('task-toggle').setAttribute('aria-expanded',String(open));};
const menuViewport=matchMedia('(max-width:720px)');
const menuShade=button('',()=>setMenu(false,true),'menu-shade');menuShade.tabIndex=-1;menuShade.setAttribute('aria-hidden','true');document.body.append(menuShade);
$('menu-toggle').setAttribute('aria-controls','sidebar');
function setMenu(open,restoreFocus=false){
  const visible=menuViewport.matches&&open;
  document.body.classList.toggle('menu-open',visible);
  document.querySelector('.sidebar').inert=menuViewport.matches&&!visible;
  $('menu-toggle').setAttribute('aria-expanded',String(visible));
  menuShade.hidden=!visible;
  if(restoreFocus)$('menu-toggle').focus();
}
menuViewport.addEventListener('change',()=>setMenu(false));
$('menu-toggle').onclick=()=>setMenu(!document.body.classList.contains('menu-open'));
document.querySelector('.sidebar').addEventListener('click',event=>{if(event.target.closest('a[data-route],a.brand'))setMenu(false);});
$('refresh').onclick=()=>fetchItems(current).catch(error=>notice(error.message,true));
document.querySelectorAll('[data-language]').forEach(control=>control.onclick=async()=>{
  control.disabled=true;try{await request('/next-api/locale',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({locale:control.dataset.language})});}catch(error){notice(error.message,true);return;}finally{control.disabled=false;}
  lang=control.dataset.language;localStorage.setItem('sas-next-language',lang);document.documentElement.lang=lang;
  if(words.preview.includes($('notice').textContent))text($('notice'),t('preview'));
  terminals.relabel();
  const saved=new Map([...views].map(([key,view])=>[key,{query:view.search.value,selected:view.selected,inspectionKind:view.inspectionKind,scope:view.scope}]));
  for(const view of views.values())view.section.remove();views.clear();
  for(const [key,state]of saved){const view=createView(key);view.search.value=state.query;view.selected=state.selected;view.inspectionKind=state.inspectionKind;view.scope=state.scope;view.section.hidden=true;}
  lastTaskRender='';route();const tasks=cache.get('tasks');if(tasks)taskUpdate(tasks);
});
window.addEventListener('hashchange',route);
document.addEventListener('keydown',event=>{
  if(event.defaultPrevented||event.ctrlKey||event.metaKey||event.altKey||document.querySelector('dialog[open]'))return;
  if(event.target.closest?.('input,textarea,select,[contenteditable=true],.xterm'))return;
  if(event.key==='/'){event.preventDefault();views.get(current)?.search.focus();}
  if(event.key==='Escape'&&document.body.classList.contains('menu-open'))setMenu(false,true);
});
document.addEventListener('selectionchange',()=>{if(!selection()){paint(current); const tasks=cache.get('tasks');if(tasks)taskUpdate(tasks);}});
setInterval(()=>{if(!document.hidden){fetchItems(current).catch(()=>{});if(current!=='tasks')fetchItems('tasks').catch(()=>{});const view=views.get('instances');if(current==='instances'&&view?.inspectionKind&&!selection()){const item=cache.get('instances')?.find(i=>identity(i)===view.selected);if(item)inspectRequest(item,view.inspectionKind);}}},3500);
setInterval(()=>{if(!document.hidden&&['instances','test-drives','firewall'].includes(current))paint(current);},1000);

let editorSave=null, editorGeneration=0, editorRevision=0, editorDirty=false, editorPending=null;
$('editor').addEventListener('input',()=>{editorRevision++;editorDirty=true;});
function closeEditor(){if(editorDirty&&!confirm(t('dirty')))return;editorGeneration++;$('editor').close();}
$('editor').addEventListener('cancel',event=>{event.preventDefault();closeEditor();});
window.addEventListener('beforeunload',event=>{if(editorDirty&&$('editor').open){event.preventDefault();event.returnValue='';}});
function field(name,label,value='',type='text',choices=null) {
  label=fieldLabel(label,lang);
  const control=choices?el('select',{name}):el('input',{name,type});
  if(choices) for(const [id,title] of choices)control.append(el('option',{value:id},choiceLabel(id,title,lang)));
  if(choices&&value!==''&&!choices.some(([id])=>String(id)===String(value)))control.append(el('option',{value,disabled:''},'⚠ '+value));
  if(type==='checkbox')control.checked=Boolean(value);else if(type!=='file')control.value=value ?? '';
  const wrapper=el('label',{},label,control);$('editor-fields').append(wrapper); return control;
}
async function openEditor(resource,item=null) {
  const generation=++editorGeneration;
  editorDirty=false;editorRevision=0;editorPending=null;
  $('editor-fields').replaceChildren();text($('editor-title'),`${t(item?'edit':'add')} · ${t(resource)}`);text($('editor-error'),'');$('editor').showModal();
  $('editor-save').disabled=true; editorSave=null;
  try {
    field('name',t('title'),item?.name || '').required=true;
    if(item){const usage=usagePanel(item,{el,language:lang,newTab:true});if(usage)$('editor-fields').append(usage);}
    if(resource==='programs') {
      field('version',t('version'));field('path',t('path'));field('file',t('file'),'','file');
      editorSave=async form=>{const payload={name:form.name.value,version:form.version.value};const file=form.file.files[0];
        if(file){if(file.size>16*1024*1024)throw Error('Maximum 16 MiB');const data=new Uint8Array(await file.arrayBuffer());let binary='';for(let n=0;n<data.length;n+=8192)binary+=String.fromCharCode(...data.subarray(n,n+8192));payload.content_base64=btoa(binary);payload.filename=file.name;}else payload.path=form.path.value;
        return action(resource,'import','',payload);};
    } else if(resource==='wiring') {
      field('kind','Type','virtual-cable','text',[['virtual-cable','Cable · 2'],['virtual-switch','Switch']]);
      editorSave=form=>action(resource,'create','',{name:form.name.value,kind:form.kind.value});
    } else {
      const [artifacts,builds,configs,bundles,networks]=await Promise.all(['programs','binaries','configs','certificates','networks'].map(key=>fetchItems(key)));
      if(!$('editor').open || generation!==editorGeneration)return;
      const application=field('application',t('application'),item?.application || 'elf','text',['elf','router','webfsd','smithproxy'].map(key=>[key,key])); application.disabled=Boolean(item);
      const artifact=field('artifact',t('artifact'),item?.program_settings?.artifact_id || '', 'text',artifacts.map(a=>[a.artifact_id,`${a.name} · ${a.version} · ${a.artifact_id.slice(0,12)}`]));
      const argv=field('argv',t('args'),JSON.stringify(item?.program_settings?.argv || []));
      const port=field('port','HTTP port',item?.program_settings?.port || 8000,'number');
      const build=field('build','Smithproxy build',item?.build_id || '', 'text',buildChoices(builds,lang));
      const config=field('config',t('config'),item?.config_id || '', 'text',configs.filter(c=>c.native).map(c=>[c.config_id,c.name]));
      field('variant','Rootfs',item?.rootfs_variant || 'barebone','text',['barebone','utils','network'].map(key=>[key,key]));
      const filesystem=field('filesystem','Filesystem',item?.filesystem_mode || 'rootfs','text',[['rootfs','Rootfs'],['host','Host sandbox']]);
      const certificate=field('certificate',t('certificates'),item?.cert_bundle_id || '', 'text',[['','—'],...bundles.map(b=>[identity(b),b.name])]);
      const ingress=field('ingress','Ingress',item?.ingress_network_profile_id || '', 'text',[['','—'],...networks.filter(n=>n.kind==='ingress').map(n=>[identity(n),n.name])]);
      const egress=field('egress','Egress',item?.egress_network_profile_id || '', 'text',[['','—'],...networks.filter(n=>n.kind==='egress').map(n=>[identity(n),n.name])]);
      field('refresh_rootfs','Refresh rootfs',false,'checkbox');
      field('ttl',t('ttl'),item?.ttl_seconds ?? '', 'number');
      const restart=typeof item?.auto_restart==='object'?item.auto_restart:{on_failure:Boolean(item?.auto_restart)};
      field('exit',t('exit'),restart?.on_exit,'checkbox');field('failure',t('failure'),restart?.on_failure,'checkbox');
      const update=()=>{artifact.parentElement.hidden=argv.parentElement.hidden=application.value!=='elf';port.parentElement.hidden=application.value!=='webfsd';build.parentElement.hidden=config.parentElement.hidden=certificate.parentElement.hidden=ingress.parentElement.hidden=egress.parentElement.hidden=filesystem.parentElement.hidden=application.value!=='smithproxy';};application.onchange=update;update();
      editorSave=form=>{const app=application.value;const payload={...(item || {}),name:form.name.value,application:app,filesystem_mode:app==='smithproxy'?filesystem.value:'rootfs',rootfs_variant:form.variant.value,refresh_rootfs:form.refresh_rootfs.checked,cert_bundle_id:certificate.value,ingress_network_profile_id:ingress.value,egress_network_profile_id:egress.value,ttl_seconds:form.ttl.value===''?null:Number(form.ttl.value),auto_restart:{on_exit:form.exit.checked,on_failure:form.failure.checked}};
        if(app==='smithproxy'){payload.build_id=form.build.value;payload.config_id=form.config.value;}
        else {payload.build_id='';payload.config_id='';payload.program_settings=app==='elf'?{artifact_id:form.artifact.value,argv:JSON.parse(form.argv.value)}:app==='webfsd'?{port:Number(form.port.value)}:{};}
        return action(resource,item?'save':'create',item?identity(item):'',payload);};
    }
  }catch(error){text($('editor-error'),error.message);}finally{$('editor-save').disabled=!editorSave;}
}
$('editor-close').onclick=closeEditor;
$('editor-form').onsubmit=async event=>{event.preventDefault();if(!editorSave||editorPending)return;const submit=$('editor-save'),revision=editorRevision,generation=editorGeneration;submit.disabled=true;try{const result=await editorSave(event.currentTarget.elements);if(generation!==editorGeneration)return;if(result.task_id){editorPending={id:result.task_id,revision,generation};text($('editor-error'),t('queued'));}else{if(editorRevision===revision)editorDirty=false;text($('editor-error'),t('done'));}}catch(error){if(generation===editorGeneration)text($('editor-error'),error.message);}finally{if(generation===editorGeneration)submit.disabled=Boolean(editorPending);}};
const workflow=workflows({el,button,request,action,fetchItems,identity,notice,terminal,language:()=>lang,setCsrf:value=>{csrf=value;document.querySelector('meta[name="csrf-token"]').content=value;document.querySelectorAll('input[name="csrf_token"]').forEach(input=>input.value=value);}});
notice(t('preview')); route(); fetchItems('tasks').catch(()=>{});
const terminalQuery=new URLSearchParams(location.search);
if(terminalQuery.has('terminal')){
  const id=terminalQuery.get('terminal'),kind=terminalQuery.get('kind'),resource=terminalQuery.get('resource');
  const allowed=resource==='instances'?['cli','gdb','netns']:resource==='test-drives'?['cli','shell']:[];
  if(/^[A-Za-z0-9._-]{1,128}$/.test(id)&&allowed.includes(kind))fetchItems(resource).then(items=>{const item=items.find(i=>identity(i)===id);if(item){document.body.classList.add('terminal-window');terminal(item,kind,resource);}}).catch(e=>notice(e.message,true));
}
