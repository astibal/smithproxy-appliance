// Recognize instance records, not arbitrary objects containing an ID.
export function instanceResultLinks(data) {
  const values=[data,data?.instance,data?.assigned_instance,data?.spawned_instance];
  const seen=new Set(),links=[];
  for(const value of values){
    if(!value||typeof value.id!=='string'||!value.id||typeof value.unit!=='string'||!value.unit||typeof value.state!=='string'||seen.has(value.id))continue;
    seen.add(value.id);links.push({id:value.id,label:value.alias||value.name||value.id,href:'#instances/'+encodeURIComponent(value.id)});
  }
  return links;
}
