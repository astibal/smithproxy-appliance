export function quickSearch({el,button,fetchItems,identity,filtered,language,label}) {
  const i=Math.max(0,['cs','en','fr'].indexOf(language)),title=['Hledat v SAS','Search SAS','Rechercher dans SAS'][i];
  const dialog=el('dialog',{class:'workflow-dialog','aria-label':title}),input=el('input',{type:'search','aria-label':title,placeholder:title}),results=el('div',{class:'quick-results'}),status=el('p',{role:'status'});
  const close=()=>{dialog.close();dialog.remove();};
  dialog.append(el('header',{},el('h2',{},title),button('×',close)),input,status,results);document.body.append(dialog);dialog.showModal();input.focus();
  dialog.addEventListener('close',()=>dialog.remove(),{once:true});
  const records=[],failures=[];let pending=6;
  function draw(){if(!dialog.isConnected)return;const focused=document.activeElement?.dataset.resultKey,matched=filtered(records,input.value),found=matched.slice(0,60);results.replaceChildren(...found.map(record=>{const control=button(`${record.alias||record.name||record.id} · ${label(record.resource)} · ${record.id.slice(0,12)}`,()=>{location.hash=record.resource+'/'+encodeURIComponent(record.id);close();});control.dataset.resultKey=record.resource+'/'+record.id;return control;}));
    if(focused)[...results.children].find(n=>n.dataset.resultKey===focused)?.focus({preventScroll:true});
    status.textContent=`${found.length} / ${matched.length}${pending?' · '+['Načítání','Loading','Chargement'][i]+'…':''}${!pending&&!matched.length?' · '+['Žádné výsledky','No results','Aucun résultat'][i]:''}${failures.length?' · '+['Nedostupné','Unavailable','Indisponible'][i]+': '+failures.map(label).join(', '):''}`;}
  input.oninput=draw;input.onkeydown=e=>{if(e.key==='ArrowDown'){e.preventDefault();results.querySelector('button')?.focus();}if(e.key==='Enter'){e.preventDefault();results.querySelector('button')?.click();}};
  for(const resource of ['instances','profiles','configs','binaries','wiring','endpoints'])fetchItems(resource).then(items=>{for(const item of items)records.push({id:identity(item),resource,alias:item.alias||'',name:item.name||item.ref||'',source_ip:item.source_ip||'',namespace:item.namespace||''});}).catch(()=>failures.push(resource)).finally(()=>{pending--;draw();});draw();
}
