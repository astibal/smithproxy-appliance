export function instanceFreshness(item,status) {
  if(item.indicate_old_build===false||item.state!=='running'||(item.application||'smithproxy')!=='smithproxy')return null;
  const builds=status?.artifacts||[],build=builds.find(b=>(b.build_id||b.id)===item.build_id);
  if(!build)return null;
  const branch=build.ref||build.branch,type=build.build_type||'Release',stamp=Date.parse(build.commit_at);
  const newer=builds.filter(b=>(b.ref||b.branch)===branch&&(b.build_type||'Release')===type&&b.commit_id!==build.commit_id&&Date.parse(b.commit_at)>stamp).sort((a,b)=>Date.parse(b.commit_at)-Date.parse(a.commit_at))[0];
  if(newer)return {kind:'build',commit:newer.commit_id,branch,type};
  const head=status.refs?.branches?.find(b=>b.name===branch);
  if(head?.commit_id&&build.commit_id&&head.commit_id!==build.commit_id)return {kind:'source',commit:head.commit_id,branch,type};
  return null;
}
