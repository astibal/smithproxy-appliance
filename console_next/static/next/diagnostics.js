export function formatInterfaces(items=[]){return items.map(i=>`${i.ifname||'—'} · ${i.operstate||''} · MTU ${i.mtu??'—'}\n${(i.addr_info||[]).map(a=>`  ${a.family||''} ${a.local}/${a.prefixlen} · ${a.scope||''}`).join('\n')}`).join('\n');}
export function formatRoutes(items=[]){return items.map(r=>[r.dst||'default',r.gateway?'via '+r.gateway:'',r.dev?'dev '+r.dev:'',r.table?'table '+r.table:'',r.metric!=null?'metric '+r.metric:''].filter(Boolean).join(' ')).join('\n');}
export function renderDiagnostics(root,data,{el,button,copy,language}) {
  const labels={execution:['Běh a procesy','Execution and processes','Exécution et processus'],paths:['Cesty a izolace','Paths and isolation','Chemins et isolation'],binding:['Profily a síť','Profiles and networking','Profils et réseau'],network:['Rozhraní a routy','Interfaces and routes','Interfaces et routes'],crash:['Poslední pád','Last crash','Dernier plantage'],debug:['Remote GDB','Remote GDB','GDB distant']};
  const title=k=>labels[k][Math.max(0,['cs','en','fr'].indexOf(language))];
  const execution=data.execution||{},instance=data.instance||{};
  const net=execution.network||{},ingress=net.ingress||{},egress=net.egress||{};
  const link=i=>[i.host_interface,i.expected_host_address,i.expected_host_address_v6,'→',i.guest_interface,i.expected_guest_address,i.expected_guest_address_v6].filter(Boolean).join(' ');
  const restart=typeof instance.auto_restart==='object'&&instance.auto_restart?instance.auto_restart:{on_failure:instance.auto_restart};
  const groups={
    execution:{model:execution.model,unit:execution.unit,slice:execution.slice_unit,RSS:execution.slice_rss_bytes==null?'—':(execution.slice_rss_bytes/1048576).toFixed(1)+' MiB',auto_restart:[restart.on_exit?'on-exit':'',restart.on_failure?'on-failure':''].filter(Boolean).join(' + ')||'—',last_restart:instance.last_restart_at,...Object.fromEntries((execution.members||[]).map((m,n)=>[`${m.role||'process'} ${n+1}`,`PID ${m.pid||'—'} · ${m.state||''} · ${m.unit||''}`]))},
    paths:{rootfs:execution.rootfs,mode:execution.rootfs_mode,namespace:execution.network_namespace,namespace_path:execution.network_namespace_path,binary:execution.binary,runtime_dir:execution.runtime_dir,live_config:execution.live_config,snapshot:execution.saved_config_snapshot,private_run:execution.private_run},
    binding:{profile:instance.runtime_profile_id,build:instance.build_id,config:instance.config_id,config_mode:instance.config_mode,ingress:execution.network_profiles?.ingress?.name,egress:execution.network_profiles?.egress?.name,source:instance.source_ip,user:instance.user_id,deadline:instance.deadline,endpoint:execution.headless_endpoint?.package_id},
    network:{ingress_pool:[ingress.subnet,ingress.subnet_v6].filter(Boolean).join('\n'),ingress_host_to_namespace:ingress.enabled?link(ingress):'',ingress_host:formatInterfaces(ingress.host_interfaces),egress_pool:[egress.subnet,egress.subnet_v6].filter(Boolean).join('\n'),egress_host_to_namespace:egress.enabled?link(egress):'',egress_host:formatInterfaces(egress.host_interfaces),namespace_interfaces:formatInterfaces(net.namespace_interfaces),namespace_routes:formatRoutes(net.namespace_routes),namespace_routes_v6:formatRoutes(net.namespace_routes_v6),host_routes:formatRoutes(net.host_routes),policy:net.packet_mark?`table ${net.route_table} · fwmark ${net.packet_mark} · present ${net.present}`:'',transport_interfaces:formatInterfaces(net.transport?.interfaces),transport_routes:formatRoutes([...(net.transport?.routes||[]),...(net.transport?.routes_v6||[])])},
    crash:instance.crash_trace?{PID:instance.crash_pid,time:instance.crash_at,trace:instance.crash_trace}:{},
    debug:{build_type:execution.build_type,unit:execution.debug?.unit,ssh_tunnel:execution.debug?.ssh_tunnel,copy_binary:execution.debug?.copy_binary,gdb:execution.debug?.gdb_commands}
  };
  for(const [key,fields]of Object.entries(groups)){
    let section=root.querySelector(`[data-diag="${key}"]`);
    if(!section){section=el('section',{'data-diag':key},el('h3'),el('dl',{class:'summary-fields'}));root.append(section);}
    section.hidden=!Object.values(fields).some(value=>value!=null&&value!=='');
    section.querySelector('h3').textContent=title(key);const dl=section.querySelector('dl');
    for(const row of [...dl.children])if(!(row.dataset.key in fields))row.remove();
    for(const [name,value]of Object.entries(fields)){
      let row=[...dl.children].find(row=>row.dataset.key===name);
      if(value==null||value===''){row?.remove();continue;}
      if(!row){const control=button('',()=>copy(control.dataset.value));row=el('div',{'data-key':name},el('dt',{},name.replaceAll('_',' ')),el('dd',{},control));dl.append(row);}
      const control=row.querySelector('button'),content=String(value);control.dataset.value=content;if(control.textContent!==content)control.textContent=content;
    }
  }
}
