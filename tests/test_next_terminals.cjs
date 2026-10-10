const assert=require('node:assert/strict');
const fs=require('node:fs');
class Node {
  constructor(tag,attrs={},...children){this.tag=tag;this.attrs=attrs;this.children=children;this.dataset={};this.style={};this.events={};this.hidden=false;this.classes=new Set();this.classList={toggle:(name,on)=>on?this.classes.add(name):this.classes.delete(name)};}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  setAttribute(k,v){this.attrs[k]=v;}
  remove(){this.removed=true;}
  scrollIntoView(){this.scrolled=(this.scrolled||0)+1;}
  focus(){}
  querySelector(){return {focus(){}};}
  addEventListener(event,fn){this.events[event]=fn;}
  setPointerCapture(){}
}
class Socket {
  static OPEN=1;static all=[];
  constructor(url){this.url=url;this.readyState=1;this.sent=[];Socket.all.push(this);}
  close(){this.closed=true;this.readyState=3;this.onclose?.();}
  send(data){this.sent.push(data);}
}
class Term {
  static all=[];
  constructor(options){this.options=options;this.output=[];Term.all.push(this);}
  loadAddon(){}open(){}focus(){}clear(){}dispose(){this.disposed=true;}
  onData(fn){this.input=fn;}write(data){this.output.push(data);}
}
(async()=>{
  const events={};global.window={innerWidth:1440,innerHeight:900,Terminal:Term,FitAddon:{FitAddon:class{fit(){}}},addEventListener:(event,fn)=>events[event]=fn};
  global.location={protocol:'http:',host:'test.invalid',href:'http://test.invalid/'};
  global.WebSocket=Socket;global.ResizeObserver=class{observe(){}disconnect(){}};
  global.requestAnimationFrame=fn=>fn();
  const preferences=new Map();global.localStorage={getItem:key=>preferences.get(key)||null,setItem:(key,value)=>preferences.set(key,value)};
  const source=fs.readFileSync('console_next/static/next/terminals.js','utf8');
  const {terminalWorkspace,clampWindow}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
  const mobile=clampWindow({x:1200,y:800,width:860,height:500},390,700);assert.ok(mobile.x+mobile.width<=390);assert.ok(mobile.y+mobile.height<=604);
  const root=new Node('section'),el=(...args)=>new Node(...args),button=(label,click)=>Object.assign(el('button',{},label),{click});
  const manager=terminalWorkspace({root,el,button,csrf:()=> 'token',language:()=> 'en',notice:()=>{}});
  manager.open({id:'one'},'cli');manager.open({id:'two'},'netns');
  Term.all[1].input('');Term.all[1].input('pwd\r');
  assert.deepEqual(Socket.all[1].sent,[JSON.stringify({type:'input',data:'pwd\r'})]);
  Term.all[1].cols=100;Term.all[1].rows=28;Socket.all[1].onopen();
  assert.deepEqual(JSON.parse(Socket.all[1].sent.at(-1)),{type:'resize',cols:100,rows:28});
  for(const term of Term.all){assert.ok(term.options.fontFamily.endsWith('monospace'));assert.equal(term.options.fontWeight,400);assert.equal(term.options.lineHeight,1.15);}
  const windows=root.children[0],tray=root.children[1],first=windows.children[0],second=windows.children[1];
  const control=(panel,label)=>panel.children.flatMap(node=>node.children||[]).find(node=>node.attrs?.['aria-label']===label||node.children?.[0]===label);
  control(second,'Maximize').click();
  assert.ok(second.classes.has('terminal-maximized'));assert.equal(Socket.all.length,2,'expanding must not reconnect');
  control(second,'A+').click();
  assert.equal(Term.all[1].options.fontSize,15);assert.equal(preferences.get('sas-next-terminal-font'),'15');
  assert.equal(Socket.all.length,2);assert.ok(!Socket.all[0].closed,'opening second terminal must retain first connection');
  manager.open({id:'one'},'cli');assert.equal(Socket.all.length,2,'selecting existing terminal must not reconnect');
  assert.equal(first.hidden,false);assert.equal(second.hidden,false,'windows can be visible side by side');
  Term.all[0].input('');Term.all[0].input('show status\r');
  assert.deepEqual(Socket.all[0].sent,['show status\r'],'empty input is never sent');
  control(first,'Reconnect').click();
  assert.equal(Socket.all.length,3);assert.equal(Socket.all[0].closed,true);assert.ok(!Socket.all[1].closed);
  Socket.all[0].onmessage({data:'stale'});assert.ok(!Term.all[0].output.includes('stale'));
  control(first,'Minimize').click();
  assert.equal(first.hidden,true);assert.equal(root.hidden,false,'tray stays visible');assert.ok(!Socket.all[2].closed,'minimizing is not closing');
  Socket.all[2].onmessage({data:'background output'});assert.ok(Term.all[0].output.includes('background output'),'minimized session still receives output');
  manager.reveal();assert.equal(first.hidden,false);assert.equal(Socket.all.length,3,'revealing hidden terminals must preserve sockets');
  const mover=first.children[0].children[0],x=parseInt(first.style.left);mover.events.keydown({key:'ArrowLeft',preventDefault(){}});assert.equal(parseInt(first.style.left),x-10,'keyboard movement');
  const grip=first.children.at(-1);grip.events.keydown({key:'ArrowLeft',preventDefault(){}});assert.equal(parseInt(first.style.width),850,'keyboard resize');
  window.innerWidth=390;events.resize();assert.ok(parseInt(first.style.left)+parseInt(first.style.width)<=390,'resize stays on screen');
  tray.children.at(-1).click();assert.ok(first.hidden&&second.hidden);assert.equal(Socket.all.length,3);
  manager.open({id:'two'},'netns');assert.equal(root.hidden,false);
  control(second,'Close and disconnect session').click();
  assert.equal(Socket.all[1].closed,true);assert.equal(Term.all[1].disposed,true);assert.ok(!Socket.all[2].closed);
  manager.open({id:'three'},'cli');assert.equal(Term.all[2].options.fontSize,15,'new sessions inherit font preference');
  let tick,cleared=false,calls=0,updates=[];
  global.setInterval=fn=>{tick=fn;return 42;};global.clearInterval=()=>{cleared=true;};global.document={hidden:false};
  const logRoot=new Node('div');
  const logs=terminalWorkspace({root:logRoot,el,button,csrf:()=>'',language:()=> 'en',notice:()=>{},request:async()=>{calls++;return {output:'hello log'};},createLogView:()=>({paused:()=>false,update:v=>updates.push(v)})});
  const sockets=Socket.all.length;logs.open({id:'log-instance'},'logs');await Promise.resolve();await Promise.resolve();
  const logPanel=logRoot.children[0].children[0];assert.deepEqual(updates,['hello log']);assert.equal(Socket.all.length,sockets);
  control(logPanel,'Minimize').click();tick();await Promise.resolve();await Promise.resolve();assert.equal(calls,2);
  logs.open({id:'log-instance'},'logs');assert.equal(logRoot.children[0].children.length,1,'reopen reuses log window');
  control(logPanel,'Close and disconnect session').click();assert.ok(cleared,'closing log stops polling');
  console.log('Terminal and floating log session isolation tests passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
