import {fieldLabel} from './field-labels.js';
import {fieldSection} from './detail-fields.js';
import {filterNetworks,networkUsageCount} from './address-inventory.js';
import {copyText} from './feedback.js';
import {endpointList} from './endpoint-list.js';
import {formatRoutes,parseRoutes} from './route-fields.js';
export function networkWorkflows({el,button,request,action,fetchItems,identity,notice,dialog,field,commit,language}) {
  const words={
    explain:['Jak to funguje?','How does this work?','Comment cela fonctionne ?'],
    authorizedHelp:['SAS vybere provoz podle autorizovaného zdroje a pošle jej přes veth do di0 této instance. Přístup stále omezuje firewall. Profil sám nepovoluje libovolnou zdrojovou IP.','SAS selects traffic by its authorized source and sends it through a veth to this instance’s di0. Firewall rules still constrain access. The profile does not authorize arbitrary source IPs.','SAS sélectionne le trafic selon sa source autorisée et le transmet par un veth vers di0. Le pare-feu limite toujours l’accès. Ce profil n’autorise pas toutes les IP sources.'],
    transportHelp:['Samostatný transportní namespace a routovaný veth dodají transport0. Tato cesta nepoužívá SAS source filtr; neznamená to změnu firewallu ani forwardingu celého hosta.','A separate transport namespace and routed veth provide transport0. This path does not use the SAS source filter; it does not change the host’s global firewall or forwarding settings.','Un namespace de transport séparé et un veth routé fournissent transport0. Ce chemin n’utilise pas le filtre source SAS et ne modifie pas le pare-feu ou le forwarding global de l’hôte.'],
    noLinkHelp:['SAS pro tuto stranu nevytvoří výchozí link ani egress routu. Rozhraní může dodat Wiring nebo mikroslužba. Zapojení a adresaci je potřeba nastavit výslovně.','SAS creates no default link or egress route for this side. Wiring or a microservice can supply the interface. Configure connections and addressing explicitly.','SAS ne crée pas de liaison par défaut ni de route de sortie pour ce côté. Wiring ou un microservice peut fournir l’interface. Configurez explicitement les connexions et l’adressage.'],
    natHelp:['Provoz opustí namespace přes do0 a host při odchodu přeloží zdrojovou IP (MASQUERADE). Protistrana uvidí adresu hosta.','Traffic leaves the namespace through do0; the host translates the source address on egress (MASQUERADE). The remote peer sees the host address.','Le trafic quitte le namespace via do0 ; l’hôte traduit l’adresse source en sortie (MASQUERADE). Le pair distant voit l’adresse de l’hôte.'],
    routedHelp:['Provoz opustí namespace přes do0 bez překladu zdrojové IP. Navazující síť musí znát zpáteční routu k adresám instance.','Traffic leaves through do0 without source-address translation. The upstream network must have a return route to the instance addresses.','Le trafic sort via do0 sans traduction d’adresse source. Le réseau amont doit disposer d’une route de retour vers les adresses de l’instance.'],
    retired:['Tento driver už runner nepodporuje. Vyber náhradu výslovně; konfiguraci neměníme automaticky.','This driver is retired. Select a replacement explicitly; configuration is not changed automatically.','Ce pilote est retiré. Choisissez explicitement son remplaçant ; la configuration ne change pas automatiquement.'],
    attach:['Připojit / rezervovat','Attach / reserve','Connecter / réserver'],detach:['Odpojit','Disconnect','Déconnecter'],
    addressing:['Adresace','Addressing','Adressage'],inventory:['Použité sítě','Used networks','Réseaux utilisés'],
    save:['Uložit adresaci','Save addressing','Enregistrer l’adressage'],check:['Ověřit překryvy','Check overlaps','Vérifier les chevauchements'],
    mode:['Správa adres','Address management','Gestion des adresses'],sas:['SAS managed','SAS managed','Géré par SAS'],
    guest:['Guest · jen evidence','Guest · declaration only','Invité · déclaration seule'],none:['Bez L3','No L3','Sans L3'],
    addresses:['IP adresy / prefixy (IPv4 + IPv6)','IP addresses / prefixes (IPv4 + IPv6)','Adresses IP / préfixes (IPv4 + IPv6)'],
    routes:['Routy: cílový prefix [brána] na řádek','Routes: destination prefix [gateway] per line','Routes : préfixe destination [passerelle] par ligne'],
    newProfile:['Nový síťový profil','New network profile','Nouveau profil réseau'],edit:['Upravit','Edit','Modifier'],
    name:['Název','Name','Nom'],description:['Popis','Description','Description'],confirm:['Opravdu odpojit?','Disconnect this endpoint?','Déconnecter ce point ?'],
    empty:['Žádné zaznamenané adresy','No recorded addresses','Aucune adresse enregistrée'],refresh:['Obnovit','Refresh','Actualiser'],
    searchNetworks:['Hledat adresu, kabel, rozhraní nebo instanci','Search address, cable, interface or instance','Rechercher adresse, câble, interface ou instance'],allNetworks:['IPv4 + IPv6','IPv4 + IPv6','IPv4 + IPv6'],expand:['Rozbalit vše','Expand all','Tout développer'],collapse:['Sbalit vše','Collapse all','Tout réduire'],uses:['použití','uses','utilisations'],
    help:['Změna adres automaticky přepne na SAS managed. Guest pouze eviduje, Bez L3 odstraní dříve spravované adresy.','Changing addresses selects SAS managed. Guest only records addresses; No L3 removes previously managed addresses.','Modifier les adresses active SAS managed. Guest ne fait que déclarer ; Sans L3 retire les adresses précédemment gérées.'],
    deleted:['Smazat','Delete','Supprimer'],selector:['Výběr provozu','Traffic selector','Sélection du trafic'],
    authorization:['Vyžadovat autorizaci','Require authorization','Exiger une autorisation'],
    noWarnings:['Bez překryvů','No overlaps','Aucun chevauchement'],
    pending:['Čeká na aplikování','Waiting to apply','En attente d’application'],applied:['Aplikováno','Applied','Appliqué'],unmanaged:['Nespravováno','Unmanaged','Non géré'],failed:['Chyba','Failed','Échec'],desired:['Požadováno','Desired','Demandé'],observed:['Ověřeno','Observed','Observé'],
    planned:['Tuto kombinaci lze uložit, ale současný runner ji zatím neumí spustit. Podporuje dual-stack a u autorizovaného ingressu výběr podle zdroje.','This combination can be saved, but the current runner cannot start it yet. It supports dual-stack and source selection for authorized ingress.','Cette combinaison peut être enregistrée, mais le runner ne peut pas encore la démarrer. Il prend en charge le double-stack et la sélection par source pour l’entrée autorisée.'],
  };
  const t=k=>(words[k]||[k,k,k])[Math.max(0,['cs','en','fr'].indexOf(language()))];
  const lines=value=>value.split(/\s+/).filter(Boolean);
  function area(w,label,value='') {
    label=fieldLabel(label,language());
    const c=el('textarea',{rows:'3','aria-label':label,spellcheck:'false'});c.value=value;
    w.body.append(el('label',{},label,c));return c;
  }
  async function inventory() {
    const w=dialog(t('inventory'));
    const search=el('input',{type:'search','aria-label':t('searchNetworks'),placeholder:t('searchNetworks')}),family=el('select',{'aria-label':'IP'},...['all','4','6'].map(v=>el('option',{value:v},v==='all'?t('allNetworks'):'IPv'+v))),count=el('span',{class:'muted'}),treeRoot=el('div');
    let data=[],busy=false,signature='',filtered=false;const expanded=new Map();
    const remember=()=>{if(!filtered)for(const d of treeRoot.querySelectorAll('details[data-prefix]'))expanded.set(d.dataset.prefix,d.open);};
    function draw(){
      remember();filtered=Boolean(search.value.trim());const visible=filterNetworks(data,search.value,family.value);
      function tree(nodes){return el('ul',{class:'address-tree'},...nodes.map(n=>{
        const detail=el('details',{'data-prefix':n.prefix},el('summary',{},`${n.prefix} · ${networkUsageCount([n])} ${t('uses')}`));detail.open=search.value.trim()?true:expanded.get(n.prefix)??true;
        for(const u of n.usages||[]){
          const address=button(u.address||n.prefix,()=>copyText(u.address||n.prefix).then(()=>notice('✓')).catch(e=>notice(e.message,true)));
          const row=el('div',{class:'inventory-usage'+(u.managed?'':' muted')},address,el('span',{},u.interface||'—'));
          if(u.segment_id)row.append(button(u.segment_name||u.segment_id,()=>endpoints({segment_id:u.segment_id,name:u.segment_name||u.segment_id})));
          if(u.instance_id)row.append(el('a',{href:'#instances/'+encodeURIComponent(u.instance_id)},u.instance_id));
          row.querySelector('a')?.addEventListener('click',()=>{w.clean();w.d.close();w.d.remove();});detail.append(row);
        }
        if(n.children?.length)detail.append(tree(n.children));return el('li',{},detail);
      }));}
      treeRoot.replaceChildren(visible.length?tree(visible):el('p',{class:'muted'},t('empty')));count.textContent=`${networkUsageCount(visible)} / ${networkUsageCount(data)} ${t('uses')}`;
    }
    search.addEventListener('input',event=>{event.stopPropagation();draw();});family.addEventListener('input',event=>event.stopPropagation());family.addEventListener('change',draw);
    const toolbar=el('div',{class:'toolbar inventory-toolbar'},search,family,count);w.body.append(toolbar,treeRoot);
    for(const [key,open]of [['expand',true],['collapse',false]])toolbar.append(button(t(key),()=>{for(const d of treeRoot.querySelectorAll('details')){d.open=open;expanded.set(d.dataset.prefix,open);}}));
    const refresh=async()=>{if(busy)return;busy=true;try{const value=await request('/next-api/detail/wiring/current/inventory');if(!w.d.isConnected)return;const next=JSON.stringify(value.tree||[]);if(next!==signature){signature=next;data=value.tree||[];draw();}w.status.textContent='';}catch(e){w.status.textContent=e.message;}finally{busy=false;}};
    w.watch(refresh);w.footer.append(button(t('refresh'),refresh));await refresh();
  }
  async function attach(segment) {
    const w=dialog(t('attach'));
    try{const instances=await fetchItems('instances');if(!w.d.isConnected)return;
      const available=instances.filter(i=>i.state!=='orphaned');
      const instance=field(w,'Instance',identity(available[0]||{}),available.map(i=>[identity(i),`${i.alias||identity(i)} · ${i.state}`]));
      const port=field(w,'Interface','cable0');port.maxLength=15;port.pattern='[A-Za-z][A-Za-z0-9_-]{0,14}';
      commit(w,'wiring','attach',identity(segment),()=>({instance_id:instance.value,interface:port.value}));
    }catch(e){w.status.textContent=e.message;}
  }
  function addressing(segment,ep) {
    const w=dialog(`${t('addressing')} · ${ep.interface}`),desired=ep.addressing?.desired||{};
    const mode=field(w,t('mode'),desired.mode||'none',[['sas',t('sas')],['guest',t('guest')],['none',t('none')]]);
    const addresses=area(w,t('addresses'),(desired.addresses||[]).join('\n'));
    const routes=area(w,t('routes'),(desired.routes||[]).map(r=>`${r.destination} ${r.gateway||''}`).join('\n'));
    w.body.append(el('p',{class:'muted'},t('help')));
    for(const input of [addresses,routes])input.addEventListener('input',()=>{mode.value='sas';});
    const warnings=el('div',{class:'address-warnings',role:'status'});w.body.append(warnings);let timer,generation=0;
    const preview=async()=>{const current=++generation;try{
      const data=await request('/next-api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({resource:'wiring',action:'check-addressing',payload:{addresses:lines(addresses.value),segment_id:identity(segment),endpoint_id:ep.id}})});
      if(!w.d.isConnected||current!==generation)return;
      warnings.replaceChildren(...(data.warnings.length?data.warnings.map(warning=>{
        const usage=warning.usage||{};
        const row=el('p',{},`⚠ ${warning.requested} ↔ ${usage.prefix||usage.address||''} · ${usage.interface||''} · ${usage.instance_id||''}`);
        if(usage.segment_id)row.append(button(usage.segment_name||usage.segment_id,()=>endpoints({segment_id:usage.segment_id,name:usage.segment_name||usage.segment_id})));
        return row;
      }):[el('p',{class:'muted'},t('noWarnings'))]));
    }catch(e){if(current===generation&&w.d.isConnected)warnings.textContent=e.message;}};
    addresses.addEventListener('input',()=>{clearTimeout(timer);generation++;timer=setTimeout(preview,400);});
    w.d.addEventListener('close',()=>{clearTimeout(timer);generation++;},{once:true});
    w.footer.append(button(t('check'),preview));preview();
    commit(w,'wiring','addressing',identity(segment),()=>({endpoint_id:ep.id,mode:mode.value,addresses:lines(addresses.value),routes:routes.value.split('\n').filter(l=>l.trim()).map(l=>{
      const parts=l.trim().split(/\s+/);if(parts.length>2)throw Error(t('routes'));return {destination:parts[0],gateway:parts[1]||''};
    })}),t('save'));
  }
  async function endpoints(segment) {
    const w=dialog(segment.name);let busy=false,signature='';
    const grid=el('div',{class:'endpoint-grid'});w.body.append(grid);let latest=segment;
    const render=endpointList(grid,{el,button,t,onAddress:addressing,onDetach:async(current,ep)=>{if(!confirm(t('confirm')))return;try{await action('wiring','detach',identity(current),{endpoint_id:ep.id});w.status.textContent='…';}catch(e){w.status.textContent=e.message;}}});
    const attachButton=button(t('attach'),()=>attach(latest));attachButton.hidden=true;
    const refresh=async()=>{if(busy||!w.d.isConnected||window.getSelection()?.toString())return;busy=true;try{const current=await request(`/next-api/detail/wiring/${identity(segment)}/segment`);if(!w.d.isConnected||window.getSelection()?.toString())return;
      const next=JSON.stringify(current);if(next===signature)return;signature=next;
      latest=current;render(current);attachButton.hidden=current.kind!=='virtual-switch'&&(current.endpoints||[]).length>=2;
    }catch(e){w.status.textContent=e.message;}finally{busy=false;}};
    w.footer.append(button(t('refresh'),refresh),attachButton);
    const timer=setInterval(refresh,3500);w.d.addEventListener('close',()=>clearInterval(timer),{once:true});w.watch(refresh);await refresh();
  }
  function profile(item=null) {
    const w=dialog(item?t('edit'):t('newProfile'));
    const name=field(w,t('name'),item?.name||''),description=field(w,t('description'),item?.description||'');
    const kind=field(w,'Ingress / Egress',item?.kind||'ingress',[['ingress','Ingress'],['egress','Egress']]);kind.disabled=Boolean(item);
    const driver=field(w,'Driver','',[]),family=field(w,'IP','dual',[['dual','IPv4 + IPv6'],['ipv4','IPv4'],['ipv6','IPv6']]);family.value=item?.address_family||'dual';
    const selector=field(w,t('selector'),item?.selector||'source',[['source','Source IP'],['destination','Destination CIDR'],['source-destination','Source + destination']]);
    const authorization=field(w,t('authorization'),String(item?.require_authorization??true),[['true','✓'],['false','—']]);
    const destinations=area(w,'Destination CIDRs',(item?.destination_cidrs||[]).join('\n'));
    const mode=field(w,'Egress mode',item?.mode||'masquerade',[['masquerade','Masquerade'],['routed','Routed']]);
    const host=field(w,'Host interface',item?.host_interface||'');
    const group=(titles,controls)=>fieldSection(w.body,titles[Math.max(0,['cs','en','fr'].indexOf(language()))],controls,{el});
    group(['Identita profilu','Profile identity','Identité du profil'],[name,description]);
    group(['Propojení a driver','Link and driver','Liaison et pilote'],[kind,driver,family]);
    const selectionGroup=group(['Výběr provozu a autorizace','Traffic selection and authorization','Sélection du trafic et autorisation'],[selector,authorization,destinations]);
    const egressGroup=group(['Odchozí cesta','Outbound path','Chemin de sortie'],[mode,host]);
    const diagram=el('pre',{class:'network-diagram'}),explanation=el('p'),help=el('details',{class:'diagram-help'},el('summary',{},'? '+t('explain')),explanation),illustration=el('div',{class:'network-illustration'},diagram,help);w.body.append(illustration);
    let helpTimer,pinned=false;
    help.querySelector('summary').addEventListener('click',event=>{event.preventDefault();pinned=!pinned;help.open=pinned;});
    illustration.addEventListener('mouseenter',()=>{clearTimeout(helpTimer);helpTimer=setTimeout(()=>help.open=true,2000);});
    illustration.addEventListener('mouseleave',()=>{clearTimeout(helpTimer);if(!pinned)help.open=false;});
    w.d.addEventListener('close',()=>clearTimeout(helpTimer),{once:true});
    const support=el('p',{class:'error',role:'status'});w.body.append(support);
    function update(){const ing=kind.value==='ingress';const authorized=driver.value==='authorized-veth';
      explanation.textContent=t(driver.value==='none'?'noLinkHelp':ing?(authorized?'authorizedHelp':'transportHelp'):mode.value==='masquerade'?'natHelp':'routedHelp');
      authorization.disabled=authorized;if(authorized)authorization.value='true';
      support.textContent=family.value!=='dual'||ing&&authorized&&selector.value!=='source'?t('planned'):'';
      selector.parentElement.hidden=authorization.parentElement.hidden=destinations.parentElement.hidden=!ing||!authorized;
      mode.parentElement.hidden=host.parentElement.hidden=ing||driver.value==='none';
      selectionGroup.hidden=!ing||!authorized;egressGroup.hidden=ing||driver.value==='none';
      diagram.textContent=driver.value==='none'?`${kind.value}: ∅\n  Wiring → namespace`:
        ing?(authorized?`Source / destination\n   │ ${selector.value}\n   ▼\nHost veth ──── di0 [namespace]`:`Host veth ──── transport0 [namespace]`):`[namespace] do0 ──── Host\n                       │ ${mode.value}\n                       ▼\n                    Upstream`;
    }
    function options(){const values=kind.value==='ingress'?['authorized-veth','unlimited-veth','none']:['veth-out','none'];driver.replaceChildren(...values.map(v=>el('option',{value:v},v)));if(item?.driver&&!values.includes(item.driver)){driver.prepend(el('option',{value:'',disabled:''},item.driver+' · retired'));driver.value='';w.status.textContent=t('retired');}else driver.value=item?.driver||values[0];update();}
    kind.onchange=options;driver.onchange=selector.onchange=mode.onchange=family.onchange=update;options();
    commit(w,'networks',item?'save':'create',identity(item||{}),()=>{if(!driver.value)throw Error(t('retired'));return {name:name.value,description:description.value,kind:kind.value,driver:driver.value,address_family:family.value,selector:selector.value,require_authorization:authorization.value==='true',destination_cidrs:lines(destinations.value),mode:mode.value,host_interface:host.value,interface_name:driver.value==='none'?'':driver.value==='unlimited-veth'?'transport0':kind.value==='ingress'?'di0':'do0'};});
  }
  async function bindings(w,existing=[],spawn=false) {
    const segments=await fetchItems('wiring');if(!w.d.isConnected)return ()=>undefined;
    const panel=el('details',{},el('summary',{},'Wiring')),rows=el('div');w.body.append(panel);
    const override=spawn?field({body:panel},'Override Wiring','false',[['false','Profile'],['true','Override']]):null;
    panel.append(rows);const entries=[];
    function add(binding={}){
      if(entries.filter(e=>e.box.isConnected).length>=16)return;
      const box=el('fieldset');rows.append(box);const nested={body:box};
      const choices=[['','—'],...segments.map(s=>[identity(s),`${s.name} · ${s.endpoints.length}/${s.kind==='virtual-cable'?2:'∞'}`])];
      if(binding.segment_id&&!choices.some(([id])=>id===binding.segment_id))choices.push([binding.segment_id,'⚠ '+binding.segment_id]);
      const segment=field(nested,'Cable / switch',binding.segment_id||'',choices),port=field(nested,'Interface',binding.interface||`cable${entries.length}`);
      let mode=null,addresses=null,routes=null;
      if(spawn){mode=field(nested,t('mode'),binding.addressing?.mode||'none',[['none',t('none')],['sas',t('sas')],['guest',t('guest')]]);addresses=area(nested,t('addresses'),(binding.addressing?.addresses||[]).join('\n'));routes=area(nested,t('routes'),formatRoutes(binding.addressing?.routes));for(const input of [addresses,routes])input.oninput=()=>mode.value='sas';}
      box.append(button('×',()=>{box.remove();w.touch?.();}));entries.push({box,segment,port,mode,addresses,routes});update();
    }
    const addButton=button('+ '+fieldLabel('Cable / switch',language()),()=>{add();w.touch?.();});panel.append(addButton);
    function update(){const disabled=override&&override.value!=='true';for(const c of rows.querySelectorAll('input,select,textarea,button'))c.disabled=disabled;addButton.disabled=disabled;}
    if(override)override.onchange=update;for(const binding of existing)add(binding);update();
    const read=()=>{
      if(override&&override.value!=='true')return undefined;
      return entries.filter(e=>e.box.isConnected).map(e=>({segment_id:e.segment.value,interface:e.port.value,...(spawn?{addressing:{mode:e.mode.value,addresses:lines(e.addresses.value),routes:parseRoutes(e.routes.value,t('routes'))}}:{})}));
    };
    read.inherit=next=>{if(override&&override.value!=='true'){rows.replaceChildren();entries.length=0;for(const binding of next)add(binding);update();}};
    return read;
  }
  function toolbar(resource,bar){if(resource==='wiring')bar.append(button(t('inventory'),inventory));if(resource==='networks')bar.append(button(t('newProfile'),()=>profile()));}
  function details(resource,item,bar){
    if(resource==='wiring')bar.append(button(t('addressing'),()=>endpoints(item)));
    if(resource==='networks')bar.append(button(t('edit'),()=>profile(item)),button(t('deleted'),()=>{if(confirm(t('deleted')+'?'))action('networks','delete',identity(item)).catch(e=>notice(e.message,true));}));
  }
  return {toolbar,details,bindings};
}
