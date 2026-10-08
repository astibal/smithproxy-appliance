export function firewallWorkflows({el,button,request,action,fetchItems,identity,notice,dialog,field,commit,language}) {
  const words={
    add:['Autorizovat IP','Authorize IP','Autoriser une IP'],edit:['Upravit','Edit','Modifier'],remove:['Odebrat','Remove','Retirer'],
    policy:['Globální politika','Global policy','Politique globale'],settings:['Síťová nastavení','Networking settings','Paramètres réseau'],
    ttl:['TTL sekund · prázdné = neomezeně','TTL seconds · empty = unlimited','TTL secondes · vide = illimité'],
    seconds:['Přidat sekund','Additional seconds','Secondes supplémentaires'],extend:['Prodloužit / oživit','Extend / renew','Prolonger / renouveler'],
    confirm:['Potvrdit změnu síťové politiky?','Confirm network policy change?','Confirmer la modification de politique réseau ?'],
    register:['Zařadit do source poolu','Register in source pool','Inscrire dans le pool source'],
    source:['Zdrojová IP / CIDR','Source IP / CIDR','IP / CIDR source'],destination:['Cílová IP / CIDR (volitelné)','Destination IP / CIDR (optional)','IP / CIDR destination (facultatif)'],
    attach:['Přiřadit další instanci','Assign another instance','Associer une autre instance'],
    newInstance:['Nebo spustit z profilu','Or start from profile','Ou démarrer depuis un profil'],
    saved:['Uložit','Save','Enregistrer'],yes:['Ano','Yes','Oui'],no:['Ne','No','Non'],
    topology:['Topologie přístupu','Access topology','Topologie d’accès'],effect:['Pouze indikace egress efektu','Egress effect indication only','Indication de l’effet de sortie uniquement'],
  };
  const t=k=>(words[k]||[k,k,k])[Math.max(0,['cs','en','fr'].indexOf(language()))];
  const yesno=[['true',t('yes')],['false',t('no')]];
  async function authorize() {
    const w=dialog(t('add'));
    try{
      const [instances,profiles]=await Promise.all([fetchItems('instances'),fetchItems('profiles')]);if(!w.d.isConnected)return;
      const source=field(w,t('source')),label=field(w,'Label'),system=field(w,'System','admin-console');
      const input=field(w,'INPUT','true',yesno),forward=field(w,'FORWARD','true',yesno);
      const protocol=field(w,'Protocol','any',['any','tcp','udp'].map(v=>[v,v]));
      const destination=field(w,t('destination')),ports=field(w,'Ports'),ttl=field(w,t('ttl'),'',null,'number');
      const pool=field(w,t('register'),'false',yesno);
      const instance=field(w,'Instance','',[['','—'],...instances.filter(i=>i.state==='running').map(i=>[identity(i),i.alias||identity(i)])]);
      const profile=field(w,t('newInstance'),'',[['','—'],...profiles.map(p=>[identity(p),p.name])]);
      instance.onchange=()=>{if(instance.value)profile.value='';};profile.onchange=()=>{if(profile.value)instance.value='';};
      const user=field(w,'User ID','admin-console');
      commit(w,'firewall','create','',()=>({source:source.value,label:label.value,system:system.value,chains:[...(input.value==='true'?['input']:[]),...(forward.value==='true'?['forward']:[])],protocol:protocol.value,destination:destination.value,ports:ports.value,...(ttl.value?{ttl_seconds:Number(ttl.value)}:{}),register_source:pool.value==='true',instance_id:instance.value,runtime_profile_id:profile.value,user_id:user.value}));
    }catch(e){w.status.textContent=e.message;}
  }
  async function policy() {
    const w=dialog(t('policy'));try{const state=await request('/next-api/detail/firewall/current/state');if(!w.d.isConnected)return;
      const input=field(w,'INPUT enforcement',String(state.input_enforced),yesno),forward=field(w,'FORWARD enforcement',String(state.forward_enforced),yesno);
      commit(w,'firewall','settings','',()=>{if(!confirm(t('confirm')))throw Error('Cancelled');return {input_enforced:input.value==='true',forward_enforced:forward.value==='true'};});
    }catch(e){w.status.textContent=e.message;}
  }
  function renew(item) {
    const w=dialog(t('extend')),seconds=field(w,t('seconds'),1800,null,'number');
    commit(w,'firewall','extend',identity(item),()=>({additional_seconds:Number(seconds.value)}));
  }
  async function attach(item) {
    const w=dialog(t('attach'));try{const instances=await fetchItems('instances');if(!w.d.isConnected)return;
      const available=instances.filter(i=>i.state==='running'&&!(item.instances||[]).some(bound=>identity(bound)===identity(i)));
      w.body.append(el('code',{},item.source));
      const select=field(w,'Instance',identity(available[0]||{}),available.map(i=>[identity(i),i.alias||identity(i)]));
      const submit=commit(w,'firewall','attach',()=>select.value,()=>({source:item.source}));if(submit)submit.disabled=!available.length;
    }catch(e){w.status.textContent=e.message;}
  }
  async function settings() {
    const w=dialog(t('settings'));try{const data=await request('/next-api/detail/settings/current/networking');if(!w.d.isConnected)return;
      const fields={};
      for(const key of ['ingress_cidr','ingress_cidr_v6','namespace_cidr','namespace_cidr_v6','fabric_cidr','fabric_cidr_v6','fabric_interface','sas_interface','sas_route_via','sas_route_via_v6','route_table_start','mark_start'])fields[key]=field(w,key.replaceAll('_',' '),data[key]??'');
      fields.fabric_link_mode=field(w,'Fabric link mode',data.fabric_link_mode||'ipvlan-l3',[['ipvlan-l3','IPvlan L3'],['ipvlan-l2','IPvlan L2']]);
      fields.egress_mode=field(w,'Egress mode',data.egress_mode||'masquerade',[['masquerade','Masquerade'],['routed','Routed']]);
      const sources=el('textarea',{rows:'3'});sources.value=(data.authorized_source_ips||[]).join('\n');w.body.append(el('label',{},t('source'),sources));
      commit(w,'settings','save','',()=>{if(!confirm(t('confirm')))throw Error('Cancelled');return {...Object.fromEntries(Object.entries(fields).map(([k,c])=>[k,['mark_start','route_table_start'].includes(k)?Number(c.value):c.value])),allocation_prefix:30,allocation_prefix_v6:126,authorized_source_ips:sources.value.split(/\s+/).filter(Boolean)};});
    }catch(e){w.status.textContent=e.message;}
  }
  async function topology() {
    const w=dialog(t('topology'));w.d.classList.add('topology-dialog');
    let signature='',busy=false;
    const refresh=async()=>{
      if(busy||!w.d.isConnected||window.getSelection()?.toString())return;busy=true;
      try{const state=await request('/next-api/detail/firewall/current/state');if(!w.d.isConnected||window.getSelection()?.toString())return;
        const value=JSON.stringify([state.topology,state.egress_groups,state.input_enforced,state.forward_enforced]);if(value===signature)return;signature=value;
        const groups=new Map();
        for(const entry of state.topology||[]){const key=JSON.stringify([entry.input_allowed,entry.forward_allowed,entry.instances,entry.selectors]);if(!groups.has(key))groups.set(key,[]);groups.get(key).push(entry);}
        const cards=[];
        for(const entries of groups.values()){
          const entry=entries[0],sources=el('div',{class:'topology-sources'});
          for(const source of entries){const block=el('div',{},el('code',{},source.source));
            for(const expiry of source.expirations||[])block.append(el('small',{'data-expiry':expiry.expires_at},expiry.expires_at));
            block.append(el('small',{},(source.systems||[]).join(', ')));sources.append(block);
          }
          const lanes=el('div',{class:'topology-lanes'});
          for(const [chain,allowed,enforced]of [['INPUT',entry.input_allowed,state.input_enforced],['FORWARD',entry.forward_allowed,state.forward_enforced]]){
            const selected=(entry.selectors||[]).some(s=>(s.chains||[]).includes(chain.toLowerCase()));
            const effective=!enforced||allowed,gate=`${chain} ${!enforced?'AUDIT':!allowed?'DROP':selected?'SELECTED':'ALLOW'}`;
            const targets=el('div',{class:'topology-targets'});
            if(chain==='INPUT'||!effective)targets.append(el('div',{},effective?'SAS HOST':'BLOCKED'));
            else if(!(entry.instances||[]).length)targets.append(el('div',{},'NO LIVE ROUTE'));
            else for(const instance of entry.instances){
              const link=el('a',{href:'#instances/'+encodeURIComponent(identity(instance)),class:'topology-instance'},`${instance.namespace} · ${instance.state}`,el('code',{},`${instance.network?.guest_ip||''} ${instance.network?.guest_ip_v6||''} / ${instance.network?.guest_interface||''}`));
              link.addEventListener('click',()=>{w.d.close();w.d.remove();});targets.append(link);
            }
            if(chain==='FORWARD'&&effective){const controls=el('div',{class:'topology-attach'});for(const source of entries){const add=button('+ '+source.source,()=>attach(source));add.title=t('attach');add.setAttribute('aria-label',t('attach')+' · '+source.source);controls.append(add);}targets.append(controls);}
            lanes.append(el('div',{class:`topology-lane ${effective?'allowed':'denied'}`},el('code',{},gate),el('span',{},'→'),targets));
          }
          for(const selector of entry.selectors||[])lanes.append(el('small',{},`${(selector.chains||[]).join('+')} · ${selector.protocol} → ${selector.destination||'*'} ${(selector.ports||[]).join(',')}`));
          cards.push(el('article',{class:'topology-group'},sources,lanes));
        }
        for(const group of state.egress_groups||[])cards.push(el('article',{class:'topology-egress'},el('div',{},...(group.instances||[]).map(i=>el('code',{},`${i.namespace} · ${i.guest_ip} ${i.guest_ip_v6||''}`))),el('span',{},'→'),el('div',{},el('strong',{},`${group.mode} · ${group.interface||'host route'}`),el('em',{},t('effect')))));
        w.body.replaceChildren(...cards);w.status.textContent=new Date().toLocaleTimeString();
      }catch(e){w.status.textContent=e.message;}finally{busy=false;}
    };
    const timer=setInterval(refresh,5000),clock=setInterval(()=>{if(window.getSelection()?.toString())return;for(const c of w.body.querySelectorAll('[data-expiry]')){const seconds=Math.max(0,Math.ceil((Date.parse(c.dataset.expiry)-Date.now())/1000));c.textContent=`⏱ ${Math.floor(seconds/3600)}:${String(Math.floor(seconds/60)%60).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;}},1000);
    w.d.addEventListener('close',()=>{clearInterval(timer);clearInterval(clock);},{once:true});await refresh();
  }
  function toolbar(resource,bar){if(resource==='firewall')bar.append(button(t('add'),authorize),button(t('policy'),policy),button(t('topology'),topology));if(resource==='settings')bar.append(button(t('edit'),settings));}
  function details(resource,item,bar){if(resource==='firewall')bar.append(button(t('extend'),()=>renew(item)),button(t('attach'),()=>attach(item)),button(t('remove'),()=>{if(confirm(t('remove')+'?'))action('firewall','delete',identity(item)).catch(e=>notice(e.message,true));}));}
  return {toolbar,details};
}
