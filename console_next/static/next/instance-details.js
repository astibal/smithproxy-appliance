// All fields remain available; unknown/new runner fields land in Advanced.
export function instanceDetailGroups(data,language='en'){
  const i=Math.max(0,['cs','en','fr'].indexOf(language));
  const labels={network:['Síť a adresace','Network & addressing','Réseau et adressage'],runtime:['Procesy a lifecycle','Processes & lifecycle','Processus et cycle de vie'],files:['Soubory a runtime','Files & runtime','Fichiers et runtime'],versions:['Verze a konfigurace','Versions & configuration','Versions et configuration'],advanced:['Pokročilé údaje','Advanced details','Détails avancés']};
  const groups=Object.fromEntries(Object.keys(labels).map(k=>[k,{}]));
  for(const [key,value] of Object.entries(data)){
    const group=/network|ingress|egress|source|address|route|wiring|interface|endpoint|namespace/.test(key)?'network':
      /build|commit|config|profile|cert_bundle|ref$/.test(key)?'versions':
      /path|dir|rootfs|filesystem|work_|binary/.test(key)?'files':
      /pid|rss|memory|slice|unit|members|restart|crash|stopped|created|deadline|ttl|persistent|result|state|debug|system_start|recovery/.test(key)?'runtime':'advanced';
    groups[group][key]=value;
  }
  return Object.fromEntries(Object.entries(groups).filter(([,v])=>Object.keys(v).length).map(([k,v])=>[labels[k][i],v]));
}
