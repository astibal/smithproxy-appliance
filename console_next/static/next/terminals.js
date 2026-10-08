// Each tab owns its socket, xterm buffer and resize observer. Navigation only
// changes visibility; an explicit close is required to dispose a session.
export function terminalWorkspace({root,el,button,csrf,language,notice,onCount=()=>{},onHide=()=>{}}) {
  const sessions=new Map();let active=null,maximized=false,serial=0;
  const preference='sas-next-terminal-font';
  function fontSize(){try{const value=Number(localStorage.getItem(preference));return Number.isFinite(value)&&value>=10&&value<=30?value:14;}catch{return 14;}}
  const words={reconnect:['Připojit znovu','Reconnect','Reconnecter'],clear:['Vyčistit','Clear','Effacer'],hide:['Skrýt panely','Hide panels','Masquer les panneaux'],close:['Zavřít relaci','Close session','Fermer la session'],detach:['Samostatné okno','Separate window','Fenêtre séparée'],opening:['Připojuji…','Connecting…','Connexion…'],open:['Připojeno','Connected','Connecté'],closed:['Odpojeno','Disconnected','Déconnecté'],error:['Chyba spojení','Connection error','Erreur de connexion'],size:['Velikost písma','Font size','Taille de police']};
  const t=k=>words[k][Math.max(0,['cs','en','fr'].indexOf(language()))];
  Object.assign(words,{maximize:['Zvětšit','Expand','Agrandir'],restore:['Obnovit velikost','Restore size','Rétablir la taille'],limit:['Nejvýše 12 terminálů; zavři nepotřebnou relaci.','Maximum 12 terminal sessions; close an unused tab.','12 terminaux maximum ; fermez une session inutilisée.']});
  const tabs=el('div',{class:'terminal-tabs',role:'tablist','aria-label':'Terminals'});
  const panels=el('div',{class:'terminal-panels'}),status=el('span',{class:'terminal-status',role:'status'});
  const font=el('span',{class:'terminal-font'});
  const controls=[];
  const control=(key,fn)=>{const node=button(t(key),fn);controls.push([key,node]);return node;};
  const resize=s=>{if(s&&!s.panel.hidden&&!root.hidden)requestAnimationFrame(()=>{if(sessions.has(s.key))s.fit.fit();});};
  const toolbar=el('header',{},status,
    button('A−',()=>changeFont(-1)),font,button('A+',()=>changeFont(1)),
    control('reconnect',()=>active&&connect(active)),control('clear',()=>active?.term.clear()),
    control('detach',()=>{if(!active)return;const url=new URL(location.href);url.hash='instances';url.search=new URLSearchParams({terminal:active.id,kind:active.kind,resource:active.resource}).toString();window.open(url,'_blank','noopener');}),
    control('maximize',()=>{maximized=!maximized;root.classList.toggle('terminal-maximized',maximized);maximizeButton.textContent=t(maximized?'restore':'maximize');maximizeButton.setAttribute('aria-pressed',String(maximized));resize(active);if(!maximized)root.scrollIntoView({block:'nearest',behavior:'auto'});}),
    control('hide',()=>{root.hidden=true;onHide();}));
  const maximizeButton=controls.find(([key])=>key==='maximize')[1];maximizeButton.setAttribute('aria-pressed','false');
  root.replaceChildren(tabs,toolbar,panels);
  function changeFont(delta){if(!active)return;active.term.options.fontSize=Math.max(10,Math.min(30,active.term.options.fontSize+delta));font.textContent=active.term.options.fontSize+' px';font.title=t('size');try{localStorage.setItem(preference,String(active.term.options.fontSize));}catch{}resize(active);}
  function show(s){active=s;root.hidden=false;for(const item of sessions.values()){item.panel.hidden=item!==s;item.tab.setAttribute('aria-selected',String(item===s));item.tab.tabIndex=item===s?0:-1;}status.textContent=t(s.state);font.textContent=s.term.options.fontSize+' px';resize(s);s.term.focus();}
  function setState(s,state){s.state=state;s.tab.dataset.state=state;if(active===s)status.textContent=t(state);}
  function connect(s){
    const old=s.socket;s.socket=null;old?.close();setState(s,'opening');
    const socket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws/${s.resource}/${encodeURIComponent(s.id)}/${s.kind}?csrf=${encodeURIComponent(csrf())}`);s.socket=socket;
    socket.onopen=()=>{if(s.socket===socket)setState(s,'open');};
    socket.onmessage=event=>{if(s.socket===socket)s.term.write(event.data);};
    socket.onclose=()=>{if(s.socket===socket){setState(s,'closed');s.term.write('\r\n['+t('closed')+']\r\n');}};
    socket.onerror=()=>{if(s.socket===socket)setState(s,'error');};
  }
  function close(s){s.socket?.close();s.socket=null;s.observer.disconnect();s.term.dispose();s.panel.remove();s.group.remove();sessions.delete(s.key);onCount(sessions.size);if(active===s){active=null;const next=sessions.values().next().value;if(next)show(next);else root.hidden=true;}}
  function open(item,kind,resource='instances'){
    const id=item.id,key=[resource,id,kind].join('/');let s=sessions.get(key);
    if(s){show(s);root.scrollIntoView({block:'nearest',behavior:'auto'});return;}
    if(sessions.size>=12){notice(t('limit'),true);return;}
    const panelId='sas-terminal-panel-'+(++serial),tabId='sas-terminal-tab-'+serial;
    const panel=el('div',{class:'terminal-screen',role:'tabpanel',id:panelId,'aria-labelledby':tabId});
    const term=new window.Terminal({cursorBlink:true,fontSize:fontSize(),scrollback:10000,theme:{background:'#070e0d',foreground:'#dbe8e3',selectionBackground:'#527d70',selectionInactiveBackground:'#527d70'}});
    const fit=new window.FitAddon.FitAddon();term.loadAddon(fit);
    const tab=button(`${kind.toUpperCase()} · ${item.alias||item.name||id.slice(0,12)}`,()=>show(s));tab.setAttribute('role','tab');tab.setAttribute('id',tabId);tab.setAttribute('aria-controls',panelId);
    const closer=button('×',()=>close(s));closer.title=t('close');closer.setAttribute('aria-label',t('close'));
    const group=el('div',{class:'terminal-tab'},tab,closer);tabs.append(group);panels.append(panel);root.hidden=false;term.open(panel);
    const observer=new ResizeObserver(()=>resize(s));
    s={key,id,kind,resource,panel,term,fit,tab,group,observer,socket:null,state:'opening'};sessions.set(key,s);onCount(sessions.size);observer.observe(panel);
    term.onData(data=>{if(data&&s.socket?.readyState===WebSocket.OPEN)s.socket.send(data);});
    tab.onkeydown=event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const items=[...sessions.values()],n=items.indexOf(s);const next=event.key==='Home'?0:event.key==='End'?items.length-1:(n+(event.key==='ArrowRight'?1:-1)+items.length)%items.length;show(items[next]);items[next].tab.focus();};
    show(s);connect(s);root.scrollIntoView({block:'nearest',behavior:'auto'});
  }
  window.addEventListener('beforeunload',event=>{if([...sessions.values()].some(s=>s.socket?.readyState===WebSocket.OPEN)){event.preventDefault();event.returnValue='';}});
  return {open,reveal(){if(active){show(active);root.scrollIntoView({block:'nearest'});}},relabel(){for(const [key,node]of controls)node.textContent=t(key==='maximize'&&maximized?'restore':key);if(active)status.textContent=t(active.state);for(const s of sessions.values()){const closeButton=s.group.children[1];closeButton.title=t('close');closeButton.setAttribute('aria-label',t('close'));}}};
}
