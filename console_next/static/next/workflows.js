import {sortedBranches,buildChoices as artifactChoices} from './model.js';
import {networkWorkflows} from './network-workflows.js';
import {firewallWorkflows} from './firewall-workflows.js';
import {libraryWorkflows} from './library-workflows.js';
import {fieldLabel,choiceLabel} from './field-labels.js';
import {usagePanel} from './usage.js';
import {downloadDraft} from './draft-download.js';
// Stateful workflows: dialogs are not part of polled resource views.
export function workflows({el, button, request, action, fetchItems, identity, notice, terminal, language, setCsrf}) {
  const labels={
    build:['Build / Adopt','Build / Adopt','Compiler / Adopter'], fetch:['Fetch větví','Fetch branches','Actualiser les branches'],
    extract:['Extrahovat config','Extract config','Extraire la configuration'],rootfs:['Připravit rootfs','Prepare rootfs','Préparer rootfs'],
    remove:['Smazat','Delete','Supprimer'],cancel:['Zavřít','Close','Fermer'],submit:['Zařadit do fronty','Enqueue','Mettre en file'],
    extend:['Prodloužit','Extend','Prolonger'],cleanup:['Cleanup','Cleanup','Nettoyer'],seconds:['Přidat sekund','Additional seconds','Secondes supplémentaires'],
    result:['Výsledek','Result','Résultat'],confirm:['Opravdu pokračovat?','Continue?','Continuer ?'],
    unchanged:['Bez změn','No changes','Aucune modification'],baseline:['Výchozí konfigurace','Default configuration','Configuration par défaut'],normalized:['Normalizovaná konfigurace','Normalized configuration','Configuration normalisée'],
    edit:['Upravit config','Edit config','Modifier la configuration'],validate:['Validovat a zobrazit diff','Validate and show diff','Valider et afficher le diff'],
    copy:['Název kopie (prázdné = uložit původní)','Copy name (empty = replace original)','Nom de copie (vide = remplacer)'],
    approve:['Schválit a uložit','Approve and save','Approuver et enregistrer'],reject:['Zamítnout','Reject','Rejeter'],
    saved:['Uloženo','Saved','Enregistré'],
    succeeded:['Dokončeno','Completed','Terminée'],failed:['Chyba','Failed','Échec'],cancelled:['Zrušeno','Cancelled','Annulée'],
    dirty:['Zahodit rozepsané změny?','Discard unsaved changes?','Abandonner les modifications ?'],
    unsaved:['Neuložené změny','Unsaved changes','Modifications non enregistrées'],
    draft:['Stáhnout rozepsané','Download draft','Télécharger le brouillon'],
    name:['Název','Name','Nom'],ref:['Git větev / ref','Git branch / ref','Branche / référence Git'],
    validating:['Úloha běží; editor zůstává otevřený.','Task running; editor remains open.','Tâche en cours ; l’éditeur reste ouvert.'],
    refresh:['Obnovit','Refresh','Actualiser'],newInstance:['Spustit instanci','Start instance','Démarrer une instance'],
    newDrive:['Spustit Test Drive','Start Test Drive','Démarrer un Test Drive'],
    profile:['Runtime profil','Runtime profile','Profil d’exécution'],source:['Autorizovaná IP','Authorized IP','IP autorisée'],
    ttl:['TTL sekund (0 = bez limitu)','TTL seconds (0 = unlimited)','TTL secondes (0 = illimité)'],
    restart:['Restartovat','Restart','Redémarrer'],upgrade:['Upgrade bez restartu','Upgrade without restart','Mettre à niveau sans redémarrer'],
    configMode:['Režim konfigurace','Configuration mode','Mode de configuration'],branches:['Větve a stav buildu','Branches and build status','Branches et état de compilation'],
    import:['Import configu','Import config','Importer une configuration'],download:['Stáhnout','Download','Télécharger'],
    file:['Soubor · volitelně, nejvýše 1 MiB','File · optional, at most 1 MiB','Fichier · facultatif, 1 Mio maximum'],openEditor:['Otevřít editor','Open editor','Ouvrir l’éditeur'],metadata:['Název a popis','Name and description','Nom et description'],description:['Popis','Description','Description'],find:['Hledat','Find','Rechercher'],wrap:['Zalomit řádky','Wrap lines','Retour à la ligne'],
  };
  const tr=k=>(labels[k]||[k,k,k])[Math.max(0,['cs','en','fr'].indexOf(language()))];
  const pending=new Map();
  function dialog(title) {
    const d=el('dialog',{class:'workflow-dialog'}),body=el('div',{class:'workflow-fields'}),status=el('p',{role:'status'}),footer=el('footer');
    const badge=el('small',{class:'unsaved-indicator',hidden:''},tr('unsaved'));
    let dirty=false, revision=0;
    const touch=()=>{dirty=true;revision++;badge.hidden=false;d.dataset.dirty='true';};
    const close=()=>{if(dirty&&!confirm(tr('dirty')))return;d.close();d.remove();};
    d.append(el('header',{},el('h2',{},title),badge,button('×',close)),body,status,footer);
    d.addEventListener('input',touch);d.addEventListener('cancel',event=>{event.preventDefault();close();});
    document.body.append(d);d.showModal();
    d.addEventListener('close',()=>window.dispatchEvent(new Event('sas:dialog-closed')),{once:true});
    const watch=refresh=>{const update=()=>{if(d.isConnected&&!window.getSelection()?.toString())refresh();};window.addEventListener('sas:task-completed',update);d.addEventListener('close',()=>window.removeEventListener('sas:task-completed',update),{once:true});};
    const unload=event=>{if(d.isConnected&&dirty){event.preventDefault();event.returnValue='';}};window.addEventListener('beforeunload',unload);d.addEventListener('close',()=>window.removeEventListener('beforeunload',unload),{once:true});
    return {d,body,status,footer,revision:()=>revision,clean:()=>{dirty=false;badge.hidden=true;d.dataset.dirty='false';},touch,watch};
  }
  function field(w,name,value='',choices=null,type='text') {
    name=fieldLabel(name,language());
    const c=choices?el('select'):el('input',{type});
    if(choices)for(const [id,label]of choices)c.append(el('option',{value:id},choiceLabel(id,label,language())));
    if(choices&&value!==''&&!choices.some(([id])=>String(id)===String(value)))c.append(el('option',{value,disabled:''},'⚠ '+value));
    c.value=value;c.setAttribute('aria-label',name);w.body.append(el('label',{},name,c));
    if(w.d)queueMicrotask(()=>{if(w.d.open&&document.activeElement===w.d.querySelector('header button'))w.body.querySelector('input:not([disabled]),select:not([disabled]),textarea:not([disabled])')?.focus();});
    return c;
  }
  function commit(w,resource,command,id,payload,label=tr('submit')) {
    const b=button(label,async()=>{b.disabled=true;try{
      const revision=w.revision();
      const result=await action(resource,command,typeof id==='function'?id():id,await payload());w.status.textContent=tr('validating');
      if(result.task_id)pending.set(result.task_id,{w,b,revision,preview:command==='preview'||command==='extract'});
      else{if(w.revision()===revision)w.clean();w.status.textContent=JSON.stringify(result);w.afterSuccess?.();b.disabled=false;}
    }catch(error){w.status.textContent=error.message;b.disabled=false;}},'primary');w.footer.append(b);return b;
  }
  const buildChoices=items=>artifactChoices(items,language());
  async function build(resource) {
    const w=dialog(tr('build')),ref=field(w,tr('ref'),'master'),type=field(w,'Build type','Release',[['Release','Release'],['Debug','Debug']]);
    const mode=resource==='binaries'?field(w,'Operation','adopt',[['adopt','Adopt'],['build','Build']]):null;
    commit(w,resource,'build','',()=>({ref:ref.value,build_type:type.value,adopt:mode?.value==='adopt'}));
  }
  async function simple(resource,command,item,fields=[]) {
    const w=dialog(tr(command));const controls=fields.map(([key,label,value,choices,type])=>[key,field(w,label,value,choices,type)]);
    commit(w,resource,command,identity(item||{}),()=>Object.fromEntries(controls.map(([k,c])=>[k,c.type==='number'?Number(c.value):c.value])));
  }
  async function startDrive(selected=null){
    const w=dialog(tr('newDrive'));
    try{const builds=await fetchItems('binaries');if(!w.d.isConnected)return;
      const build=field(w,'Build',identity(selected||builds[0]||{}),buildChoices(builds));
      const ttl=field(w,tr('ttl'),0,null,'number'),mode=field(w,tr('configMode'),'rw',[['ro','RO'],['rw','RW']]);
      commit(w,'test-drives','create','',()=>({build_id:build.value,ttl_seconds:Number(ttl.value),config_mode:mode.value}));
    }catch(e){w.status.textContent=e.message;}
  }
  function configImport(){
    const w=dialog(tr('import')),file=field(w,tr('file'),'',null,'file');
    w.footer.append(button(tr('openEditor'),async()=>{try{const selected=file.files[0];if(selected?.size>1024*1024)throw Error('Maximum 1 MiB');const content=selected?new TextDecoder('utf-8',{fatal:true}).decode(await selected.arrayBuffer()):'';w.clean();w.d.close();w.d.remove();configEditor(null,{name:selected?.name||'',content});}catch(e){w.status.textContent=e.message;}}));
  }
  async function configEditor(item=null,imported=null) {
    const w=dialog(tr(item?'edit':'import'));w.d.classList.add('config-workflow');w.status.textContent='…';
    try {
      const [original,builds]=await Promise.all([item?request(`/next-api/detail/configs/${identity(item)}/content`):Promise.resolve(imported||{name:'',content:''}),fetchItems('binaries')]);
      if(!w.d.isConnected)return;
      // The existing bundled CodeMirror initializes these IDs on load.
      if(document.getElementById('config-editor'))throw Error('A configuration editor is already open');
      const name=field(w,tr('name'),original.name);if(item)name.disabled=true;
      const usage=usagePanel(original,{el,language:language(),newTab:true});if(usage)w.body.append(usage);
      const copy=item?field(w,tr('copy')):null;
      const tools=el('div',{class:'toolbar'});for(const [id,label]of [['editor-find',tr('find')],['editor-wrap',tr('wrap')],['editor-font-down','A−'],['editor-font-up','A+']])tools.append(el('button',{type:'button',id},label));w.body.append(tools);
      const mount=el('div',{id:'config-editor'}),source=el('textarea',{id:'config-editor-content',hidden:''});source.value=original.content;
      tools.append(button(tr('draft'),()=>{try{downloadDraft(source.value,copy?.value||name.value);}catch(error){w.status.textContent=error.message;}}));
      w.body.append(mount,source);
      const validator=field(w,'Build',original.normalized_build_id||identity(builds[0]||{}),buildChoices(builds));
      const script=el('script',{src:'/static/vendor/codemirror/config-editor.js'});w.d.append(script);
      commit(w,'configs','preview','',()=>({name:copy?.value||name.value,content:source.value,build_id:validator.value,description:original.description||'',profile:original.profile||'custom',action:item&&!copy.value?'update':'create',config_id:item&&!copy.value?identity(item):''}),tr('validate'));
      w.status.textContent='';
    }catch(error){w.status.textContent=error.message;}
  }
  async function result(task,source=null) {
    const data=await request(`/next-api/detail/tasks/${task.task_id}/result`);
    const w=dialog(tr('result'));
    function showDiff(value){const diff=el('pre',{class:'native-diff'});for(const line of (value||tr('unchanged')).split('\n'))diff.append(el('div',{class:line.startsWith('+')?'added':line.startsWith('-')?'removed':''},line||' '));w.body.append(diff);}
    if(data.preview_id) {
      w.body.append(el('p',{},`${data.name || ''} · ${data.normalizer?.build_id || ''}`));
      showDiff(data.diff);
      commit(w,'configs','commit','',()=>({preview_id:data.preview_id,approved:true}),tr('approve'));
      w.afterSuccess=()=>{if(source?.w.d.isConnected&&source.w.revision()===source.revision){source.w.clean();source.w.status.textContent='✓ '+tr('saved');}};
      w.footer.append(button(tr('reject'),async()=>{try{await action('configs','cancel',data.preview_id);w.clean();w.d.close();w.d.remove();}catch(e){w.status.textContent=e.message;}}));
    } else if(typeof data.diff==='string') {
      showDiff(data.diff);
      for(const [key,label]of [['default_native','baseline'],['config_native','normalized']])if(typeof data[key]==='string')w.body.append(el('details',{},el('summary',{},tr(label)),el('pre',{},data[key])));
    } else w.body.append(el('pre',{class:'result-json'},JSON.stringify(data,null,2)));
  }
  function tasks(items) {
    for(const task of items){const entry=pending.get(task.task_id);if(!entry||!['succeeded','failed','cancelled'].includes(task.state))continue;
      pending.delete(task.task_id);const {w,b,preview}=entry;b.disabled=false;
      w.status.textContent=task.state==='succeeded'?'✓ '+tr(task.state):task.error||tr(task.state);
      if(task.state==='succeeded'){if(w.revision()===entry.revision&&!preview)w.clean();w.afterSuccess?.();if(preview&&w.d.isConnected)result(task,{w,revision:entry.revision}).catch(e=>w.status.textContent=e.message);}
    }
  }
  async function startInstance(selected=null) {
    const w=dialog(tr('newInstance'));
    try{const [profiles,builds,configs,sources,endpoints]=await Promise.all(['profiles','binaries','configs','sources','endpoints'].map(fetchItems));if(!w.d.isConnected)return;
      const p=field(w,tr('profile'),identity(selected||profiles.find(p=>p.available!==false)||{}),[['','Standalone'],...profiles.map(p=>[identity(p),p.name+(p.available===false?' · unavailable':'')])]);
      for(const option of p.options)if(profiles.find(profile=>identity(profile)===option.value)?.available===false)option.disabled=true;
      const build=field(w,'Build',identity(builds[0]||{}),buildChoices(builds));
      const config=field(w,'Config',identity(configs[0]||{}),configs.filter(c=>c.native).map(c=>[identity(c),c.name]));
      const fs=field(w,'Filesystem','rootfs',[['rootfs','Rootfs'],['host','Host sandbox']]);
      const source=field(w,tr('source'),'', [['','—'],...sources.map(s=>[s.ip,`${s.ip}${s.available?'':' · active'}`])]),user=field(w,'User ID','admin-console');
      for(const option of source.options)if(sources.find(s=>s.ip===option.value)?.available===false)option.disabled=true;
      const mode=field(w,tr('configMode'),'ro',[['ro','RO'],['rw','RW']]),ttl=field(w,tr('ttl'),'',null,'number'),persistent=field(w,'Persistent','false',[['false','—'],['true','✓']]);
      const dynamic=el('div',{class:'workflow-fields'});w.body.append(dynamic);let templates=[],runtime=[],readWiring=()=>undefined;
      function update(){
        const profile=profiles.find(item=>identity(item)===p.value),cfg=configs.find(c=>identity(c)===(profile?.config_id||config.value));
        source.disabled=Boolean(profile&&((profile.application||'smithproxy')!=='smithproxy'||['none','unlimited-veth'].includes(profile.network_drivers?.ingress)));
        source.parentElement.hidden=source.disabled;readWiring.inherit?.(profile?.wiring||[]);
        for(const c of [build,config,fs])c.parentElement.hidden=Boolean(profile);
        const previous=new Map([...templates,...runtime].map(([key,c])=>[key,c.value]));dynamic.replaceChildren();
        templates=(cfg?.placeholders||[]).map(key=>[key,field({body:dynamic},`{{${key}}}`,previous.get(key)||'')]);
        runtime=(profile?.network_start_parameters||[]).map(key=>[key,field({body:dynamic},key,previous.get(key)||'',key==='headless_endpoint_id'?[['','—'],...endpoints.filter(e=>e.state==='available').map(e=>[identity(e),e.name])]:null,key.includes('secret')?'password':'text')]);
      }
      p.onchange=config.onchange=update;update();
      readWiring=await network.bindings(w,profiles.find(profile=>identity(profile)===p.value)?.wiring||[],true);
      commit(w,'instances','create','',()=>({runtime_profile_id:p.value,...(!p.value?{build_id:build.value,config_id:config.value,filesystem_mode:fs.value}:{}),source_ip:source.value,user_id:user.value,config_mode:mode.value,persistent:persistent.value==='true',...(ttl.value!==''?{runtime_seconds:Number(ttl.value)}:{}),template_values:Object.fromEntries(templates.map(([k,c])=>[k,c.value])),network_runtime:Object.fromEntries(runtime.map(([k,c])=>[k,c.value])),...(readWiring()!==undefined?{wiring:readWiring()}:{}),parameters:{socks_port:1080,plaintext_port:50080,tls_port:50443,http_port:3128,cli_port:50000,workers:1,pcap_quota_mb:100}}));
    }catch(e){w.status.textContent=e.message;}
  }
  async function branches(resource) {
    const w=dialog(tr('branches')),summary=el('p'),active=el('div'),attic=el('details',{},el('summary',{},'Attic')),log=el('pre'),logs=el('details',{},el('summary',{},'Log'),log);
    w.body.append(summary,active,attic,logs);let signature='',busy=false,lastState='';
    const refresh=async()=>{if(busy||!w.d.isConnected||document.hidden||window.getSelection()?.toString())return;busy=true;try{
      const status=await request(`/next-api/detail/${resource}/current/status`);if(!w.d.isConnected||window.getSelection()?.toString())return;
      const title=`${status.state} · ${status.refs?.state||''} · jobs ${status.jobs??'—'}`;if(summary.textContent!==title)summary.textContent=title;
      const output=String(status.log||status.error||'');if(log.textContent!==output){const tail=log.scrollHeight-log.scrollTop-log.clientHeight<40;log.textContent=output;if(tail)log.scrollTop=log.scrollHeight;}
      if(status.state==='failed'&&lastState!==status.state)logs.open=true;lastState=status.state;
      const next=JSON.stringify([status.refs?.branches,status.artifacts]);if(next===signature)return;signature=next;active.replaceChildren();for(const child of [...attic.children].slice(1))child.remove();
      for(const branch of sortedBranches(status.refs?.branches||[],status.artifacts||[])) {
        const row=el('div',{class:'branch-row'},el('strong',{},branch.name),el('code',{},(branch.commit_id||'').slice(0,12)),el('small',{},branch.commit_at||branch.commit_time||branch.committed_at||''));
        const hasRelease=(status.artifacts||[]).some(b=>b.ref===branch.name&&b.commit_id===branch.commit_id&&b.build_type==='Release');
        if(branch.hasBuild&&!hasRelease)row.append(button('Build Release',async()=>{try{await action(resource,'build','',{ref:branch.name,build_type:'Release',adopt:true});}catch(e){w.status.textContent=e.message;}}));
        (branch.attic?attic:active).append(row);
      }
    }catch(e){w.status.textContent=e.message;}finally{busy=false;}};
    const timer=setInterval(refresh,3500);w.d.addEventListener('close',()=>clearInterval(timer),{once:true});
    w.watch(refresh);w.footer.append(button(tr('refresh'),refresh));await refresh();
  }
  function toolbar(resource,bar) {
    network.toolbar(resource,bar);
    firewall.toolbar(resource,bar);
    library.toolbar(resource,bar);
    if(['binaries','tuntom'].includes(resource)){bar.append(button(tr('build'),()=>build(resource)),button(tr('fetch'),()=>action(resource,'fetch').catch(e=>notice(e.message,true))),button(tr('branches'),()=>branches(resource)));}
    if(resource==='instances')bar.append(button(tr('newInstance'),()=>startInstance()),button(tr('cleanup'),()=>{if(confirm(tr('confirm')))action(resource,'cleanup').catch(e=>notice(e.message,true));}));
    if(resource==='configs')bar.append(button(tr('import'),configImport));
    if(resource==='test-drives')bar.append(button(tr('newDrive'),()=>startDrive()));
  }
  function details(resource,item,bar) {
    network.details(resource,item,bar);
    firewall.details(resource,item,bar);
    library.details(resource,item,bar);
    const id=identity(item),run=(cmd,payload={})=>action(resource,cmd,id,payload).catch(e=>notice(e.message,true));
    if(['binaries','tuntom'].includes(resource)) {
      bar.append(button(tr('remove'),()=>{if(confirm(`${tr('remove')} · ${item.ref||item.name||id}\nID: ${id}\n\n${tr('confirm')}`))run('delete');}));
      if(resource==='binaries')bar.append(button(tr('extract'),()=>{const w=dialog(tr('extract'));commit(w,resource,'extract',id,()=>({}));}),button(tr('rootfs'),()=>run('rootfs')),button('Test Drive',()=>startDrive(item)));
    }
    if(resource==='instances'){
      if(['running','starting','orphaned'].includes(item.state))bar.append(button(tr('extend'),()=>simple(resource,'extend',item,[['additional_seconds',tr('seconds'),1800,null,'number']])));
      if((item.application||'smithproxy')==='smithproxy'){
        bar.append(button(tr('extract'),()=>simple(resource,'extract',item,[['name',tr('name'),item.alias||id]])),el('a',{href:`/instances/${id}/config/download`,class:'download-link'},tr('download')));
        if(item.state==='running'&&(item.build_type==='Debug'||item.build_id?.endsWith('-debug')))bar.append(button('GDB server ▶',()=>run('debug-start')));
        if(item.debug_unit)bar.append(button('GDB server ■',()=>run('debug-stop')));
      }
    }
    if(resource==='configs')bar.append(button(tr('edit'),()=>configEditor(item)),button(tr('metadata'),()=>simple(resource,'metadata',item,[['name',tr('name'),item.name],['description',tr('description'),item.description||'']])),el('a',{href:`/configs/${id}/download`,class:'download-link'},tr('download')),button(tr('remove'),()=>{if(confirm(`${tr('remove')} · ${item.name||id}\nID: ${id}\n\n${tr('confirm')}`))run('delete');}));
    if(resource==='tasks'&&item.state==='succeeded')bar.append(button(tr('result'),()=>result(item).catch(e=>notice(e.message,true))));
    if(resource==='test-drives'){
      bar.append(
        button(tr('restart'),()=>run('restart')),
        button(tr('extend'),()=>simple(resource,'extend',item,[['additional_seconds',tr('seconds'),1800,null,'number']])),
        button(tr('configMode'),()=>simple(resource,'config-mode',item,[['config_mode',tr('configMode'),item.config_mode||'ro',[['ro','RO'],['rw','RW']]]])),
        button(tr('extract'),()=>simple(resource,'extract',item,[['name',tr('name'),id]])),
        button(tr('upgrade'),async()=>{try{const builds=await fetchItems('binaries');simple(resource,'upgrade',item,[['build_id','Build',item.build_id,buildChoices(builds)]]);}catch(e){notice(e.message,true);}}),
        button(tr('remove'),()=>{if(confirm(`${tr('remove')} · Test Drive ${id}\n\n${tr('confirm')}`))run('delete');})
      );
      if(item.state==='running')bar.append(button('CLI',()=>terminal(item,'cli','test-drives')),button('Shell',()=>terminal(item,'shell','test-drives')));
    }
  }
  const network=networkWorkflows({el,button,request,action,fetchItems,identity,notice,dialog,field,commit,language});
  const firewall=firewallWorkflows({el,button,request,action,fetchItems,identity,notice,dialog,field,commit,language});
  const library=libraryWorkflows({el,button,request,action,fetchItems,identity,notice,dialog,field,commit,language,setCsrf});
  return {toolbar,details,tasks,result,startInstance,profileBindings:network.bindings,profileFiles:library.files};
}
