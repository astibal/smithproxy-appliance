const fs=require('node:fs'),assert=require('node:assert/strict');
(async()=>{
 const {configSavePayload:save}=await import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync('console_next/static/next/config-save.js')).toString('base64'));
 const input={original:{name:'Original',description:'Keep'},id:'cfg',mode:'update',name:'Original',copyName:'draft name',content:'edited',buildId:'valid',builds:[{build_id:'valid'}]};
 assert.equal(save(input).action,'update');assert.equal(save(input).name,'Original');assert.equal(save(input).config_id,'cfg');
 const copy=save({...input,mode:'copy'});assert.equal(copy.action,'create');assert.equal(copy.name,'draft name');assert.equal(copy.config_id,'');
 assert.throws(()=>save({...input,mode:'copy',copyName:' '}),/copyNameRequired/);
 assert.throws(()=>save({...input,buildId:'deleted'}),/validator/);
 assert.throws(()=>save({...input,buildId:''}),/validator/);
 assert.equal(save({...input,id:'',name:'Imported',description:'New'}).description,'New');
 assert.equal(save({...input,builds:[{commit_id:'valid'}]}).build_id,'valid');
 console.log('Explicit config save intent and validation build tests passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
