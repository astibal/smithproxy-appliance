// An explicit intent prevents a partially typed copy name from silently
// changing an update into a create operation.
export function configSavePayload({original,id='',mode='update',name,copyName='',content,buildId,builds,description}) {
  if(!buildId||!builds.some(b=>(b.build_id||b.commit_id)===buildId))throw Error('validator');
  const create=!id||mode==='copy';
  const selectedName=(id&&create?copyName:name||original.name||'').trim();
  if(!selectedName)throw Error(id&&create?'copyNameRequired':'nameRequired');
  return {name:selectedName,content,build_id:buildId,description:description??original.description??'',profile:original.profile||'custom',action:create?'create':'update',config_id:create?'':id};
}
