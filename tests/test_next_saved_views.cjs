const fs=require('node:fs'),assert=require('node:assert/strict');
const el=(tag,attrs={},...children)=>({tag,attrs,children,value:'',append(...v){this.children.push(...v)},replaceChildren(...v){this.children=v},focus(){}});
(async()=>{
 const {savedViews}=await import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync('console_next/static/next/saved-views.js')).toString('base64'));
 const data=new Map();let fail=false;global.localStorage={getItem:k=>data.get(k),setItem:(k,v)=>{if(fail)throw Error('quota');data.set(k,v)}};
 const setup=account=>{const toolbar=el('div'),view={search:{value:'lab'},scope:'active',sort:null};savedViews({toolbar,view,paint(){},el,button:(title,click)=>({...el('button',{},title),click}),language:'en',account});return toolbar;};
 const a=setup('admin-a'),editor=a.children[3],name=editor.children[0],save=editor.children[1];
 name.value='Lab';save.click();assert.ok(data.has('sas-next-views:admin-a'));assert.equal(a.children[0].children.length,2);
 assert.equal(setup('admin-b').children[0].children.length,1);
 name.value='Lab';save.click();assert.match(a.children[4].textContent,/already exists/);
 fail=true;name.value='Second';save.click();assert.equal(a.children[0].children.length,2);assert.equal(name.value,'Second');assert.match(a.children[4].textContent,/unchanged/);
 a.children[0].value='0';a.children[2].click();assert.equal(a.children[0].children.length,2);
 console.log('Saved view account isolation, duplicate and failed-write retention passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
