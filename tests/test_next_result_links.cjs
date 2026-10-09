const fs=require('node:fs'),assert=require('node:assert/strict');
(async()=>{
 const {instanceResultLinks:links}=await import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync('console_next/static/next/result-links.js')).toString('base64'));
 const instance={id:'a/b',unit:'appliance.service',state:'running',alias:'lab-router'};
 assert.deepEqual(links(instance),[{id:'a/b',label:'lab-router',href:'#instances/a%2Fb'}]);
 assert.equal(links({assigned_instance:instance,spawned_instance:instance}).length,1);
 assert.deepEqual(links({id:'config-id',name:'Configuration'}),[]);
 assert.deepEqual(links(null),[]);assert.deepEqual(links({preview_id:'preview'}),[]);
 assert.equal(links({instance}).length,1);
 console.log('Canonical instance result links and non-instance exclusion passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
