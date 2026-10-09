export function formatRoutes(routes=[]) {
  return routes.map(r=>[r.destination,r.gateway].filter(Boolean).join(' ')).join('\n');
}
export function parseRoutes(value,errorMessage) {
  return value.split('\n').map(line=>line.trim()).filter(Boolean).map(line=>{
    const parts=line.split(/\s+/);
    if(parts.length>2)throw Error(errorMessage);
    return {destination:parts[0],gateway:parts[1]||''};
  });
}
