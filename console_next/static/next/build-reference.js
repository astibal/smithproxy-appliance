export function buildReferenceInfo(status,ref,type) {
  const name=ref.trim(),branch=(status.refs?.branches||[]).find(b=>b.name===name);
  if(!branch)return null;
  const matching=(status.artifacts||[]).filter(b=>(b.ref||b.branch)===name&&(b.build_type||'Release')===type);
  return {branch,built:matching.some(b=>b.commit_id===branch.commit_id),previous:matching.length};
}
