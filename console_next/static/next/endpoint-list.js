// Keep actionable DOM stable when observed addressing/status is polled.
export function endpointList(root,{el,button,t,onAddress,onDetach}) {
  const cards=new Map();
  const text=(node,value)=>{if(node.textContent!==value)node.textContent=value;};
  return current=>{
    const endpoints=current.endpoints||[],wanted=new Set(endpoints.map(ep=>ep.id));
    for(const [id,entry]of cards)if(!wanted.has(id)){entry.card.remove();cards.delete(id);}
    endpoints.forEach((ep,index)=>{
      let entry=cards.get(ep.id);
      if(!entry){
        const title=el('h3'),identity=el('code'),state=el('p'),error=el('p',{class:'error'}),status=el('p'),addressError=el('p',{class:'error'}),observed=el('small'),desired=el('p');
        entry={title,identity,state,error,status,addressError,observed,desired,current,ep};
        const address=button(t('addressing'),()=>onAddress(entry.current,entry.ep)),detach=button(t('detach'),()=>onDetach(entry.current,entry.ep));
        entry.card=el('article',{class:'endpoint-card'},title,identity,state,error,status,addressError,observed,desired,address,detach);cards.set(ep.id,entry);
      }
      entry.current=current;entry.ep=ep;const addressing=ep.addressing||{};
      text(entry.title,ep.interface||'—');text(entry.identity,ep.instance_id||'—');text(entry.state,`${ep.type} · ${ep.state}`);
      text(entry.error,ep.error||'');entry.error.hidden=!ep.error;
      text(entry.status,t(addressing.state||'unmanaged'));entry.status.className=addressing.error?'error':'muted';
      text(entry.addressError,addressing.error||'');entry.addressError.hidden=!addressing.error;
      text(entry.observed,`${t('observed')}: ${(addressing.observed?.addresses||[]).join(', ')||'—'}`);entry.observed.hidden=!addressing.observed;
      text(entry.desired,`${t('desired')}: ${(addressing.desired?.addresses||[]).join(', ')||'—'}`);
      if(root.children[index]!==entry.card)root.insertBefore(entry.card,root.children[index]||null);
    });
  };
}
