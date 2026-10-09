export function networkChoices(profiles,side) {
  return profiles.filter(p=>(p.consumes||[p.kind]).includes(side)).map(p=>[p.network_profile_id,p.name+((p.consumes||[]).length>1?' · VIA ↔':'')]);
}
export function bindNetworkSides(ingress,egress,profiles,{el,language='en'}) {
  const controls={ingress,egress},hints={};let owner=null;
  const duplex=value=>{const p=profiles.find(p=>p.network_profile_id===value);return p?.consumes?.includes('ingress')&&p.consumes.includes('egress');};
  const message=['Řízeno protistranou: VIA používá ingress i egress.','Controlled by the other side: VIA uses ingress and egress.','Contrôlé par l’autre côté : VIA utilise l’entrée et la sortie.'][Math.max(0,['cs','en','fr'].indexOf(language))];
  for(const [side,control] of Object.entries(controls)){hints[side]=el('small',{},message);control.parentElement.append(hints[side]);}
  function render(){for(const [side,control]of Object.entries(controls)){const locked=Boolean(owner)&&owner!==side;control.disabled=locked;control.setAttribute('aria-disabled',String(locked));hints[side].hidden=!locked;}}
  function select(side){
    if(duplex(controls[side].value)){owner=side;controls[side==='ingress'?'egress':'ingress'].value=controls[side].value;}
    else if(owner===side){controls[side==='ingress'?'egress':'ingress'].value='';owner=null;}
    render();
  }
  for(const [side,control]of Object.entries(controls))control.addEventListener('change',()=>select(side));
  if(duplex(egress.value))select('egress');else if(duplex(ingress.value))select('ingress');else render();
}
