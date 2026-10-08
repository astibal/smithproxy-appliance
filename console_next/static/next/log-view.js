// Read-only rolling log buffer. Scrolling away freezes the visible snapshot:
// even a server-side tail-window rollover must not change the lines being read.
export function logView(root,{el,button,copy,language}){
  const words={pause:['Pozastavit','Pause','Pause'],resume:['Pokračovat','Resume','Reprendre'],follow:['Sledovat konec','Follow tail','Suivre la fin'],wrap:['Zalomit řádky','Wrap lines','Retour à la ligne'],copy:['Kopírovat log','Copy log','Copier le journal'],waiting:['Načítám log…','Loading log…','Chargement du journal…'],empty:['Log je prázdný','Log is empty','Journal vide'],paused:['Aktualizace pozastavené','Updates paused','Actualisation en pause']};
  const t=k=>words[k][Math.max(0,['cs','en','fr'].indexOf(language()))];
  words.newer=['Je dostupný novější log · klikni na Sledovat konec','New log data available · click Follow tail','Nouvelles données disponibles · cliquer sur Suivre la fin'];
  let paused=false,following=true,content='',pending='',hasContent=false;
  const pre=el('pre',{class:'log-buffer',tabindex:'0'}),status=el('small',{role:'status',class:'muted'},t('waiting'));
  const pause=button(t('pause'),()=>{paused=!paused;pause.textContent=t(paused?'resume':'pause');pause.setAttribute('aria-pressed',String(paused));status.textContent=paused?t('paused'):!hasContent?t('waiting'):pending!==content?t('newer'):content?'':t('empty');});
  function render(){
    if(content!==pending){content=pending;pre.textContent=content;}
    status.textContent=paused?t('paused'):!hasContent?t('waiting'):content?'':t('empty');
    pre.scrollTop=pre.scrollHeight;
  }
  const follow=button(t('follow'),()=>{following=true;follow.setAttribute('aria-pressed','true');render();});follow.setAttribute('aria-pressed','true');
  const wrap=button(t('wrap'),()=>{const enabled=pre.classList.toggle('wrap-lines');wrap.setAttribute('aria-pressed',String(enabled));});wrap.setAttribute('aria-pressed','false');
  pre.addEventListener('scroll',()=>{const wasFollowing=following;following=pre.scrollHeight-pre.scrollTop-pre.clientHeight<40;follow.setAttribute('aria-pressed',String(following));if(following&&!wasFollowing)render();});
  root.append(el('div',{class:'log-toolbar'},pause,follow,wrap,button(t('copy'),()=>copy(content))),status,pre);
  return {
    paused:()=>paused,
    update(value){
      if(paused)return;
      hasContent=true;pending=value;
      if(!following){status.textContent=pending!==content?t('newer'):content?'':t('empty');return;}
      render();
    },
  };
}
