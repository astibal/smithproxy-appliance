// Short messages never change the page geometry. Errors and queued work remain
// visible until dismissed/replaced; routine confirmations expire on their own.
export function feedback(root,{el,button,label}){
  const message=el('span',{class:'notice-message'});
  const link=el('a',{class:'notice-link',hidden:''});
  const dismiss=button('×',()=>hide());dismiss.setAttribute('aria-label',label());
  root.replaceChildren(el('div',{},message,link),dismiss);let timer=null;
  function hide(){clearTimeout(timer);root.hidden=true;}
  return {
    show(value,error=false){
      clearTimeout(timer);message.textContent=value;root.hidden=false;link.hidden=true;
      root.classList.toggle('error',error);root.removeAttribute('aria-busy');
      dismiss.setAttribute('aria-label',label());
      if(!error)timer=setTimeout(()=>{if(root.getAttribute('aria-busy')!=='true')hide();},8000);
    }, hide, text:()=>message.textContent,
    task(id,title){link.href='#tasks/'+encodeURIComponent(id);link.textContent=title;link.hidden=false;link.onclick=hide;},
  };
}

export async function copyText(value,{document:doc=document,navigator:nav=navigator}={}){
  if(nav.clipboard?.writeText){try{await nav.clipboard.writeText(value);return;}catch{/* HTTP/permission fallback below. */}}
  const focused=doc.activeElement,selection=doc.getSelection();
  const ranges=selection?Array.from({length:selection.rangeCount},(_,n)=>selection.getRangeAt(n).cloneRange()):[];
  const input=doc.createElement('textarea');input.value=value;input.readOnly=true;
  input.style.cssText='position:fixed;left:-9999px;top:0;opacity:0';doc.body.append(input);input.select();
  try{if(!doc.execCommand('copy'))throw Error('Clipboard unavailable');}
  finally{input.remove();focused?.focus({preventScroll:true});if(selection){selection.removeAllRanges();for(const range of ranges)selection.addRange(range);}}
}
