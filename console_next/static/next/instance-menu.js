const names={runtime:['Běh','Lifecycle','Cycle de vie'],console:['Konzole a diagnostika','Consoles & diagnostics','Consoles et diagnostic'],config:['Konfigurace','Configuration','Configuration'],network:['Síť a služby','Network & services','Réseau et services'],settings:['Nastavení','Settings','Réglages']};
export function instanceMenu(root,{el,language}){
  root.classList.add('instance-menu');
  const groups=new Map();
  return key=>{
    if(groups.has(key))return groups.get(key);
    const summary=el('summary',{},names[key][Math.max(0,['cs','en','fr'].indexOf(language))]);
    const body=el('div',{class:'instance-menu-items'}),section=el('details',{class:'instance-menu-group'},summary,body);
    section.addEventListener('toggle',()=>{if(section.open)for(const other of root.querySelectorAll('.instance-menu-group'))if(other!==section)other.open=false;});
    section.addEventListener('keydown',e=>{if(e.key==='Escape'){e.stopPropagation();section.open=false;summary.focus();}});
    body.addEventListener('click',e=>{if(e.target.closest('button,a'))section.open=false;});
    root.append(section);groups.set(key,body);return body;
  };
}
