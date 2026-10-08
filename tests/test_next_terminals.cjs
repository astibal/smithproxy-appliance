const assert=require('node:assert/strict');
const fs=require('node:fs');
class Node {
  constructor(tag,attrs={},...children){this.tag=tag;this.attrs=attrs;this.children=children;this.dataset={};this.hidden=false;this.classes=new Set();this.classList={toggle:(name,on)=>on?this.classes.add(name):this.classes.delete(name)};}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  setAttribute(k,v){this.attrs[k]=v;}
  remove(){this.removed=true;}
  scrollIntoView(){this.scrolled=(this.scrolled||0)+1;}
  focus(){}
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
  global.window={Terminal:Term,FitAddon:{FitAddon:class{fit(){}}},addEventListener(){},open(){}};
  global.location={protocol:'http:',host:'test.invalid',href:'http://test.invalid/'};
  global.WebSocket=Socket;global.ResizeObserver=class{observe(){}disconnect(){}};
  global.requestAnimationFrame=fn=>fn();
  const preferences=new Map();global.localStorage={getItem:key=>preferences.get(key)||null,setItem:(key,value)=>preferences.set(key,value)};
  const source=fs.readFileSync('console_next/static/next/terminals.js','utf8');
  const {terminalWorkspace}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
  const root=new Node('section'),el=(...args)=>new Node(...args),button=(label,click)=>Object.assign(el('button',{},label),{click});
  const manager=terminalWorkspace({root,el,button,csrf:()=> 'token',language:()=> 'en',notice:()=>{}});
  manager.open({id:'one'},'cli');manager.open({id:'two'},'netns');
  root.children[1].children.find(n=>n.children?.[0]==='Expand').click();
  assert.ok(root.classes.has('terminal-maximized'));assert.equal(Socket.all.length,2,'expanding must not reconnect');
  root.children[1].children.find(n=>n.children?.[0]==='A+').click();
  assert.equal(Term.all[1].options.fontSize,15);assert.equal(preferences.get('sas-next-terminal-font'),'15');
  assert.equal(Socket.all.length,2);assert.ok(!Socket.all[0].closed,'opening second terminal must retain first connection');
  const scrolled=root.scrolled;manager.open({id:'one'},'cli');assert.equal(Socket.all.length,2,'selecting existing terminal must not reconnect');
  assert.equal(root.scrolled,scrolled+1,'existing terminal is brought back into view');
  Term.all[0].input('');Term.all[0].input('show status\r');
  assert.deepEqual(Socket.all[0].sent,['show status\r'],'empty input is never sent');
  root.children[1].children.find(n=>n.children?.[0]==='Reconnect').click();
  assert.equal(Socket.all.length,3);assert.equal(Socket.all[0].closed,true);assert.ok(!Socket.all[1].closed);
  Socket.all[0].onmessage({data:'stale'});assert.ok(!Term.all[0].output.includes('stale'));
  root.children[1].children.find(n=>n.children?.[0]==='Hide panels').click();
  assert.equal(root.hidden,true);assert.ok(!Socket.all[2].closed,'hiding is not closing');
  manager.reveal();assert.equal(root.hidden,false);assert.equal(Socket.all.length,3,'revealing hidden terminals must preserve sockets');
  manager.open({id:'two'},'netns');assert.equal(root.hidden,false);
  root.children[0].children[1].children[1].click();
  assert.equal(Socket.all[1].closed,true);assert.equal(Term.all[1].disposed,true);assert.ok(!Socket.all[2].closed);
  manager.open({id:'three'},'cli');assert.equal(Term.all[2].options.fontSize,15,'new sessions inherit font preference');
  console.log('Terminal session isolation tests passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
