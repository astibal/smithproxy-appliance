const fs=require('node:fs'),assert=require('node:assert/strict');
(async()=>{
 const {instanceEvents,instanceReferences}=await import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync('console_next/static/next/instance-context.js')).toString('base64'));
 assert.deepEqual(instanceEvents({}),[]);
 assert.deepEqual(instanceEvents({created_at:'invalid',deadline:'2030-01-01'}),[]);
 const events=instanceEvents({created_at:'2026-01-01',last_restart_at:'2026-01-03',crash_at:'2026-01-02'});
 assert.deepEqual(events.map(e=>e.kind),['restart','crash','created']);
 assert.deepEqual(instanceReferences({build_id:'active',config_id:'',runtime_profile_id:'p'}),[{key:'runtime_profile_id',resource:'profiles',id:'p'}]);
 assert.equal(instanceReferences({secret:'hidden'}).length,0);
 console.log('Instance references and evidence-only milestones passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
