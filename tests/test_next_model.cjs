const assert = require('node:assert/strict');
const fs = require('node:fs');
(async()=>{
  const source=fs.readFileSync('console_next/static/next/model.js','utf8');
  const model=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
  assert.equal(model.identity({profile_id:'profile'}),'profile');
  assert.equal(model.completed(null,[{task_id:'old',state:'succeeded'}]),false);
  assert.equal(model.completed(new Map([['a','running']]),[{task_id:'a',state:'succeeded'}]),true);
  assert.equal(model.completed(new Map([['a','succeeded']]),[{task_id:'a',state:'succeeded'}]),false);
  assert.deepEqual(model.filtered([{name:'First',source_ip:'::1'},{name:'Second'}],'first ::1'),[{name:'First',source_ip:'::1'}]);
  const branches=model.sortedBranches([{name:'new',commit_time:'2026-10-01'},{name:'built',commit_time:'2026-09-01'},{name:'old',commit_time:'2020-01-01'}],[{ref:'built'}],Date.parse('2026-10-07'));
  assert.equal(branches[0].name,'built');
  assert.equal(branches[2].attic,true);
  const choices=model.buildChoices([
    {build_id:'old',ref:'master',build_type:'Release',built_at:'2026-10-01',commit_at:'2026-09-01'},
    {build_id:'new',ref:'master',build_type:'Release',built_at:'2026-10-06',commit_at:'2026-10-05'},
    {build_id:'image',ref:'zbranch',build_type:'Debug',built_at:'2026-10-02',rootfs_ready:true}
  ],'en',Date.parse('2026-10-07'));
  assert.equal(choices[0][0],'image');
  assert.equal(choices[1][0],'new');
  assert.match(choices[1][1],/latest build.*build 1 d.*code 2 d/);
  assert.ok(!choices[2][1].includes('latest build'));
  const freshness=model.buildFreshness([{build_id:'old-code-new-build',ref:'master',build_type:'Release',commit_at:'2026-09-01',built_at:'2026-10-08'},{build_id:'new-code',ref:'master',build_type:'Release',commit_at:'2026-10-01',built_at:'2026-10-02'},{build_id:'debug',ref:'master',build_type:'Debug',commit_at:'2026-09-01'}]);
  assert.equal(freshness[0].newer_build_available,true);assert.equal(freshness[0].newest_build_id,'new-code');assert.equal(freshness[1].newer_build_available,false);assert.equal(freshness[2].newer_build_available,false);
  assert.equal(model.countdown('',Date.parse('2026-10-07')),null);
  assert.deepEqual(model.resourceScope([{state:'running'},{state:'stopped'},{state:'failed'}],'instances','active'),[{state:'running'}]);
  assert.deepEqual(model.resourceScope([{state:'running',orphaned:true},{state:'failed'},{state:'expired'}],'instances','problems'),[{state:'running',orphaned:true},{state:'failed'}]);
  assert.deepEqual(model.resourceScope([{source_kind:'native-build-default'},{source_kind:'custom'}],'configs','build'),[{source_kind:'native-build-default'}]);
  assert.deepEqual(model.resourceScope([{application:'router'},{application:'smithproxy'},{}],'profiles','smithproxy'),[{application:'smithproxy'},{}]);
  assert.equal(model.countdown('2026-10-07T00:00:00Z',Date.parse('2026-10-07T00:00:01Z')),'00:00:00');
  assert.equal(model.countdown('2026-10-07T01:02:03Z',Date.parse('2026-10-07T00:00:00Z')),'01:02:03');
  const ui=fs.readFileSync('console_next/static/next/workspace.js','utf8');
  assert.ok(!ui.includes('location.reload('),'live updates must not reload document');
  assert.ok(ui.includes('selection()) return'),'text selection protects live view');
  console.log('Next UI model tests passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
