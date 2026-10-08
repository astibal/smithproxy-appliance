export const identity = item => String(item.id || item.image_id || item.authorization_id || item.segment_id || item.network_profile_id || item.profile_id || item.artifact_id || item.build_id || item.config_id || item.bundle_id || item.package_id || item.export_id || item.task_id || item.commit_id || '');
export function completed(previous, tasks) {
  if (!previous) return false;
  return tasks.some(task => ['succeeded', 'failed', 'cancelled'].includes(task.state) && previous.get(task.task_id) !== task.state);
}
export function changed(previous, next) { return JSON.stringify(previous) !== JSON.stringify(next); }
export function filtered(items, query) {
  const normalize=value=>String(value).normalize('NFKD').replace(/\p{M}/gu,'').toLowerCase();
  const words = normalize(query).trim().split(/\s+/).filter(Boolean);
  if(!words.length)return items;
  // Search values, not JSON property names (e.g. "failed" must not match a
  // healthy item merely because it has a failed_count field).
  const values=value=>value==null?'':typeof value==='object'?Object.values(value).map(values).join(' '):String(value);
  return items.filter(item => {const haystack=normalize(values(item));return words.every(word=>haystack.includes(word));});
}
export function sortedBranches(branches, artifacts, now=Date.now()) {
  const built=new Set(artifacts.map(item=>item.ref));
  const stamp=b=>Date.parse(b.commit_at||b.commit_time||b.committed_at||b.commit_date||'')||0;
  return branches.map(branch=>({...branch,hasBuild:built.has(branch.name),attic:stamp(branch)>0&&now-stamp(branch)>365*86400000})).sort((a,b)=>Number(b.hasBuild)-Number(a.hasBuild)||stamp(b)-stamp(a)||a.name.localeCompare(b.name));
}
export function buildChoices(artifacts, locale='en', now=Date.now()) {
  const stamp=(b,key)=>Date.parse(b[key]||'')||0;
  const labels={cs:['nejnovější build','build','kód'],en:['latest build','build','code'],fr:['dernier build','build','code']}[locale]||['latest build','build','code'];
  const groups=new Map();
  for(const b of artifacts){const key=(b.ref||b.branch||'')+'\0'+b.build_type;const prev=groups.get(key);if(!prev||stamp(b,'built_at')>stamp(prev,'built_at'))groups.set(key,b);}
  const age=(b,key)=>stamp(b,key)?Math.max(0,Math.floor((now-stamp(b,key))/86400000))+' d':'?';
  return [...artifacts].sort((a,b)=>Number(Boolean(b.rootfs_ready))-Number(Boolean(a.rootfs_ready))||String(a.ref||a.branch||'').localeCompare(String(b.ref||b.branch||''))||String(a.build_type).localeCompare(String(b.build_type))||stamp(b,'built_at')-stamp(a,'built_at')||identity(a).localeCompare(identity(b))).map(b=>{
    const latest=groups.get((b.ref||b.branch||'')+'\0'+b.build_type)===b;
    return [identity(b),`${latest?'★ '+labels[0]+' · ':''}${b.ref||b.branch||'detached'} · ${b.build_type||''} · ${b.rootfs_ready?'rootfs ✓ · ':''}${labels[1]} ${age(b,'built_at')} · ${labels[2]} ${age(b,'commit_at')} · ${(b.commit_id||identity(b)).slice(0,12)}`];
  });
}
export function countdown(deadline, now=Date.now()) {
  const stamp=Date.parse(deadline||'');if(!Number.isFinite(stamp))return null;
  const seconds=Math.max(0,Math.ceil((stamp-now)/1000));
  return [Math.floor(seconds/3600),Math.floor(seconds/60)%60,seconds%60].map(n=>String(n).padStart(2,'0')).join(':');
}
export function buildFreshness(items){
  const groups=new Map(),key=b=>`${b.ref||b.branch||''}\0${b.build_type||'Release'}`,stamp=b=>Date.parse(b.commit_at||'')||0;
  for(const b of items){const prev=groups.get(key(b));if(!prev||stamp(b)>stamp(prev)||stamp(b)===stamp(prev)&&(Date.parse(b.built_at)||0)>(Date.parse(prev.built_at)||0))groups.set(key(b),b);}
  return items.map(b=>{const newest=groups.get(key(b));return {...b,newer_build_available:stamp(b)>0&&stamp(newest)>stamp(b),newest_build_id:identity(newest),newest_commit_id:newest.commit_id||identity(newest),code_behind_days:stamp(b)>0?Math.max(0,Math.floor((stamp(newest)-stamp(b))/86400000)):null};});
}
export function resourceScope(items, resource, scope='all') {
  if(scope==='all')return items;
  return items.filter(item=>{
    if(resource==='configs'){
      const kind=['active','preset'].includes(item.source_kind)?'builtin':['build','native-build-default'].includes(item.source_kind)?'build':'custom';
      return kind===scope;
    }
    if(resource==='profiles')return scope==='smithproxy'?(item.application||'smithproxy')==='smithproxy':item.application&&item.application!=='smithproxy';
    if(resource==='instances')return scope==='active'?['running','starting'].includes(item.state):scope==='problems'?item.orphaned||['failed','orphaned'].includes(item.state):['stopped','expired'].includes(item.state);
    if(resource==='tasks')return scope==='active'?['pending','running'].includes(item.state):scope==='problems'?item.state==='failed':['succeeded','failed','cancelled'].includes(item.state);
    return true;
  });
}
