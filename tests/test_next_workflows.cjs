const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
function moduleURL(file){const source=fs.readFileSync(file,'utf8').replace(/from ['"]\.\/([^'"]+)['"]/g,(_,name)=>`from '${moduleURL(path.join(path.dirname(file),name))}'`);return 'data:text/javascript;base64,'+Buffer.from(source).toString('base64');}
class Element {
  constructor(tag,attrs={},...children){this.tag=tag;this.attrs=attrs;this.children=children;this.isConnected=true;this.dataset={};this.classList={add(){}};this.value='';}
  append(...children){this.children.push(...children);}replaceChildren(...children){this.children=children;}
  close(){}remove(){this.isConnected=false;}addEventListener(){}setAttribute(){}
  querySelectorAll(){return [];}
}
function harness(catalogues={}){
  const controls=[],commits=[],dialogs=[];
  const el=(...args)=>new Element(...args);
  const button=(label,click)=>Object.assign(el('button',{},label),{click});
  const api={el,button,language:()=> 'en',identity:i=>i.id||i.config_id||i.build_id||i.profile_id||'',notice(){},setCsrf(){},action:async()=>({}),
    fetchItems:async key=>catalogues[key]||[],request:async url=>url.endsWith('/session')?{id:'self'}:{work_files:[]},
    dialog:title=>{const w={title,d:el('dialog'),body:el('div'),footer:el('footer'),status:el('p'),clean(){},watch(){}};dialogs.push(w);return w;},
    field:(w,name,value='',choices=null,type='text')=>{const control={name,value,choices,type,files:[]};controls.push(control);return control;},
    commit:(w,resource,command,id,payload)=>commits.push({resource,command,id,payload})};
  return {api,controls,commits,dialogs,bar:el('div'),field:name=>controls.find(c=>c.name===name)};
}
(async()=>{
  const {libraryWorkflows}=await import(moduleURL('console_next/static/next/library-workflows.js'));
  let h=harness({binaries:[{build_id:'build',ref:'master',build_type:'Release'}],configs:[{config_id:'cfg',name:'Native',native:true,placeholders:['HOST']},{config_id:'legacy',name:'Legacy',native:false}]});
  libraryWorkflows(h.api).toolbar('exports',h.bar);await h.bar.children[0].click();
  h.field('Name').value='portable';h.field('{{HOST}}').value='example.invalid';
  let payload=await h.commits[0].payload();
  assert.deepEqual(payload.parameters,{HOST:'example.invalid'});assert.equal(payload.config_id,'cfg');assert.ok(!('template_values' in payload));
  assert.deepEqual(h.field('Config').choices,[['cfg','Native']]);

  h=harness();const library=libraryWorkflows(h.api);library.details('profiles',{profile_id:'p'},h.bar);
  assert.equal(h.bar.children.length,0,'profile files live inside the unified editor, not a separate launcher');
  const embedded=h.api.dialog('Profile');await library.files({profile_id:'p'},embedded);
  assert.equal(h.dialogs.length,1,'embedded files must not open another dialog');
  h.field('File').files=[{name:'fixture.bin',size:3,arrayBuffer:async()=>new Uint8Array([1,2,3]).buffer}];
  payload=await h.commits[0].payload();assert.deepEqual(payload,{path:'fixture.bin',mode:'0600',content_base64:'AQID'});

  h=harness();libraryWorkflows(h.api).details('admins',{id:'self',email:'self@example.invalid'},h.bar);await h.bar.children[0].click();
  assert.ok(!h.controls.some(c=>c.type==='password'),'own password must use Preferences');
  assert.ok(!h.controls.some(c=>c.name==='Account status'),'own deactivation must not be offered');

  const {firewallWorkflows}=await import(moduleURL('console_next/static/next/firewall-workflows.js'));
  h=harness({instances:[{id:'instance',state:'running'}],profiles:[{profile_id:'profile',name:'Profile'}]});
  firewallWorkflows(h.api).toolbar('firewall',h.bar);await h.bar.children[0].click();
  h.field('Source IP / CIDR').value='2001:db8::1/128';h.field('Protocol').value='tcp';h.field('Ports').value='443';h.field('TTL seconds · empty = unlimited').value='600';
  h.field('Instance').value='instance';h.field('Or start from profile').value='profile';h.field('Or start from profile').onchange();
  payload=await h.commits[0].payload();assert.equal(payload.instance_id,'');assert.equal(payload.runtime_profile_id,'profile');assert.equal(payload.ttl_seconds,600);assert.deepEqual(payload.chains,['input','forward']);assert.equal(payload.register_source,false);
  h=harness({instances:[{id:'bound',state:'running'},{id:'free',state:'running'},{id:'stopped',state:'stopped'}]});
  firewallWorkflows(h.api).details('firewall',{id:'auth',source:'2001:db8::1',instances:[{id:'bound'}]},h.bar);
  await h.bar.children[1].click();
  assert.deepEqual(h.field('Instance').choices,[['free','free']]);
  assert.equal(h.commits[0].id(),'free');assert.deepEqual(h.commits[0].payload(),{source:'2001:db8::1'});
  console.log('Workflow payload and self-protection tests passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
