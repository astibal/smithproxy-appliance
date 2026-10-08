const assert=require('node:assert/strict');
const fs=require('node:fs');
(async()=>{
  const code=fs.readFileSync('console_next/static/next/row-selection.js','utf8');
  const {bindRowSelection}=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'));
  const handlers={};const row={addEventListener:(name,callback)=>handlers[name]=callback};
  let count=0,selected=false;
  bindRowSelection(row,()=>count++,()=>selected);
  const cell={closest:()=>null};
  const click=(target=cell,button=0)=>handlers.click({target,button,defaultPrevented:false});
  assert.equal(row.tabIndex,0);
  click();assert.equal(count,1,'any non-interactive cell opens detail');
  selected=true;click();assert.equal(count,1,'drag selection does not open detail');
  selected=false;click({closest:()=>({})});assert.equal(count,1,'nested control owns its click');
  click(cell,2);assert.equal(count,1,'context menu does not activate row');
  let prevented=false;
  handlers.keydown({target:row,key:'Enter',preventDefault:()=>prevented=true});
  assert.equal(count,2);assert.equal(prevented,true);
  handlers.keydown({target:row,key:' ',preventDefault:()=>{}});assert.equal(count,3);
  handlers.keydown({target:cell,key:'Enter'});assert.equal(count,3);
  let focused='';const next={focus:()=>focused='next'},previous={focus:()=>focused='previous'};
  row.nextElementSibling=next;row.previousElementSibling=previous;
  row.parentElement={firstElementChild:previous,lastElementChild:next};
  for(const [key,expected]of [['ArrowDown','next'],['ArrowUp','previous'],['Home','previous'],['End','next']]){
    handlers.keydown({target:row,key,preventDefault:()=>{}});assert.equal(focused,expected);assert.equal(count,3);
  }
  row.nextElementSibling=null;
  handlers.keydown({target:row,key:'ArrowDown',preventDefault:()=>{}});assert.equal(count,3);
  focused='';handlers.keydown({target:row,key:'Home',ctrlKey:true});assert.equal(focused,'');
  console.log('Row selection tests passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
