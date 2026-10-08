export function taskDuration(task,now=Date.now()){
  const start=Date.parse(task.started_at||task.created_at||'');
  const finished=['succeeded','failed','cancelled'].includes(task.state);
  const end=finished?Date.parse(task.finished_at||''):now;
  if(!Number.isFinite(start)||!Number.isFinite(end))return '—';
  const seconds=Math.max(0,Math.floor((end-start)/1000));
  return [Math.floor(seconds/3600),Math.floor(seconds/60)%60,seconds%60].map(n=>String(n).padStart(2,'0')).join(':');
}
