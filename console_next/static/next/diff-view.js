export function diffLines(text='') {
  return text.split('\n').map(text=>({text,kind:text.startsWith('--- ')||text.startsWith('+++ ')||text.startsWith('diff ')||text.startsWith('index ')?'header':text.startsWith('@@')?'hunk':text.startsWith('+')?'added':text.startsWith('-')?'removed':text.startsWith('\\')?'marker':'context'}));
}
export function diffView(root,value,{el,button,copy,language='en',notice=()=>{}}) {
  const index=Math.max(0,['cs','en','fr'].indexOf(language));
  const t=key=>({copy:['Kopírovat diff','Copy diff','Copier le diff'],changes:['Jen změny','Changes only','Modifications seules'],wrap:['Zalomit řádky','Wrap lines','Retour à la ligne'],empty:['Bez změn','No changes','Aucune modification'],added:['přidáno','added','ajoutées'],removed:['odebráno','removed','supprimées'],label:['Rozdíl konfigurací','Configuration diff','Différence de configuration']}[key][index]);
  const lines=diffLines(value),added=lines.filter(l=>l.kind==='added').length,removed=lines.filter(l=>l.kind==='removed').length;
  const pre=el('pre',{class:'native-diff diff-view',tabindex:'0','aria-label':t('label')}),summary=el('span',{class:'diff-counts'},`+${added} ${t('added')} / −${removed} ${t('removed')}`);
  const changes=button(t('changes'),()=>{const on=pre.classList.toggle('changes-only');changes.setAttribute('aria-pressed',String(on));});changes.setAttribute('aria-pressed','false');
  const wrap=button(t('wrap'),()=>{const on=pre.classList.toggle('wrap-lines');wrap.setAttribute('aria-pressed',String(on));});wrap.setAttribute('aria-pressed','false');
  const toolbar=el('div',{class:'toolbar diff-toolbar'},summary,changes,wrap,button(t('copy'),async()=>{try{await copy(value);notice('✓');}catch(e){notice(e.message,true);}}));
  if(value)for(const line of lines)pre.append(el('div',{class:'diff-'+line.kind},line.text||' '));else pre.textContent=t('empty');
  root.append(toolbar,pre);
}
