const assert=require('node:assert/strict');
const fs=require('node:fs');
const load=name=>import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync('console_next/static/next/'+name,'utf8')).toString('base64'));
class Element{
  constructor(tag,attrs={},...children){this.tag=tag;this.attrs=attrs;this.children=children;this.handlers={};this.scrollTop=0;this.scrollHeight=1000;this.clientHeight=200;this.textContent='';this.classes=new Set();this.classList={toggle:name=>{if(this.classes.has(name)){this.classes.delete(name);return false;}this.classes.add(name);return true;}};}
  append(...nodes){this.children.push(...nodes);}setAttribute(key,value){this.attrs[key]=value;}
  addEventListener(name,fn){this.handlers[name]=fn;}
}
(async()=>{
  const {orderItems,nextOrder}=await load('list-order.js');
  const {draftFilename}=await load('draft-download.js');
  assert.equal(draftFilename('sample.cfg'),'sas-sample-draft.cfg');
  assert.equal(draftFilename(''), 'sas-config-draft.cfg');
  assert.ok(!/[\\/\n\r'"]/.test(draftFilename('../bad\n"name\'.cfg')));
  const {taskDuration}=await load('task-time.js');
  assert.equal(taskDuration({state:'pending',created_at:'2026-10-08T00:00:00Z'},Date.parse('2026-10-08T00:01:02Z')),'00:01:02');
  assert.equal(taskDuration({state:'succeeded',created_at:'2026-10-08T00:00:00Z',started_at:'2026-10-08T00:01:00Z',finished_at:'2026-10-08T01:02:03Z'}),'01:01:03');
  assert.equal(taskDuration({state:'failed'}),'—');
  const data=[{id:'b',name:'item10',rss_bytes:100},{id:'a',name:'item2',rss_bytes:200},{id:'c',name:'item1'}];
  const columns=(r,i)=>[i.name];
  assert.equal(orderItems(data,null,'instances',columns),data);
  assert.deepEqual(orderItems(data,{column:0,direction:'asc'},'instances',columns).map(i=>i.id),['c','a','b']);
  assert.deepEqual(orderItems(data,{column:3,direction:'desc'},'instances',columns).map(i=>i.id),['a','b','c']);
  assert.deepEqual(data.map(i=>i.id),['b','a','c'],'sorting never mutates cache');
  assert.deepEqual(nextOrder(null,1),{column:1,direction:'asc'});
  assert.deepEqual(nextOrder({column:1,direction:'asc'},1),{column:1,direction:'desc'});
  assert.equal(nextOrder({column:1,direction:'desc'},1),null);
  const dates=[{id:'future',built_at:'2026-01-01'},{id:'missing'},{id:'older',built_at:'2025-01-01'}];
  assert.deepEqual(orderItems(dates,{column:3,direction:'asc'},'binaries',columns).map(i=>i.id),['older','future','missing']);
  const {logView}=await load('log-view.js');
  const el=(...args)=>new Element(...args),button=(label,click)=>Object.assign(el('button'),{textContent:label,click});
  const root=el('div');let copied='';const view=logView(root,{el,button,copy:value=>copied=value,language:()=> 'en'});
  const [bar,status,pre]=root.children;
  view.update('first');assert.equal(pre.textContent,'first');assert.equal(pre.scrollTop,1000);
  pre.scrollTop=100;pre.handlers.scroll();view.update('second');assert.equal(pre.scrollTop,100,'reading position preserved');
  assert.equal(pre.textContent,'first','rolling log cannot replace the text being read');
  bar.children[0].click();assert.equal(view.paused(),true);view.update('third');assert.equal(pre.textContent,'first');
  bar.children[3].click();assert.equal(copied,'first');
  bar.children[0].click();view.update('third');assert.equal(pre.textContent,'first');
  bar.children[1].click();assert.equal(pre.textContent,'third');assert.equal(pre.scrollTop,1000);assert.equal(bar.children[1].attrs['aria-pressed'],'true');
  bar.children[2].click();assert.ok(pre.classes.has('wrap-lines'));
  view.update('');assert.equal(status.textContent,'Log is empty');
  const {copyText}=await load('feedback.js');
  let restored=false,removed=false,focused=false;const range={cloneRange:()=>range};
  const doc={activeElement:{focus:()=>focused=true},getSelection:()=>({rangeCount:1,getRangeAt:()=>range,removeAllRanges(){},addRange:r=>restored=r===range}),createElement:()=>({style:{},select(){},remove:()=>removed=true}),body:{append(){}},execCommand:()=>true};
  await copyText('test',{document:doc,navigator:{clipboard:{writeText:async()=>{throw Error('denied');}}}});
  assert.ok(restored&&removed&&focused,'HTTP clipboard fallback restores focus and selection');
  console.log('List ordering, log controls and clipboard UX tests passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
