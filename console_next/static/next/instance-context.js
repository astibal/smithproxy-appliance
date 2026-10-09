export function instanceEvents(item) {
  return [['created_at','created'],['last_restart_at','restart'],['crash_at','crash'],['stopped_at','stopped']]
    .filter(([field])=>item[field]&&Number.isFinite(Date.parse(item[field])))
    .map(([field,kind])=>({kind,time:item[field]})).sort((a,b)=>Date.parse(b.time)-Date.parse(a.time));
}
export function instanceReferences(item) {
  return [['runtime_profile_id','profiles'],['build_id','binaries'],['config_id','configs'],['cert_bundle_id','certificates'],['ingress_network_profile_id','networks'],['egress_network_profile_id','networks'],['headless_endpoint_id','endpoints']]
    .filter(([key])=>item[key]&&item[key]!=='active').map(([key,resource])=>({key,resource,id:item[key]}));
}
export function instanceContext(item,{el,language='en',cache,identity,label,stateLabel}) {
  const i=Math.max(0,['cs','en','fr'].indexOf(language));
  const t=values=>values[i];
  const root=el('section',{class:'instance-context'});
  root.append(el('h3',{},t(['Provozní souhrn','Runtime summary','Résumé d’exécution'])),el('p',{},stateLabel(item.state||'unknown')));
  if(item.result)root.append(el('p',{class:item.state==='failed'?'error':''},String(item.result)));
  root.append(el('p',{class:'muted'},t(['Stav hlášený runnerem; neověřuje dostupnost aplikace ani průchodnost sítě.','State reported by the runner; does not verify application availability or network reachability.','État signalé par le runner ; ne vérifie ni la disponibilité de l’application ni la connectivité réseau.'])));
  root.append(el('h3',{},t(['Vazby této instance','Instance references','Références de l’instance'])));
  for(const ref of instanceReferences(item)){
    const known=(cache.get(ref.resource)||[]).find(v=>identity(v)===ref.id);
    root.append(el('p',{},label(ref.resource)+': ',el('a',{href:'#'+ref.resource+'/'+encodeURIComponent(ref.id),title:ref.id},known?.alias||known?.name||known?.ref||ref.id)));
  }
  root.append(el('p',{class:'muted'},t(['Vazby jsou ze záznamu instance. Obsah knihovny se mohl od startu změnit; nejde o porovnání živých souborů.','References come from the instance record. Library content may have changed since startup; this is not a comparison of live files.','Les références proviennent de l’instance. La bibliothèque peut avoir changé depuis le démarrage ; ceci ne compare pas les fichiers actifs.'])));
  root.append(el('h3',{},t(['Zaznamenané milníky','Recorded milestones','Événements enregistrés'])));
  const names={created:['Vytvoření','Created','Création'],restart:['Poslední restart','Last restart','Dernier redémarrage'],crash:['Poslední pád','Last crash','Dernier plantage'],stopped:['Zastavení','Stopped','Arrêt']};
  const list=el('ol');for(const event of instanceEvents(item))list.append(el('li',{},el('time',{datetime:event.time},new Date(event.time).toLocaleString(language)), ' · '+t(names[event.kind])));root.append(list);
  root.append(el('small',{class:'muted'},t(['Jen uložené časové údaje, nikoliv úplný audit. Opakované restarty či pády zde mají pouze poslední záznam.','Stored timestamps only, not a full audit. Repeated restarts or crashes show only the latest record.','Horodatages enregistrés uniquement, pas un audit complet. Seul le dernier redémarrage ou plantage est affiché.'])));
  return root;
}
