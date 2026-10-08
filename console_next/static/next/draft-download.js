// Explicit local export only. Draft contents never enter localStorage, the
// runner or a background autosave; native validation/approval remains required.
export function draftFilename(name){
  const stem=String(name||'config').replace(/\.cfg$/i,'').replace(/[^\p{L}\p{N}._-]+/gu,'-').slice(0,80)||'config';
  return `sas-${stem}-draft.cfg`;
}
export function downloadDraft(content,name){
  const url=URL.createObjectURL(new Blob([content],{type:'text/plain;charset=utf-8'}));
  const link=document.createElement('a');link.href=url;link.download=draftFilename(name);
  document.body.append(link);
  try{link.click();}finally{link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
}
