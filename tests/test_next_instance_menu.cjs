const assert=require('node:assert/strict');
const fs=require('node:fs');
(async()=>{
 const {instanceMenu}=await import('data:text/javascript;base64,'+fs.readFileSync('console_next/static/next/instance-menu.js').toString('base64'));
 const el=(tag,attrs={},...children)=>({tag,attrs,children,open:false,events:{},classList:{add(){}},append(x){this.children.push(x)},addEventListener(k,f){this.events[k]=f},querySelectorAll(){return this.children},focus(){this.focused=true}});
 for(const language of ['cs','en','fr']){
  const root=el('div'),group=instanceMenu(root,{el,language});
  assert.equal(group('runtime'),group('runtime'));
  group('console');assert.equal(root.children.length,2);
  const [first,second]=root.children;first.open=true;second.open=true;second.events.toggle();assert.equal(first.open,false);
  second.events.keydown({key:'Escape',stopPropagation(){}});assert.equal(second.open,false);assert.equal(second.children[0].focused,true);
  first.open=true;first.children[1].events.click({target:{closest:()=>true}});assert.equal(first.open,false);
 }
 const source=fs.readFileSync('console_next/static/next/workspace.js','utf8');
 assert.ok(source.includes("querySelector('.actions').after(output)"));
 assert.ok(source.includes('if(reveal){'));assert.ok(source.includes('inspectRequest(item,path,true)'));
 console.log('Instance action groups, dismissal, translations and explicit result reveal passed');
})();
