const assert=require('node:assert/strict');
const fs=require('node:fs');
(async()=>{
  const code=fs.readFileSync('console_next/static/next/diagnostics.js','utf8');
  const {formatInterfaces,formatRoutes}=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'));
  assert.equal(formatInterfaces(), '');
  assert.match(formatInterfaces([{ifname:'di0',mtu:1500,operstate:'UP',addr_info:[{family:'inet6',local:'2001:db8::2',prefixlen:126,scope:'global'}]}]),/inet6 2001:db8::2\/126/);
  assert.equal(formatRoutes([{dst:'2001:db8::/32',gateway:'fe80::1',dev:'do0',metric:0}]),'2001:db8::/32 via fe80::1 dev do0 metric 0');
  assert.equal(formatRoutes([{dev:'do0'}]),'default dev do0');
  console.log('Diagnostic interface and route formatting tests passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
