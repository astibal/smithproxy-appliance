// Presentation never owns the connection: moving/minimizing/navigation retain
// each session's socket, xterm buffer and listeners until explicit close.
export function clampWindow(rect,width,height) {
  const w=Math.min(Math.max(320,rect.width),Math.max(200,width-16));
  const h=Math.min(Math.max(230,rect.height),Math.max(120,height-104));
  return {width:w,height:h,x:Math.max(8,Math.min(rect.x,width-w-8)),y:Math.max(8,Math.min(rect.y,height-h-96))};
}
export function terminalWorkspace({root,el,button,csrf,language,notice,onCount=()=>{},onHide=()=>{}}) {
  const sessions=new Map();let active=null,serial=0,level=0;
  const preference='sas-next-terminal-font';
  function fontSize(){try{const value=Number(localStorage.getItem(preference));return Number.isFinite(value)&&value>=10&&value<=30?value:14;}catch{return 14;}}
  const words={
    reconnect:['Připojit znovu','Reconnect','Reconnecter'],clear:['Vyčistit','Clear','Effacer'],
    minimize:['Minimalizovat','Minimize','Réduire'],minimizeAll:['Minimalizovat vše','Minimize all','Tout réduire'],
    close:['Zavřít a odpojit relaci','Close and disconnect session','Fermer et déconnecter la session'],
    opening:['Připojuji…','Connecting…','Connexion…'],open:['Připojeno','Connected','Connecté'],closed:['Odpojeno','Disconnected','Déconnecté'],error:['Chyba spojení','Connection error','Erreur de connexion'],
    size:['Velikost písma','Font size','Taille de police'],maximize:['Zvětšit','Maximize','Agrandir'],restore:['Obnovit velikost','Restore size','Rétablir la taille'],terminals:['Terminály','Terminals','Terminaux'],
    move:['Přesunout okno · táhni nebo použij šipky','Move window · drag or use arrow keys','Déplacer la fenêtre · glisser ou touches fléchées'],resize:['Velikost okna · táhni nebo použij šipky','Resize window · drag or use arrow keys','Redimensionner · glisser ou touches fléchées'],
    limit:['Nejvýše 12 terminálů; zavři nepotřebnou relaci.','Maximum 12 terminal sessions; close an unused session.','12 terminaux maximum ; fermez une session inutilisée.'],
  };
  const t=k=>words[k][Math.max(0,['cs','en','fr'].indexOf(language()))];
  const windows=el('div',{class:'terminal-windows'}),tray=el('div',{class:'terminal-tray',role:'toolbar'}),tabs=el('div',{class:'terminal-tray-items'}),caption=el('span',{class:'terminal-tray-caption'},t('terminals'));
  const minimizeAll=button('−',()=>{for(const s of sessions.values())minimize(s,false);onHide();});
  tray.append(caption,tabs,minimizeAll);root.replaceChildren(windows,tray);
  function resize(s){if(s&&!s.panel.hidden)requestAnimationFrame(()=>{if(sessions.has(s.key)&&!s.panel.hidden)s.fit.fit();});}
  function place(s){s.rect=clampWindow(s.rect,window.innerWidth,window.innerHeight);Object.assign(s.panel.style,{left:s.rect.x+'px',top:s.rect.y+'px',width:s.rect.width+'px',height:s.rect.height+'px'});resize(s);}
  function raise(s){active=s;for(const other of sessions.values())other.panel.classList.toggle('focused',other===s);s.panel.style.zIndex=String(++level);}
  function show(s){root.hidden=false;s.panel.hidden=false;s.tab.setAttribute('aria-pressed','true');raise(s);place(s);s.term.focus();}
  function minimize(s,focus=true){s.panel.hidden=true;s.tab.setAttribute('aria-pressed','false');if(focus)s.tab.focus();}
  function maximize(s){s.maximized=!s.maximized;s.panel.classList.toggle('terminal-maximized',s.maximized);s.maxButton.title=t(s.maximized?'restore':'maximize');s.maxButton.setAttribute('aria-label',s.maxButton.title);s.maxButton.setAttribute('aria-pressed',String(s.maximized));resize(s);}
  function setState(s,state){s.state=state;s.tab.dataset.state=state;s.status.textContent=t(state);s.status.dataset.state=state;}
  function connect(s){
    const old=s.socket;s.socket=null;old?.close();setState(s,'opening');
    const socket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws/${s.resource}/${encodeURIComponent(s.id)}/${s.kind}?csrf=${encodeURIComponent(csrf())}`);s.socket=socket;
    socket.onopen=()=>{if(s.socket===socket)setState(s,'open');};socket.onmessage=event=>{if(s.socket===socket)s.term.write(event.data);};
    socket.onclose=()=>{if(s.socket===socket){setState(s,'closed');s.term.write('\r\n['+t('closed')+']\r\n');}};socket.onerror=()=>{if(s.socket===socket)setState(s,'error');};
  }
  function close(s){const socket=s.socket;s.socket=null;socket?.close();s.observer.disconnect();s.term.dispose();s.panel.remove();s.tab.remove();sessions.delete(s.key);onCount(sessions.size);if(active===s)active=[...sessions.values()].at(-1)||null;root.hidden=!sessions.size;onHide();}
  function gesture(s,handle,sizing=false){
    let drag=null;
    handle.addEventListener('pointerdown',event=>{if(event.button!==0||s.maximized)return;event.preventDefault();raise(s);drag={x:event.clientX,y:event.clientY,rect:{...s.rect}};handle.setPointerCapture(event.pointerId);});
    handle.addEventListener('pointermove',event=>{if(!drag)return;const dx=event.clientX-drag.x,dy=event.clientY-drag.y;s.rect=sizing?{...drag.rect,width:drag.rect.width+dx,height:drag.rect.height+dy}:{...drag.rect,x:drag.rect.x+dx,y:drag.rect.y+dy};place(s);});
    const end=()=>{drag=null;};for(const event of ['pointerup','pointercancel','lostpointercapture'])handle.addEventListener(event,end);
    handle.addEventListener('keydown',event=>{if(s.maximized||!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key))return;event.preventDefault();const step=event.shiftKey?40:10,dx=event.key==='ArrowLeft'?-step:event.key==='ArrowRight'?step:0,dy=event.key==='ArrowUp'?-step:event.key==='ArrowDown'?step:0;s.rect=sizing?{...s.rect,width:s.rect.width+dx,height:s.rect.height+dy}:{...s.rect,x:s.rect.x+dx,y:s.rect.y+dy};place(s);});
  }
  function open(item,kind,resource='instances'){
    const id=item.id,key=[resource,id,kind].join('/');let s=sessions.get(key);if(s){show(s);return;}if(sessions.size>=12){notice(t('limit'),true);return;}
    const n=++serial,title=`${kind.toUpperCase()} · ${item.alias||item.name||id.slice(0,12)}`;
    const panel=el('section',{class:'terminal-float',role:'dialog','aria-modal':'false','aria-labelledby':'terminal-title-'+n,id:'terminal-window-'+n});
    const mover=el('div',{class:'terminal-mover',tabindex:'0',role:'button'},el('strong',{id:'terminal-title-'+n},title)),controls=[];
    const control=(key,label,fn)=>{const node=button(label||t(key),fn);controls.push([key,node,!label]);return node;};
    const minButton=control('minimize','−',()=>minimize(s)),maxButton=control('maximize','□',()=>maximize(s)),closer=control('close','×',()=>close(s));
    const header=el('header',{class:'terminal-titlebar'},mover,minButton,maxButton,closer),status=el('span',{class:'terminal-status',role:'status'}),font=el('span',{class:'terminal-font'});
    const toolbar=el('div',{class:'terminal-tools'},status,button('A−',()=>changeFont(-1)),font,button('A+',()=>changeFont(1)),control('reconnect','',()=>connect(s)),control('clear','',()=>s.term.clear()));
    const screen=el('div',{class:'terminal-screen'}),grip=control('resize','◢',()=>{});grip.className='terminal-resizer';panel.append(header,toolbar,screen,grip);windows.append(panel);root.hidden=false;
    const term=new window.Terminal({cursorBlink:true,fontSize:fontSize(),scrollback:10000,theme:{background:'#070e0d',foreground:'#dbe8e3',selectionBackground:'#527d70',selectionInactiveBackground:'#527d70'}});
    const fit=new window.FitAddon.FitAddon();term.loadAddon(fit);term.open(screen);
    const tab=button(title,()=>{if(!s.panel.hidden&&active===s)minimize(s);else show(s);});tab.setAttribute('aria-controls','terminal-window-'+n);tabs.append(tab);
    const observer=new ResizeObserver(()=>resize(s)),offset=(sessions.size%5)*28;
    s={key,id,kind,resource,panel,screen,term,fit,tab,observer,socket:null,state:'opening',status,font,controls,mover,maxButton,maximized:false,rect:{x:Math.max(8,window.innerWidth-940)+offset,y:88+offset,width:860,height:510}};
    sessions.set(key,s);onCount(sessions.size);observer.observe(screen);
    function changeFont(delta){term.options.fontSize=Math.max(10,Math.min(30,term.options.fontSize+delta));font.textContent=term.options.fontSize+' px';try{localStorage.setItem(preference,String(term.options.fontSize));}catch{}resize(s);}
    term.onData(data=>{if(data&&s.socket?.readyState===WebSocket.OPEN)s.socket.send(data);});
    panel.addEventListener('pointerdown',()=>raise(s));panel.addEventListener('focusin',()=>raise(s));gesture(s,mover);gesture(s,grip,true);mover.addEventListener('dblclick',()=>maximize(s));
    tab.onkeydown=event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const items=[...sessions.values()],i=items.indexOf(s),next=event.key==='Home'?0:event.key==='End'?items.length-1:(i+(event.key==='ArrowRight'?1:-1)+items.length)%items.length;items[next].tab.focus();};
    relabelSession(s);show(s);connect(s);
  }
  function relabelSession(s){for(const [key,node,hasText]of s.controls){const label=t(key==='maximize'&&s.maximized?'restore':key);if(hasText)node.textContent=label;node.title=label;node.setAttribute('aria-label',label);}s.mover.title=t('move');s.mover.setAttribute('aria-label',t('move'));s.font.textContent=s.term.options.fontSize+' px';s.font.title=t('size');s.status.textContent=t(s.state);}
  function relabel(){caption.textContent=t('terminals');tray.setAttribute('aria-label',t('terminals'));minimizeAll.title=t('minimizeAll');minimizeAll.setAttribute('aria-label',t('minimizeAll'));for(const s of sessions.values())relabelSession(s);}
  window.addEventListener('resize',()=>{for(const s of sessions.values())place(s);});
  window.addEventListener('beforeunload',event=>{if([...sessions.values()].some(s=>s.socket?.readyState===WebSocket.OPEN)){event.preventDefault();event.returnValue='';}});
  relabel();return {open,reveal(){if(active)show(active);},relabel};
}
