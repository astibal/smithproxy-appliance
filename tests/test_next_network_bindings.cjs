const fs=require('node:fs'),assert=require('node:assert/strict');
(async()=>{
  const source=fs.readFileSync('console_next/static/next/network-bindings.js','utf8');
  const {networkChoices,bindNetworkSides}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
  const profiles=[{network_profile_id:'via',name:'Fabric',kind:'egress',consumes:['ingress','egress']},{network_profile_id:'in',name:'Input',kind:'ingress'},{network_profile_id:'out',name:'Output',kind:'egress'}];
  assert.deepEqual(networkChoices(profiles,'ingress').map(x=>x[0]),['via','in']);
  assert.deepEqual(networkChoices(profiles,'egress').map(x=>x[0]),['via','out']);
  const control=value=>({value,parentElement:{append(){}},setAttribute(){},addEventListener(event,fn){this.change=fn;}});
  const i=control(''),e=control('via');bindNetworkSides(i,e,profiles,{el:()=>({})});
  assert.equal(i.value,'via');assert.equal(i.disabled,true);assert.equal(e.disabled,false);
  e.value='out';e.change();assert.equal(i.value,'');assert.equal(i.disabled,false);
  i.value='via';i.change();assert.equal(e.value,'via');assert.equal(e.disabled,true);
  i.value='in';i.change();assert.equal(e.value,'');assert.equal(e.disabled,false);
  console.log('Duplex network profile selection and unlock tests passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
