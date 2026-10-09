const fs=require('node:fs'),assert=require('node:assert/strict');
(async()=>{
 const {formatRoutes,parseRoutes}=await import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync('console_next/static/next/route-fields.js')).toString('base64'));
 const routes=[{destination:'10.0.0.0/8',gateway:'192.168.1.1'},{destination:'::/0',gateway:'fe80::1'},{destination:'2001:db8::/64',gateway:''}];
 assert.deepEqual(parseRoutes(formatRoutes(routes),'invalid'),routes);
 assert.deepEqual(parseRoutes(' \n\t','invalid'),[]);
 assert.deepEqual(parseRoutes('  ::/0   fe80::1  \n','invalid'),[routes[1]]);
 assert.throws(()=>parseRoutes('default via extra','invalid'),/invalid/);
 assert.equal(formatRoutes(), '');
 console.log('Wiring route field round-trip tests passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
