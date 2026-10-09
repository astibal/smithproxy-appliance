const fs=require('node:fs'),assert=require('node:assert/strict');
(async()=>{
 const {certificateValidity:state,certificateCard}=await import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync('console_next/static/next/certificate-info.js')).toString('base64'));
 const now=Date.UTC(2026,9,8),cert={notbefore:'Oct  1 00:00:00 2026 GMT',notafter:'Oct 20 00:00:00 2026 GMT'};
 assert.deepEqual(state(cert,now),{state:'soon',days:12});assert.equal(state({...cert,notafter:'Oct  8 00:00:00 2026 GMT'},now).state,'expired');
 assert.equal(state({...cert,notbefore:'Oct  9 00:00:00 2026 GMT'},now).state,'future');assert.equal(state({...cert,notafter:'Oct 20 00:00:00 2027 GMT'},now).state,'current');assert.equal(state({},now).state,'unknown');
 const el=(tag,attrs={},...children)=>({tag,attrs,children,append(...nodes){this.children.push(...nodes);},setAttribute(k,v){this.attrs[k]=v;}}),button=(label,click)=>Object.assign(el('button',{},label),{click});const copied=[];
 const card=certificateCard({...cert,sha256:'file',sha256_fingerprint:'certificate'},{el,button,copy:v=>copied.push(v),now});
 const controls=card.children.filter(c=>c.tag==='button');controls.forEach(c=>c.click());assert.deepEqual(copied,['certificate','file']);assert.ok(controls[0].title.includes('fingerprint'));assert.ok(controls[1].title.includes('File'));
 console.log('Certificate dates and distinct certificate/file fingerprints passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
