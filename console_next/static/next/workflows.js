import {sortedBranches,buildChoices as artifactChoices} from './model.js';
import {networkWorkflows} from './network-workflows.js';
import {firewallWorkflows} from './firewall-workflows.js';
import {libraryWorkflows} from './library-workflows.js';
import {fieldLabel,choiceLabel} from './field-labels.js';
import {usagePanel} from './usage.js';
import {downloadDraft} from './draft-download.js';
import {renderFields,fieldSection} from './detail-fields.js';
import {copyText} from './feedback.js';
import {configSavePayload} from './config-save.js';
import {diffView} from './diff-view.js';
import {buildReferenceInfo} from './build-reference.js';
import {spawnSummary} from './spawn-summary.js';
import {lifetimeHint} from './spawn-lifetime.js';
import {instanceResultLinks} from './result-links.js';
// Stateful workflows: dialogs are not part of polled resource views.
export function workflows({el, button, request, action, fetchItems, identity, notice, terminal, language, setCsrf}) {
  const labels={
    openInstance:['Otevřít instanci','Open instance','Ouvrir l’instance'],
    queuedTask:['Zařazeno do fronty · otevřít task','Queued · open task','En file · ouvrir la tâche'],
    alias:['Přejmenovat instanci','Rename instance','Renommer l’instance'],
    aliasHelp:['Alias: 1–63 malých písmen, číslic a pomlček; začíná písmenem. Musí být unikátní. Prázdné pole alias odstraní. UUID se nemění.','Alias: 1–63 lowercase letters, digits and hyphens; starts with a letter. Must be unique. Empty removes the alias. UUID stays unchanged.','Alias : 1–63 lettres minuscules, chiffres et tirets ; commence par une lettre. Doit être unique. Vide supprime l’alias. L’UUID reste inchangé.'],
    build:['Build / Adopt','Build / Adopt','Compiler / Adopter'], fetch:['Fetch větví','Fetch branches','Actualiser les branches'],
    extract:['Adaptovat konfiguraci','Adapt config','Adapter la configuration'],rootfs:['Připravit rootfs','Prepare rootfs','Préparer rootfs'],
    remove:['Smazat','Delete','Supprimer'],cancel:['Zavřít','Close','Fermer'],submit:['Zařadit do fronty','Enqueue','Mettre en file'],
    extend:['Prodloužit','Extend','Prolonger'],cleanup:['Cleanup','Cleanup','Nettoyer'],seconds:['Přidat sekund','Additional seconds','Secondes supplémentaires'],
    result:['Výsledek','Result','Résultat'],confirm:['Opravdu pokračovat?','Continue?','Continuer ?'],
    unchanged:['Bez změn','No changes','Aucune modification'],baseline:['Výchozí konfigurace','Default configuration','Configuration par défaut'],normalized:['Normalizovaná konfigurace','Normalized configuration','Configuration normalisée'],
    edit:['Upravit config','Edit config','Modifier la configuration'],validate:['Validovat a zobrazit diff','Validate and show diff','Valider et afficher le diff'],
    copy:['Název kopie (prázdné = uložit původní)','Copy name (empty = replace original)','Nom de copie (vide = remplacer)'],
    saveIntent:['Způsob uložení','Save as','Mode d’enregistrement'],replaceOriginal:['Upravit původní','Update original','Modifier l’original'],createCopy:['Vytvořit kopii','Create a copy','Créer une copie'],copyName:['Název nové kopie','New copy name','Nom de la nouvelle copie'],
    validationSection:['Validace a uložení','Validation and save','Validation et enregistrement'],contentSection:['Obsah konfigurace','Configuration content','Contenu de configuration'],validatorLabel:['Validující build','Validation build','Binaire de validation'],chooseBuild:['Vyber validační build','Choose a validation build','Choisir un binaire de validation'],
    validator:['Vyber dostupný build pro validaci. Původní build může být smazaný.','Choose an available validation build. The original build may have been deleted.','Choisissez un binaire de validation disponible. Le binaire original a peut-être été supprimé.'],copyNameRequired:['Zadej název nové kopie.','Enter a name for the new copy.','Saisissez le nom de la nouvelle copie.'],nameRequired:['Zadej název konfigurace.','Enter a configuration name.','Saisissez un nom de configuration.'],
    approvalHint:['Validace vytvoří náhled. Konfigurace se uloží až po schválení diffu.','Validation creates a preview. The configuration is saved only after approving the diff.','La validation crée un aperçu. La configuration n’est enregistrée qu’après approbation du diff.'],
    editorFallback:['Pokročilý editor se nepodařilo načíst. Obsah můžeš upravit v textovém poli níže; validace funguje stejně.','The advanced editor could not load. Edit the text below; validation still works normally.','L’éditeur avancé n’a pas pu être chargé. Modifiez le texte ci-dessous ; la validation reste disponible.'],
    approve:['Schválit a uložit','Approve and save','Approuver et enregistrer'],reject:['Zamítnout','Reject','Rejeter'],
    saved:['Uloženo','Saved','Enregistré'],
    succeeded:['Dokončeno','Completed','Terminée'],failed:['Chyba','Failed','Échec'],cancelled:['Zrušeno','Cancelled','Annulée'],
    dirty:['Zahodit rozepsané změny?','Discard unsaved changes?','Abandonner les modifications ?'],
    unsaved:['Neuložené změny','Unsaved changes','Modifications non enregistrées'],
    draft:['Stáhnout rozepsané','Download draft','Télécharger le brouillon'],
    newSource:['Novější commit k dispozici','Newer commit available','Nouveau commit disponible'],
    builtCurrent:['Aktuální commit sestaven','Current commit built','Commit actuel compilé'],noBuild:['Zatím bez buildu','No build yet','Pas encore compilé'],
    fetched:['Poslední fetch','Last fetch','Dernière actualisation'],fetchFailed:['Fetch selhal','Fetch failed','Échec de l’actualisation'],
    refHelp:['Vyber známou větev, nebo napiš tag či commit ID. Nabídka odpovídá poslednímu fetchi.','Choose a known branch, or enter a tag or commit ID. Suggestions reflect the last fetch.','Choisissez une branche connue, ou saisissez un tag ou un commit. La liste reflète la dernière actualisation.'],
    manualRef:['Ruční ref — ověří se při buildu.','Manual ref — resolved when the build runs.','Référence manuelle — résolue lors de la compilation.'],
    refsUnavailable:['Nabídku větví se nepodařilo načíst; ref můžeš zadat ručně.','Branch suggestions could not load; you can still enter a ref manually.','La liste des branches est indisponible ; vous pouvez saisir une référence manuellement.'],
    name:['Název','Name','Nom'],ref:['Git větev / ref','Git branch / ref','Branche / référence Git'],
    validating:['Úloha běží; editor zůstává otevřený.','Task running; editor remains open.','Tâche en cours ; l’éditeur reste ouvert.'],
    refresh:['Obnovit','Refresh','Actualiser'],newInstance:['Spustit instanci','Start instance','Démarrer une instance'],
    newDrive:['Spustit Test Drive','Start Test Drive','Démarrer un Test Drive'],
    profile:['Runtime profil','Runtime profile','Profil d’exécution'],source:['Autorizovaná IP','Authorized IP','IP autorisée'],
    ttl:['TTL sekund (0 = bez limitu)','TTL seconds (0 = unlimited)','TTL secondes (0 = illimité)'],
    restart:['Restartovat','Restart','Redémarrer'],upgrade:['Upgradovat a restartovat','Upgrade and restart','Mettre à niveau et redémarrer'],
    snapshots:['Snapshoty','Snapshots','Snapshots'],snapshotName:['Název snapshotu','Snapshot name','Nom du snapshot'],forensic:['Forenzní live core','Forensic live core','Core live forensique'],restore:['Obnovit','Restore','Restaurer'],hot:['Hot — běžící kopie','Hot — live copy','Hot — copie active'],cold:['Cold — zmrazit hlavní proces','Cold — freeze main process','Cold — geler le processus principal'],stop:['Stop — zastavit vše','Stop — stop everything','Stop — tout arrêter'],sameBranch:['stejná větev','same branch','même branche'],component:['Komponenta','Component','Composant'],version:['Cílová verze','Target version','Version cible'],
    configMode:['Režim konfigurace','Configuration mode','Mode de configuration'],branches:['Větve a stav buildu','Branches and build status','Branches et état de compilation'],
    import:['Import configu','Import config','Importer une configuration'],download:['Stáhnout konfiguraci','Download config','Télécharger la configuration'],
    file:['Soubor · volitelně, nejvýše 1 MiB','File · optional, at most 1 MiB','Fichier · facultatif, 1 Mio maximum'],openEditor:['Otevřít editor','Open editor','Ouvrir l’éditeur'],metadata:['Název a popis','Name and description','Nom et description'],description:['Popis','Description','Description'],find:['Hledat','Find','Rechercher'],wrap:['Zalomit řádky','Wrap lines','Retour à la ligne'],
  };
  const tr=k=>(labels[k]||[k,k,k])[Math.max(0,['cs','en','fr'].indexOf(language()))];
  const pending=new Map();let dialogSerial=0;
  function dialog(title) {
    const titleId='workflow-title-'+(++dialogSerial),d=el('dialog',{class:'workflow-dialog','aria-labelledby':titleId}),body=el('div',{class:'workflow-fields'}),status=el('p',{role:'status'}),footer=el('footer');
    const badge=el('small',{class:'unsaved-indicator',hidden:''},tr('unsaved'));
    let dirty=false, revision=0;
    const touch=()=>{dirty=true;revision++;badge.hidden=false;d.dataset.dirty='true';};
    const close=()=>{if(dirty&&!confirm(tr('dirty')))return;d.close();d.remove();};
    const closer=button('×',close);closer.setAttribute('aria-label',tr('cancel'));closer.title=tr('cancel');
    d.append(el('header',{},el('h2',{id:titleId},title),badge,closer),body,status,footer);
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
      const response=await action(resource,command,typeof id==='function'?id():id,await payload());
      w.footer.querySelector('[data-task-result]')?.remove();
      if(response.task_id){
        w.status.replaceChildren(el('a',{href:'#tasks/'+encodeURIComponent(response.task_id),target:'_blank',rel:'noopener'},tr('queuedTask')));
        pending.set(response.task_id,{w,b,revision,preview:command==='preview'||command==='extract'});
      }
      else{if(w.revision()===revision)w.clean();w.status.textContent='✓ '+tr('succeeded');w.afterSuccess?.();b.disabled=false;}
    }catch(error){w.status.textContent=error.message;b.disabled=false;}},'primary');w.footer.append(b);return b;
  }
  const buildChoices=items=>artifactChoices(items,language());
  async function build(resource) {
    const w=dialog(tr('build')),ref=field(w,tr('ref'),'master'),type=field(w,'Build type','Release',[['Release','Release'],['Debug','Debug']]);
    const mode=resource==='binaries'?field(w,'Operation','adopt',[['adopt','Adopt'],['build','Build']]):null;
    const list=el('datalist',{id:'build-refs-'+dialogSerial}),info=el('p',{class:'build-reference-info',role:'status'}),fetchInfo=el('small',{class:'muted'});ref.setAttribute('list',list.id);ref.autocomplete='off';
    w.body.append(list,el('p',{class:'muted'},tr('refHelp')),info,fetchInfo);
    commit(w,resource,'build','',()=>({ref:ref.value,build_type:type.value,adopt:mode?.value==='adopt'}));
    let busy=false,status=null,refEdited=false;
    const update=()=>{if(!status)return;const value=buildReferenceInfo(status,ref.value,type.value);info.classList.toggle('source-update',Boolean(value&&!value.built));info.textContent=value?`${value.branch.name} · ${(value.branch.commit_id||'').slice(0,12)} · ${type.value} · ${tr(value.built?'builtCurrent':value.previous?'newSource':'noBuild')}`:tr('manualRef');};
    ref.addEventListener('input',()=>{refEdited=true;update();});type.addEventListener('change',update);
    const refresh=async()=>{if(busy)return;busy=true;try{
      status=await request(`/next-api/detail/${resource}/current/status`);if(!w.d.isConnected)return;
      list.replaceChildren();
      const branches=sortedBranches(status.refs?.branches||[],status.artifacts||[]);
      for(const branch of branches)list.append(el('option',{value:branch.name},`${(branch.commit_id||'').slice(0,12)}${branch.attic?' · Attic':''}`));
      if(!refEdited&&ref.value==='master'&&!branches.some(b=>b.name==='master'))ref.value=branches.find(b=>b.name===status.ref)?.name||branches[0]?.name||ref.value;
      fetchInfo.textContent=`${tr('fetched')}: ${status.refs?.refreshed_at?new Date(status.refs.refreshed_at).toLocaleString(language()):'—'}`;
      update();
    }catch{if(w.d.isConnected)info.textContent=tr('refsUnavailable');}finally{busy=false;}};
    const fetch=button(tr('fetch'),async()=>{fetch.disabled=true;try{await action(resource,'fetch');}catch(e){w.status.textContent=e.message;}finally{fetch.disabled=false;}});w.body.append(fetch);
    w.watch(refresh);await refresh();
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
      const description=item?null:field(w,tr('description'),original.description||'');
      fieldSection(w.body,tr('metadata'),[name,description].filter(Boolean),{el});
      const tools=el('div',{class:'toolbar'});for(const [id,label]of [['editor-find',tr('find')],['editor-wrap',tr('wrap')],['editor-font-down','A−'],['editor-font-up','A+']])tools.append(el('button',{type:'button',id},label));w.body.append(tools);
      const mount=el('div',{id:'config-editor'}),source=el('textarea',{id:'config-editor-content',hidden:''});source.value=original.content;
      tools.append(button(tr('draft'),()=>{try{downloadDraft(source.value,mode?.value==='copy'?copy.value:name.value);}catch(error){w.status.textContent=error.message;}}));
      const contentSection=el('section',{class:'editor-section'},el('h3',{},tr('contentSection')),tools,mount,source);w.body.append(contentSection);
      const validator=field(w,tr('validatorLabel'),original.normalized_build_id||'',[['',tr('chooseBuild')],...buildChoices(builds)]);
      const mode=item?field(w,tr('saveIntent'),'update',[['update',tr('replaceOriginal')],['copy',tr('createCopy')]]):null;
      const copy=item?field(w,tr('copyName')):null;
      const saveSection=fieldSection(w.body,tr('validationSection'),[validator,mode,copy].filter(Boolean),{el});
      saveSection.append(el('p',{class:'muted'},tr('approvalHint')));
      if(mode){const update=()=>{copy.parentElement.hidden=mode.value!=='copy';};mode.addEventListener('change',update);update();}
      const script=el('script',{src:'/static/vendor/codemirror/config-editor.js'});
      script.addEventListener('error',()=>{if(!w.d.isConnected)return;mount.hidden=true;source.hidden=false;source.rows=24;source.spellcheck=false;source.className='config-fallback';source.setAttribute('aria-label',tr('contentSection'));w.status.textContent=tr('editorFallback');for(const control of tools.querySelectorAll('button[id]'))control.disabled=true;});w.d.append(script);
      commit(w,'configs','preview','',()=>{try{return configSavePayload({original,id:item?identity(item):'',mode:mode?.value,name:name.value,copyName:copy?.value,content:source.value,buildId:validator.value,builds,description:description?.value});}catch(error){({validator,copyNameRequired:copy,nameRequired:name}[error.message])?.focus();throw Error(tr(error.message));}},tr('validate'));
      w.status.textContent='';
    }catch(error){w.status.textContent=error.message;}
  }
  async function result(task,source=null) {
    const data=await request(`/next-api/detail/tasks/${task.task_id}/result`);
    const w=dialog(tr('result'));
    for(const link of instanceResultLinks(data))w.body.append(el('a',{class:'download-link',href:link.href,target:'_blank',rel:'noopener',title:link.id},tr('openInstance')+' · '+link.label));
    function showDiff(value){diffView(w.body,value||'',{el,button,copy:copyText,language:language(),notice});}
    if(data.preview_id) {
      w.body.append(el('p',{},`${data.name || ''} · ${data.normalizer?.build_id || ''}`));
      showDiff(data.diff);
      commit(w,'configs','commit','',()=>({preview_id:data.preview_id,approved:true}),tr('approve'));
      w.afterSuccess=()=>{if(source?.w.d.isConnected&&source.w.revision()===source.revision){source.w.clean();source.w.status.textContent='✓ '+tr('saved');}};
      w.footer.append(button(tr('reject'),async()=>{try{await action('configs','cancel',data.preview_id);w.clean();w.d.close();w.d.remove();}catch(e){w.status.textContent=e.message;}}));
    } else if(typeof data.diff==='string') {
      showDiff(data.diff);
      for(const [key,label]of [['default_native','baseline'],['config_native','normalized']])if(typeof data[key]==='string')w.body.append(el('details',{},el('summary',{},tr(label)),el('pre',{},data[key])));
    } else {const fields=el('div',{class:'data-fields'});w.body.append(fields);renderFields(fields,data,{el,button,language:language(),copy:value=>copyText(value).then(()=>notice('✓')).catch(e=>notice(e.message,true))});}
  }
  function tasks(items) {
    for(const task of items){const entry=pending.get(task.task_id);if(!entry||!['succeeded','failed','cancelled'].includes(task.state))continue;
      pending.delete(task.task_id);const {w,b,preview}=entry;b.disabled=false;
      w.status.textContent=task.state==='succeeded'?'✓ '+tr(task.state):task.error||tr(task.state);
      w.status.append(el('span',{},' · '),el('a',{href:'#tasks/'+encodeURIComponent(task.task_id),target:'_blank',rel:'noopener'},task.task_id.slice(0,12)));
      if(task.state==='succeeded'&&!preview&&w.d.isConnected){const control=button(tr('result'),()=>result(task).catch(e=>w.status.textContent=e.message));control.dataset.taskResult=task.task_id;w.footer.append(control);}
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
      const ttlHint=el('small',{class:'muted'});ttl.parentElement.append(ttlHint);ttl.min='0';ttl.step='1';
      const group=(titles,controls)=>fieldSection(w.body,titles[Math.max(0,['cs','en','fr'].indexOf(language()))],controls,{el});
      group(['Program a konfigurace','Application and configuration','Application et configuration'],[p,build,config,fs]);
      group(['Identita a přístup','Identity and access','Identité et accès'],[source,user]);
      group(['Životní cyklus','Lifecycle','Cycle de vie'],[mode,ttl,persistent]);
      const profileSummary=el('fieldset',{class:'form-section'},el('legend',{},['Vybraný profil','Selected profile','Profil sélectionné'][Math.max(0,['cs','en','fr'].indexOf(language()))])),summaryFields=el('div',{class:'data-fields'});
      profileSummary.append(summaryFields);w.body.append(profileSummary);
      const dynamic=el('div',{class:'workflow-fields'});w.body.append(dynamic);let templates=[],runtime=[],readWiring=()=>undefined;
      function update(){
        const profile=profiles.find(item=>identity(item)===p.value),cfg=configs.find(c=>identity(c)===(profile?.config_id||config.value));
        ttl.disabled=Boolean(profile);ttlHint.textContent=lifetimeHint(profile,language());
        profileSummary.hidden=!profile;
        if(profile)renderFields(summaryFields,spawnSummary(profile,builds,configs,identity),{el,button,language:language(),copy:value=>copyText(value).catch(e=>notice(e.message,true))});
        source.disabled=Boolean(profile&&((profile.application||'smithproxy')!=='smithproxy'||['none','unlimited-veth'].includes(profile.network_drivers?.ingress)));
        source.parentElement.hidden=source.disabled;readWiring.inherit?.(profile?.wiring||[]);
        for(const c of [build,config,fs])c.parentElement.hidden=Boolean(profile);
        const previous=new Map([...templates,...runtime].map(([key,c])=>[key,c.value]));dynamic.replaceChildren();
        templates=(cfg?.placeholders||[]).map(key=>[key,field({body:dynamic},`{{${key}}}`,previous.get(key)||'')]);
        runtime=(profile?.network_start_parameters||[]).map(key=>[key,field({body:dynamic},key,previous.get(key)||'',key==='headless_endpoint_id'?[['','—'],...endpoints.filter(e=>e.state==='available').map(e=>[identity(e),e.name])]:null,key.includes('secret')?'password':'text')]);
      }
      p.onchange=config.onchange=update;update();
      readWiring=await network.bindings(w,profiles.find(profile=>identity(profile)===p.value)?.wiring||[],true);
      commit(w,'instances','create','',()=>({runtime_profile_id:p.value,...(!p.value?{build_id:build.value,config_id:config.value,filesystem_mode:fs.value}:{}),source_ip:source.value,user_id:user.value,config_mode:mode.value,persistent:persistent.value==='true',...(!p.value&&ttl.value!==''?{runtime_seconds:Number(ttl.value)}:{}),template_values:Object.fromEntries(templates.map(([k,c])=>[k,c.value])),network_runtime:Object.fromEntries(runtime.map(([k,c])=>[k,c.value])),...(readWiring()!==undefined?{wiring:readWiring()}:{}),parameters:{socks_port:1080,plaintext_port:50080,tls_port:50443,http_port:3128,cli_port:50000,workers:1,pcap_quota_mb:100}}));
    }catch(e){w.status.textContent=e.message;}
  }
  async function branches(resource) {
    const w=dialog(tr('branches')),summary=el('p'),active=el('div'),attic=el('details',{},el('summary',{},'Attic')),log=el('pre'),logs=el('details',{},el('summary',{},'Log'),log);
    w.body.append(summary,active,attic,logs);let signature='',busy=false,lastState='';
    const refresh=async()=>{if(busy||!w.d.isConnected||document.hidden||window.getSelection()?.toString())return;busy=true;try{
      const status=await request(`/next-api/detail/${resource}/current/status`);if(!w.d.isConnected||window.getSelection()?.toString())return;
      const title=`${status.state} · ${status.refs?.state||''} · ${tr('fetched')}: ${status.refs?.refreshed_at?new Date(status.refs.refreshed_at).toLocaleString(language()):'—'} · jobs ${status.jobs??'—'}`;if(summary.textContent!==title)summary.textContent=title;
      w.status.textContent=status.refs?.error||'';
      const output=String(status.log||status.error||'');if(log.textContent!==output){const tail=log.scrollHeight-log.scrollTop-log.clientHeight<40;log.textContent=output;if(tail)log.scrollTop=log.scrollHeight;}
      if(status.state==='failed'&&lastState!==status.state)logs.open=true;lastState=status.state;
      const next=JSON.stringify([status.refs?.branches,status.artifacts]);if(next===signature)return;signature=next;active.replaceChildren();for(const child of [...attic.children].slice(1))child.remove();
      for(const branch of sortedBranches(status.refs?.branches||[],status.artifacts||[])) {
        const row=el('div',{class:'branch-row'},el('strong',{},branch.name),el('code',{},(branch.commit_id||'').slice(0,12)),el('small',{},branch.commit_at||branch.commit_time||branch.committed_at||''));
        row.append(el('span',{class:branch.update_available?'source-update':'branch-build-state'},tr(branch.update_available?'newSource':branch.hasBuild?'builtCurrent':'noBuild')));
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
      bar.append(button(tr('alias'),()=>{const w=dialog(tr('alias'));w.body.append(el('p',{},tr('aliasHelp')),el('code',{},id));const alias=field(w,'Alias',item.alias||'');alias.maxLength=63;commit(w,resource,'alias',id,()=>{if(alias.value&&!/^[a-z][a-z0-9-]{0,62}$/.test(alias.value)){alias.focus();throw Error(tr('aliasHelp'));}return {alias:alias.value};});}));
      if(['running','starting','orphaned'].includes(item.state))bar.append(button(tr('extend'),()=>simple(resource,'extend',item,[['additional_seconds',tr('seconds'),1800,null,'number']])));
      if((item.application||'smithproxy')==='smithproxy'){
        bar.append(button(tr('extract'),()=>simple(resource,'extract',item,[['name',tr('name'),item.alias||id]])),el('a',{href:`/instances/${id}/config/download`,class:'download-link'},tr('download')));
        if(item.state==='running'&&(item.build_type==='Debug'||item.build_id?.endsWith('-debug')))bar.append(button('GDB server ▶',()=>run('debug-start')));
        if(item.debug_unit)bar.append(button('GDB server ■',()=>run('debug-stop')));
      }
      if(item.state==='running'){
        if((item.application||'smithproxy')==='smithproxy'||item.application==='elf'||item.tuntom_build_id)bar.append(button(tr('upgrade'),()=>upgradeInstance(item)));
        bar.append(button(tr('snapshots'),()=>snapshotManager(item)));
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
  async function upgradeInstance(item){
    const w=dialog(tr('upgrade')),components=[];
    if((item.application||'smithproxy')==='smithproxy')components.push(['smithproxy','Smithproxy']);
    if(item.application==='elf')components.push(['program','ELF program']);
    if(item.tuntom_build_id)components.push(['tuntom','Tuntom']);
    const component=field(w,tr('component'),components[0]?.[0]||'',components),version=field(w,tr('version'),'',[['','—']]);
    const catalogs={};
    const load=async()=>{
      const resource={smithproxy:'binaries',program:'programs',tuntom:'tuntom'}[component.value];
      const values=catalogs[resource]||(catalogs[resource]=await fetchItems(resource));
      const current={smithproxy:item.build_id,program:item.program_artifact_id,tuntom:item.tuntom_build_id}[component.value];
      const currentItem=values.find(v=>identity(v)===current),branch=currentItem?.ref,name=currentItem?.name;
      const ordered=[...values].sort((a,b)=>Number((b.ref===branch)||(b.name===name))-Number((a.ref===branch)||(a.name===name)));
      version.replaceChildren(...ordered.map(v=>{const same=(branch&&v.ref===branch)||(name&&v.name===name),label=[v.ref||v.name,v.build_type||v.version,identity(v).slice(0,12),same?'★ '+tr('sameBranch'):''].filter(Boolean).join(' · ');return el('option',{value:identity(v)},label);}));
      version.value=current||identity(ordered[0]||{});
    };
    component.onchange=()=>load().catch(e=>w.status.textContent=e.message);await load();
    commit(w,'instances','upgrade',identity(item),()=>({component:component.value,version_id:version.value}));
  }
  async function snapshotManager(item){
    const w=dialog(tr('snapshots')),name=field(w,tr('snapshotName'),''),mode=field(w,'Mode','cold',[['cold',tr('cold')],['hot',tr('hot')],['stop',tr('stop')]]),forensic=field(w,tr('forensic'),false,null,'checkbox'),list=el('div',{class:'snapshot-list'});w.body.append(list);
    const refresh=async()=>{const data=await request(`/next-api/detail/instances/${encodeURIComponent(identity(item))}/snapshots`);list.replaceChildren(...(data.snapshots||[]).map(s=>{const restore=button(tr('restore'),async()=>{restore.disabled=true;try{await action('instances','snapshot-restore',identity(item),{snapshot_id:s.snapshot_id});w.status.textContent='✓';}catch(e){w.status.textContent=e.message;}finally{restore.disabled=false;}}),drop=button(tr('remove'),async()=>{if(!confirm(`${tr('remove')} · ${s.name}?`))return;try{await action('instances','snapshot-drop',identity(item),{snapshot_id:s.snapshot_id});await refresh();}catch(e){w.status.textContent=e.message;}});return el('article',{class:'snapshot-entry'},el('strong',{},s.name),el('code',{},(s.snapshot_path||[]).join(' → ')),el('small',{},`${s.mode} · ${s.forensic_status} · ${s.created_at}`),restore,drop);}));};
    commit(w,'instances','snapshot-create',identity(item),()=>({name:name.value,mode:mode.value,forensic:forensic.checked}));w.watch(refresh);await refresh();
  }
  const network=networkWorkflows({el,button,request,action,fetchItems,identity,notice,dialog,field,commit,language});
  const firewall=firewallWorkflows({el,button,request,action,fetchItems,identity,notice,dialog,field,commit,language});
  const library=libraryWorkflows({el,button,request,action,fetchItems,identity,notice,dialog,field,commit,language,setCsrf});
  function sourceOverview(section,resource,toolbar){
    const root=el('section',{class:'source-overview'}),message=el('span'),open=button(tr('branches'),()=>branches(resource));root.append(message,open);toolbar.after(root);let busy=false;
    const refresh=async()=>{if(busy||section.hidden||document.hidden||window.getSelection()?.toString())return;busy=true;try{const status=await request(`/next-api/detail/${resource}/current/status`);if(!root.isConnected)return;
      const updates=(status.refs?.branches||[]).filter(b=>b.update_available);root.classList.toggle('has-updates',updates.length>0);
      const text=status.refs?.error?`${tr('fetchFailed')}: ${status.refs.error}`:updates.length?`${tr('newSource')}: ${updates.map(b=>`${b.name} → ${b.commit_id.slice(0,12)}`).join(' · ')}`:`${tr('fetched')}: ${status.refs?.refreshed_at?new Date(status.refs.refreshed_at).toLocaleString(language()):'—'} · ${status.refs?.state||'—'}`;
      if(message.textContent!==text)message.textContent=text;
    }catch(e){message.textContent=e.message;}finally{busy=false;}};
    const timer=setInterval(()=>{if(!root.isConnected){clearInterval(timer);window.removeEventListener('sas:task-completed',refresh);}else refresh();},5000);window.addEventListener('sas:task-completed',refresh);refresh();
  }
  return {toolbar,details,tasks,result,startInstance,profileBindings:network.bindings,profileFiles:library.files,firewallOverview:firewall.overview,sourceOverview};
}
