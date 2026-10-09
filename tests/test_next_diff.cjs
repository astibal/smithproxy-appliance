const fs=require('node:fs'),assert=require('node:assert/strict');
(async()=>{
 const {diffLines,diffView}=await import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync('console_next/static/next/diff-view.js')).toString('base64'));
 const lines=diffLines('--- old.cfg\n+++ new.cfg\n@@ -1,2 +1,2 @@\n unchanged\n-old\n+new\n\\ No newline at end of file');
 assert.deepEqual(lines.map(l=>l.kind),['header','header','hunk','context','removed','added','marker']);
 assert.equal(lines.filter(l=>l.kind==='added').length,1);assert.equal(lines.filter(l=>l.kind==='removed').length,1);
 assert.equal(diffLines('+<script>')[0].text,'+<script>');assert.equal(diffLines('')[0].kind,'context');
 const el=(tag,attrs={},...children)=>({tag,attrs,children,append(...nodes){this.children.push(...nodes);},setAttribute(k,v){this.attrs[k]=v;},classList:{values:new Set(),toggle(k){if(this.values.has(k)){this.values.delete(k);return false;}this.values.add(k);return true;}}});
 const button=(label,click)=>Object.assign(el('button',{},label),{click});let copied;
 const root=el('div');diffView(root,'--- old\n+++ new\n-old\n+<script>',{el,button,copy:async value=>{copied=value;}});
 const [bar,pre]=root.children;assert.equal(bar.children[0].children[0],'+1 added / −1 removed');
 bar.children[1].click();assert.equal(bar.children[1].attrs['aria-pressed'],'true');assert.ok(pre.classList.values.has('changes-only'));
 bar.children[2].click();assert.ok(pre.classList.values.has('wrap-lines'));await bar.children[3].click();assert.ok(copied.endsWith('+<script>'));
 assert.equal(pre.children.at(-1).children[0],'+<script>');
 console.log('Unified diff headers, counts and literal content tests passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
