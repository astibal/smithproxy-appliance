const fs=require('node:fs'),assert=require('node:assert/strict');
(async()=>{
 const {spawnSummary}=await import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync('console_next/static/next/spawn-summary.js')).toString('base64'));
 const id=x=>x.id;
 const result=spawnSummary({build_id:'b',config_id:'c',secret:'never'},[{id:'b',ref:'master',commit_id:'123456789012345',build_type:'Debug'}],[{id:'c',name:'Config'}],id);
 assert.equal(result.build,'master · 123456789012 · Debug');assert.equal(result.configuration,'Config');assert.equal(result.application,'smithproxy');assert.equal(result.secret,undefined);
 const elf=spawnSummary({application:'elf',program_settings:{artifact_id:'elf1',secret:'hidden'},rootfs_variant:'barebone'},[],[],id);
 assert.equal(elf.artifact_id,'elf1');assert.equal(elf.build,undefined);assert.equal(elf.rootfs_variant,'barebone');assert.ok(!JSON.stringify(elf).includes('hidden'));
 assert.equal(spawnSummary({build_id:'missing',config_id:'missing'},[],[],id).configuration,'missing');
 console.log('Spawn profile summary and secret exclusion tests passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
