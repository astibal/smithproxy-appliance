const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
function moduleURL(file){return 'data:text/javascript;base64,'+Buffer.from(fs.readFileSync(file,'utf8').replace(/from ['"]\.\/([^'"]+)['"]/g,(_,name)=>`from '${moduleURL(path.join(path.dirname(file),name))}'`)).toString('base64');}
class Node{
  constructor(tag,attrs={},...children){this.tagName=tag.toUpperCase();this.attrs=attrs;this.dataset={};this.children=[];this.textContent='';for(const [k,v]of Object.entries(attrs))if(k.startsWith('data-'))this.dataset[k.slice(5)]=v;this.append(...children);}
  append(...children){for(const child of children){if(typeof child==='string'){this.textContent+=child;continue;}if(child.parentElement)child.remove();child.parentElement=this;this.children.push(child);}}
  remove(){if(this.parentElement){this.parentElement.children=this.parentElement.children.filter(c=>c!==this);this.parentElement=null;}}
  get isConnected(){return Boolean(this.parentElement);}
}
(async()=>{
  const {renderFields,detailLabel,protectedField,argumentFields}=await import(moduleURL('console_next/static/next/detail-fields.js'));
  const el=(...args)=>new Node(...args),button=(label,click)=>Object.assign(el('button',{},label),{click});
  const root=el('section'),copied=[];
  const options={el,button,language:'en',copy:v=>copied.push(v)};
  renderFields(root,{source:'2001:db8::1',chains:['input','forward'],private_key:'DO NOT SHOW',enabled:true},options);
  const source=root.children[0],group=root.children[1];group.open=false;
  assert.equal(source.children[1].textContent,'2001:db8::1');source.children[1].click();assert.deepEqual(copied,['2001:db8::1']);
  assert.equal(root.children[2].children[1].textContent,'Protected value');assert.equal(root.children[3].children[1].textContent,'Yes');
  renderFields(root,{source:'2001:db8::2',chains:['input','forward'],private_key:'HIDDEN',enabled:false},options);
  assert.equal(root.children[0],source);assert.equal(root.children[1],group);assert.equal(group.open,false);assert.equal(source.children[1].textContent,'2001:db8::2');
  renderFields(root,{source:'plain'},options);assert.equal(root.children.length,1);
  assert.equal(detailLabel('source','cs'),'Zdroj');assert.equal(protectedField('has_private_key'),true);
  renderFields(root,{has_private_key:true,has_secret:false,private_key:'hidden',has_token:'must remain hidden'},options);
  assert.deepEqual(root.children.map(row=>row.children[1].textContent),['Yes','No','Protected value','Protected value']);
  const body=el('div');let touches=0;const args=argumentFields(body,['a b','','--literal=$x'],{el,button,touch:()=>touches++});
  assert.deepEqual(args.read(),['a b','','--literal=$x']);args.root.children.at(-1).click();assert.equal(touches,1);assert.deepEqual(args.read(),['a b','','--literal=$x','']);
  console.log('Structured fields, redaction, keyed updates and literal argument tests passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
