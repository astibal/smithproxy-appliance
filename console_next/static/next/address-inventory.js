export function filterNetworks(nodes,query='',family='all') {
  const term=query.trim().toLocaleLowerCase();
  const match=value=>String(value??'').toLocaleLowerCase().includes(term);
  return nodes.flatMap(node=>{
    if(family!=='all'&&String(node.version)!==family)return [];
    const own=match(node.prefix),usages=(node.usages||[]).filter(u=>own||Object.values(u).some(match));
    const children=filterNetworks(node.children||[],own?'':term,family);
    return !term||own||usages.length||children.length?[{...node,usages,children}]:[];
  });
}
export function networkUsageCount(nodes){return nodes.reduce((n,node)=>n+(node.usages||[]).length+networkUsageCount(node.children||[]),0);}
