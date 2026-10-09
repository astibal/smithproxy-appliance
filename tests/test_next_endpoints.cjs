const fs=require('node:fs'),assert=require('node:assert/strict');
class Element {
 constructor(tag,attrs={},...children){this.tag=tag;this.attrs=attrs;this.children=[];this.textContent='';this.append(...children);}
 append(...nodes){for(const node of nodes){if(typeof node==='string'){this.textContent=node;continue;}node.remove?.();node.parent=this;this.children.push(node);}}
 remove(){if(this.parent){this.parent.children=this.parent.children.filter(n=>n!==this);this.parent=null;}}
 insertBefore(node,before){node.remove();node.parent=this;const index=this.children.indexOf(before);if(index<0)this.children.push(node);else this.children.splice(index,0,node);}
}
(async()=>{
 const {endpointList}=await import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync('console_next/static/next/endpoint-list.js')).toString('base64'));
 const el=(...args)=>new Element(...args),button=(label,click)=>Object.assign(el('button',{},label),{click});let clicked;
 const root=el('div'),render=endpointList(root,{el,button,t:k=>k,onAddress:(segment,ep)=>clicked=ep,onDetach(){}});
 const ep={id:'one',interface:'cable0',instance_id:'instance',type:'veth',state:'attached',addressing:{desired:{addresses:['10.0.0.1/24']}}};
 render({endpoints:[ep]});const card=root.children[0],control=card.children.at(-2);
 const updated={...ep,addressing:{desired:{addresses:['10.0.0.2/24']}}};render({endpoints:[updated]});
 assert.equal(root.children[0],card);assert.equal(card.children.at(-2),control);control.click();assert.equal(clicked,updated,'click uses current endpoint, not captured stale data');
 assert.ok(card.children.at(-3).textContent.includes('10.0.0.2/24'));
 render({endpoints:[{...ep,id:'two'},updated]});assert.equal(root.children[1],card);render({endpoints:[updated]});assert.equal(root.children[0],card);
 render({endpoints:[]});assert.equal(root.children.length,0);console.log('Keyed endpoint cards preserve controls and use latest state');
})().catch(e=>{console.error(e);process.exitCode=1;});
