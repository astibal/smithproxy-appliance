// Usage is informational; links never mutate or restart the referenced object.
export function usagePanel(item,{el,language='en',instances=true,newTab=false}) {
  const index=Math.max(0,['cs','en','fr'].indexOf(language));
  const labels=[['Použití','Usage','Utilisation'],['Profily','Profiles','Profils'],['Instance','Instances','Instances']];
  const groups=[['profiles',item.usage?.runtime_profiles||[],labels[1][index]]];
  if(instances)groups.push(['instances',item.usage?.instances||[],labels[2][index]]);
  if(!groups.some(([,items])=>items.length))return null;
  const panel=el('section',{class:'usage-panel'},el('strong',{},labels[0][index]));
  for(const [route,items,label]of groups){if(!items.length)continue;
    const links=items.map(value=>{
      const id=route==='profiles'?(value.profile_id||value.id):value.id;
      if(!id)return el('span',{},value.name||'—');
      return el('a',{href:'#'+route+'/'+encodeURIComponent(id),title:String(id),class:'download-link',...(newTab?{target:'_blank',rel:'noopener'}:{})},value.alias||value.name||String(id).slice(0,12));
    });
    panel.append(el('div',{},el('small',{},label+': '),...links));
  }
  return panel;
}
