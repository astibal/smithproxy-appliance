const fs=require('node:fs'),assert=require('node:assert/strict');
(async()=>{const {instanceFreshness:f}=await import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync('console_next/static/next/instance-freshness.js')).toString('base64'));
const item={build_id:'old',state:'running'},old={build_id:'old',ref:'master',build_type:'Release',commit_id:'a',commit_at:'2026-01-01'};
const status={artifacts:[old,{...old,build_id:'new',commit_id:'b',commit_at:'2026-02-01'}]};
assert.equal(f(item,status).kind,'build');assert.equal(f({...item,indicate_old_build:false},status),null);assert.equal(f({...item,state:'stopped'},status),null);
assert.equal(f(item,{artifacts:[old,{...status.artifacts[1],build_type:'Debug'}]}),null);
assert.equal(f(item,{artifacts:[old],refs:{branches:[{name:'master',commit_id:'b'}]}}).kind,'source');assert.equal(f(item,{}),null);console.log('Instance freshness matching and opt-out passed');})().catch(e=>{console.error(e);process.exitCode=1;});
