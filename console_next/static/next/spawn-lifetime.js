export function lifetimeHint(profile,language='en') {
  const i=Math.max(0,['cs','en','fr'].indexOf(language));
  if(!profile)return ['Prázdné nebo 0 = bez časového limitu.','Empty or 0 = no time limit.','Vide ou 0 = sans limite de durée.'][i];
  const seconds=profile.ttl_seconds===undefined?1800:profile.ttl_seconds;
  const value=seconds==null?['bez časového limitu','no time limit','sans limite de durée'][i]:`${seconds} s`;
  return [ `Převzato z profilu: ${value}. Limit změň v editaci profilu.`, `Inherited from profile: ${value}. Change the limit in the profile editor.`, `Hérité du profil : ${value}. Modifiez la limite dans l’éditeur du profil.` ][i];
}
