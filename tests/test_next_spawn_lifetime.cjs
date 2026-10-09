const fs=require('node:fs'),assert=require('node:assert/strict');
(async()=>{
 const {lifetimeHint}=await import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync('console_next/static/next/spawn-lifetime.js')).toString('base64'));
 assert.match(lifetimeHint(null),/Empty or 0/);
 assert.match(lifetimeHint({ttl_seconds:null}),/Inherited.*no time limit/);
 assert.match(lifetimeHint({ttl_seconds:3600}),/3600 s/);
 assert.match(lifetimeHint({}),/1800 s/);
 assert.match(lifetimeHint({ttl_seconds:null},'cs'),/bez časového limitu/);
 assert.match(lifetimeHint({ttl_seconds:null},'fr'),/sans limite/);
 console.log('Profile lifetime inheritance and localization passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
