export function certificateValidity(cert,now=Date.now()) {
  const start=Date.parse(cert.notbefore||''),end=Date.parse(cert.notafter||'');
  if(Number.isFinite(start)&&start>now)return {state:'future',days:Math.ceil((start-now)/86400000)};
  if(!Number.isFinite(end))return {state:'unknown',days:null};
  const days=Math.ceil((end-now)/86400000);
  return {state:end<=now?'expired':days<=30?'soon':'current',days};
}
export function certificateCard(cert,{el,button,copy,language='en',now=Date.now()}) {
  const index=Math.max(0,['cs','en','fr'].indexOf(language)),t=key=>({
    expired:['Platnost skončila','Expired','Expiré'],future:['Platnost ještě nezačala','Not yet valid','Pas encore valide'],soon:['Brzy expiruje','Expires soon','Expire bientôt'],current:['V časové platnosti','Within validity period','Dans la période de validité'],unknown:['Platnost neznámá','Validity unknown','Validité inconnue'],
    days:['dní','days','jours'],fingerprint:['Otisk certifikátu SHA-256','Certificate SHA-256 fingerprint','Empreinte SHA-256 du certificat'],fileHash:['SHA-256 souboru','File SHA-256','SHA-256 du fichier'],privateKey:['Privátní klíč přítomen','Private key present','Clé privée présente'],
  }[key][index]);
  const validity=certificateValidity(cert,now),card=el('article',{class:'certificate-entry','data-validity':validity.state});
  card.append(el('strong',{},cert.name||cert.filename),el('span',{class:'certificate-validity'},t(validity.state)+(validity.days>0?` · ${validity.days} ${t('days')}`:'')),el('code',{},[cert.filename,cert.key_filename].filter(Boolean).join(' + ')),el('small',{},`${cert.kind||''} · ${cert.subject||''}${cert.has_private_key?' · '+t('privateKey'):''}`),el('small',{},`${cert.notbefore||'—'} → ${cert.notafter||'—'}`));
  for(const [key,label]of [['sha256_fingerprint','fingerprint'],['sha256','fileHash']])if(cert[key]){
    const control=button(cert[key],()=>copy(cert[key]));control.title=t(label);control.setAttribute('aria-label',t(label)+': '+cert[key]);card.append(el('small',{},t(label)),control);
  }
  return card;
}
