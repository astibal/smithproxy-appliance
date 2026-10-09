const fs=require('node:fs'),assert=require('node:assert/strict');
(async()=>{
 const {buildReferenceInfo:info}=await import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync('console_next/static/next/build-reference.js')).toString('base64'));
 const status={refs:{branches:[{name:'master',commit_id:'new'}]},artifacts:[{ref:'master',build_type:'Release',commit_id:'old'},{ref:'master',build_type:'Debug',commit_id:'new'}]};
 assert.equal(info(status,'master','Release').built,false);assert.equal(info(status,'master','Release').previous,1);
 assert.equal(info(status,' master ','Debug').built,true);assert.equal(info(status,'a44129e7','Release'),null);
 assert.equal(info({},'master','Release'),null);
 assert.equal(info({...status,artifacts:[]},'master','Release').previous,0);
 console.log('Build branch head and variant-aware availability tests passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
