const assert=require('node:assert/strict'),fs=require('node:fs');
(async()=>{
 const {instanceDetailGroups}=await import('data:text/javascript;base64,'+fs.readFileSync('console_next/static/next/instance-details.js').toString('base64'));
 const values={namespace:'ns',build_id:'abc',rootfs_path:'/root',members:[{pid:1}],new_field:42};
 for(const lang of ['cs','en','fr']){
  const groups=instanceDetailGroups(values,lang);
  assert.equal(Object.keys(groups).length,5);
  assert.deepEqual(Object.assign({},...Object.values(groups)),values);
 }
 assert.deepEqual(instanceDetailGroups({}),{});
 console.log('Instance detail grouping preserves all fields in CZ/EN/FR');
})();
