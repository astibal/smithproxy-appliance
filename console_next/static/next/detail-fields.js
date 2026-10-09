import {fieldLabel} from './field-labels.js';
const words={
  yes:['Ano','Yes','Oui'],no:['Ne','No','Non'],empty:['Žádné položky','No entries','Aucune entrée'],
  protected:['Chráněná hodnota','Protected value','Valeur protégée'],
  auto_restart:['Automatický restart','Automatic restart','Redémarrage automatique'],
  program_settings:['Nastavení programu','Application settings','Paramètres du programme'],
  work_files:['Soubory /work','Files /work','Fichiers /work'],wiring:['Zapojení','Connections','Connexions'],
  endpoints:['Konce propojení','Endpoints','Extrémités'],addressing:['Adresace','Addressing','Adressage'],
  desired:['Požadované nastavení','Desired configuration','Configuration souhaitée'],observed:['Zjištěný stav','Observed state','État observé'],
  chains:['Firewall řetězce','Firewall chains','Chaînes du pare-feu'],selectors:['Výběr provozu','Traffic selectors','Sélecteurs de trafic'],
  source:['Zdroj','Source','Source'],destination:['Cíl','Destination','Destination'],protocol:['Protokol','Protocol','Protocole'],
  expires_at:['Platnost do','Expires at','Expiration'],ttl_seconds:['TTL (sekundy)','TTL (seconds)','TTL (secondes)'],
  instances:['Navázané instance','Linked instances','Instances associées'],network:['Síť','Network','Réseau'],
  addresses:['Adresy','Addresses','Adresses'],routes:['Routy','Routes','Routes'],parameters:['Parametry','Parameters','Paramètres'],
  ingress:['Ingress','Ingress','Entrée'],egress:['Egress','Egress','Sortie'],
  ports:['Porty','Ports','Ports'],driver:['Driver','Driver','Pilote'],mode:['Režim','Mode','Mode'],
  register_source:['Source pool','Source pool','Pool source'],system:['Systém','System','Système'],
  status:['Stav','Status','État'],state:['Stav','State','État'],name:['Název','Name','Nom'],
  description:['Popis','Description','Description'],error:['Chyba','Error','Erreur'],
  available:['Dostupné','Available','Disponible'],persistent:['Perzistentní','Persistent','Persistant'],
  source_ips:['Autorizované zdroje','Authorized sources','Sources autorisées'],
  has_private_key:['Privátní klíč přítomen','Private key present','Clé privée présente'],has_secret:['Secret nastaven','Secret configured','Secret configuré'],
};
export function detailLabel(key,language='en'){
  const index=Math.max(0,['cs','en','fr'].indexOf(language));
  if(words[key])return words[key][index];
  const spaced=key.replaceAll('_',' '),translated=fieldLabel(spaced,language);
  return translated!==spaced?translated:fieldLabel(spaced.charAt(0).toUpperCase()+spaced.slice(1),language);
}
export function protectedField(key){return /(^|_)(password|secret|token|private_key|content_base64)($|_)/i.test(key);}
// Keyed updates keep opened sections, focused copy controls and selected text
// stable across polling. No HTML from API values is ever interpreted.
export function renderFields(root,data,{el,button,copy=()=>{},language='en'},depth=0){
  const entries=Array.isArray(data)?data.map((v,i)=>[String(i+1),v]):data&&typeof data==='object'?Object.entries(data):[['value',data]];
  const keys=new Set(entries.map(([k])=>k));for(const node of [...root.children])if(!keys.has(node.dataset.key))node.remove();
  for(const [key,raw]of entries){
    const presence=typeof raw==='boolean'&&/^has_/.test(key);
    const value=protectedField(key)&&!presence?detailLabel('protected',language):raw;
    const nested=value!==null&&typeof value==='object';
    let row=[...root.children].find(n=>n.dataset.key===key);
    if(row&&row.dataset.kind!==(nested?'group':'value')){row.remove();row=null;}
    if(!row){
      if(nested){row=el('details',{class:'data-group','data-key':key,'data-kind':'group'},el('summary'),el('div',{class:'data-fields'}));row.open=depth===0;}
      else{const control=button('',()=>copy(control.dataset.value));row=el('div',{class:'data-value','data-key':key,'data-kind':'value'},el('span',{class:'data-label'}),control);}
      root.append(row);
    }
    const label=Array.isArray(data)?String(value?.name||value?.interface||value?.path||value?.role||key):detailLabel(key,language);
    const text=nested?`${label} · ${Object.keys(value).length}`:label;
    if(row.children[0].textContent!==text)row.children[0].textContent=text;
    if(nested){if(depth<8)renderFields(row.children[1],value,{el,button,copy,language},depth+1);}
    else{const control=row.children[1],display=value==null||value===''?'—':typeof value==='boolean'?detailLabel(value?'yes':'no',language):String(value);control.dataset.value=value==null?'':String(value);if(control.textContent!==display)control.textContent=display;}
  }
}
export function fieldSection(body,title,controls,{el}){
  const section=el('fieldset',{class:'form-section'},el('legend',{},title)),grid=el('div',{class:'section-fields'});
  section.append(grid);for(const control of controls){const label=control?.parentElement;if(label?.tagName==='LABEL')grid.append(label);}
  body.append(section);return section;
}
export function argumentFields(body,values,{el,button,language='en',touch=()=>{}}){
  const index=Math.max(0,['cs','en','fr'].indexOf(language));
  const label=['Argumenty · jeden vstup = jeden argument','Arguments · one input = one argument','Arguments · une entrée = un argument'][index];
  const root=el('div',{class:'argument-editor'},el('span',{},label)),list=el('div',{class:'argument-list'}),entries=[];
  function add(value=''){const input=el('input',{type:'text','aria-label':label}),row=el('div',{class:'argument-row'});input.value=value;row.append(input,button('−',()=>{row.remove();touch();}));list.append(row);entries.push({row,input});}
  root.append(list,button(['+ Argument','+ Argument','+ Argument'][index],()=>{add();touch();}));body.append(root);values.forEach(add);
  return {root,control:list,read:()=>entries.filter(e=>e.row.isConnected).map(e=>e.input.value)};
}
