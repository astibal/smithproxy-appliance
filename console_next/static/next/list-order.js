// Sort the displayed catalogue without mutating the runner response. Stable IDs
// break ties so polling cannot shuffle equal values under the user's pointer.
export function orderItems(items, sort, resource, columns, locale='en') {
  if(!sort || !Number.isInteger(sort.column))return items;
  const collator=new Intl.Collator(locale,{numeric:true,sensitivity:'base'});
  const stamp=value=>{const n=Date.parse(value||'');return Number.isFinite(n)?n:null;};
  const value=item=>{
    if(resource==='admins'&&sort.column===2)return stamp(item.created_at);
    if(resource==='admins'&&sort.column===3)return stamp(item.password_changed_at);
    if(sort.column===3){
      if(resource==='instances')return item.slice_rss_bytes??item.rss_bytes??null;
      if(['binaries','tuntom'].includes(resource))return stamp(item.built_at);
      if(resource==='configs')return stamp(item.updated_at||item.created_at);
      if(resource==='profiles'&&!item.description)return stamp(item.created_at||item.updated_at);
      if(resource==='tasks')return stamp(item.created_at);
      if(resource==='test-drives')return stamp(item.deadline);
      if(resource==='firewall')return stamp(item.expires_at);
    }
    return columns(resource,item)[sort.column]??null;
  };
  const id=item=>String(item.id||item.task_id||item.build_id||item.profile_id||item.config_id||'');
  return items.map((item,index)=>({item,index,value:value(item)})).sort((a,b)=>{
    // Missing/unlimited values always follow concrete values, in either direction.
    if(a.value==null&&b.value!=null)return 1;
    if(a.value!=null&&b.value==null)return -1;
    const comparison=a.value==null?0:typeof a.value==='number'&&typeof b.value==='number'?a.value-b.value:collator.compare(String(a.value),String(b.value));
    return comparison*(sort.direction==='desc'?-1:1)||collator.compare(id(a.item),id(b.item))||a.index-b.index;
  }).map(entry=>entry.item);
}

export function nextOrder(sort,column){
  return sort?.column===column ? (sort.direction==='asc'?{column,direction:'desc'}:null) : {column,direction:'asc'};
}
