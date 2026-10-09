const fs=require('node:fs'),assert=require('node:assert/strict');
(async()=>{
 const {filterNetworks:filter,networkUsageCount:count}=await import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync('console_next/static/next/address-inventory.js')).toString('base64'));
 const data=[{prefix:'10.0.0.0/8',version:4,usages:[],children:[{prefix:'10.2.0.0/24',version:4,usages:[{address:'10.2.0.1',segment_name:'Lab Cable',instance_id:'one'}],children:[]}]},{prefix:'2001:db8::/64',version:6,usages:[{address:'2001:db8::2',segment_name:'IPv6 Lab'}],children:[]}];
 assert.equal(count(data),2);assert.equal(count(filter(data,'','4')),1);assert.equal(filter(data,'','6')[0].version,6);
 assert.equal(count(filter(data,'LAB CABLE')),1);assert.equal(count(filter(data,'one')),1);assert.equal(count(filter(data,'10.0.0.0/8')),1);
 assert.deepEqual(filter(data,'missing'),[]);assert.equal(count(data),2,'filter does not mutate inventory');
 console.log('IPv4/IPv6 inventory search and usage counts passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
