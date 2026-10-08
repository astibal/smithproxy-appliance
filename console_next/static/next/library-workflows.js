import {buildChoices} from './model.js';
import {logView} from './log-view.js';
import {copyText} from './feedback.js';
export function libraryWorkflows({el,button,request,action,fetchItems,identity,notice,dialog,field,commit,language,setCsrf}) {
  const words={
    exclusive:['Balíček může patřit právě jedné instanci. Obsazený balíček nejde znovu použít ani smazat.','A package belongs to exactly one instance. A claimed package cannot be reused or deleted.','Un paquet appartient à une seule instance. Un paquet réservé ne peut être réutilisé ni supprimé.'],
    checkServices:['Zkontrolovat mikroslužby','Check microservices','Vérifier les microservices'],logs:['Logy','Logs','Journaux'],
    save:['Uložit','Save','Enregistrer'],account:['Administrátor','Administrator','Administrateur'],
    enabled:['Aktivní','Active','Actif'],disabled:['Deaktivovaný','Disabled','Désactivé'],status:['Stav účtu','Account status','État du compte'],
    optionalPassword:['Nové heslo · prázdné = beze změny','New password · empty = unchanged','Nouveau mot de passe · vide = inchangé'],
    name:['Název','Name','Nom'],create:['Vytvořit','Create','Créer'],remove:['Smazat','Delete','Supprimer'],
    generate:['Generovat certifikát','Generate certificate','Générer un certificat'],import:['Importovat','Import','Importer'],
    days:['Platnost (dny)','Validity (days)','Validité (jours)'],file:['Soubor','File','Fichier'],
    work:['Soubory /work','Files in /work','Fichiers /work'],refresh:['Obnovit','Refresh','Actualiser'],
    path:['Relativní cesta','Relative path','Chemin relatif'],download:['Stáhnout','Download','Télécharger'],
    observer:['Porovnat s defaultem buildu','Compare with build default','Comparer à la configuration du build'],
    password:['Změnit heslo','Change password','Changer le mot de passe'],current:['Současné heslo','Current password','Mot de passe actuel'],new:['Nové heslo (min. 12 znaků)','New password (12 characters minimum)','Nouveau mot de passe (12 caractères minimum)'],confirm:['Zopakovat nové heslo','Confirm new password','Confirmer le nouveau mot de passe'],
  };
  const t=k=>(words[k]||[k,k,k])[Math.max(0,['cs','en','fr'].indexOf(language()))];
  const choices=items=>items.map(i=>[identity(i),i.name||`${i.ref} · ${i.build_type} · ${identity(i).slice(0,12)}`]);
  async function readFile(control,limit,base64=false) {
    const file=control.files[0];if(!file)throw Error(t('file'));if(file.size>limit)throw Error(`Maximum ${limit} bytes`);
    if(!base64)return {file,content:await file.text()};
    const data=new Uint8Array(await file.arrayBuffer());let value='';for(let n=0;n<data.length;n+=8192)value+=String.fromCharCode(...data.subarray(n,n+8192));return {file,content:btoa(value)};
  }
  function bundle(item=null,imported=false) {
    const w=dialog(item?(imported?t('import'):t('generate')):t('create')+' CA');
    const name=field(w,t('name'));
    if(imported){const file=field(w,t('file'),'',null,'file'),filename=field(w,'Filename');
      commit(w,'certificates','certificate',identity(item),async()=>{const value=await readFile(file,256*1024);return {action:'import',name:name.value||value.file.name,filename:filename.value||value.file.name,content:value.content};});
    }else{const cn=field(w,'Common name'),days=field(w,t('days'),item?825:3650,null,'number'),stem=item?field(w,'File stem'):null;
      commit(w,'certificates',item?'certificate':'create',identity(item||{}),()=>({...(item?{action:'generate',file_stem:stem.value}:{}),name:name.value,common_name:cn.value,days:Number(days.value)}));
    }
  }
  function endpoint() {
    const w=dialog('Fabric endpoint');const controls={};
    w.body.append(el('p',{class:'muted'},t('exclusive')));
    for(const key of ['name','package_id','fabric_port_id','switch_ip','tunnel_id','secret'])controls[key]=field(w,key.replaceAll('_',' '),'',null,key==='secret'?'password':'text');
    commit(w,'endpoints','create','',()=>({kind:'tuntom-via',...Object.fromEntries(Object.entries(controls).map(([k,c])=>[k,c.value]))}));
  }
  async function exportAppliance() {
    const w=dialog('Appliance export');try{const [builds,configs]=await Promise.all([fetchItems('binaries'),fetchItems('configs')]);if(!w.d.isConnected)return;
      const native=configs.filter(c=>c.native);
      const name=field(w,t('name')),build=field(w,'Build',identity(builds[0]||{}),buildChoices(builds,language())),config=field(w,'Config',identity(native[0]||{}),choices(native)),mode=field(w,'Filesystem','rootfs',[['rootfs','Rootfs'],['plain','Plain']]);
      const dynamic=el('div',{class:'workflow-fields'});w.body.append(dynamic);let placeholders=[];
      config.onchange=()=>{const previous=new Map(placeholders.map(([key,c])=>[key,c.value]));dynamic.replaceChildren();placeholders=(configs.find(c=>identity(c)===config.value)?.placeholders||[]).map(key=>[key,field({body:dynamic},`{{${key}}}`,previous.get(key)||'')]);};
      config.onchange();
      commit(w,'exports','create','',()=>({name:name.value,build_id:build.value,config_id:config.value,filesystem_mode:mode.value,parameters:Object.fromEntries(placeholders.map(([key,c])=>[key,c.value]))}));
    }catch(e){w.status.textContent=e.message;}
  }
  async function files(item,embedded=null) {
    const w=embedded||dialog(t('work'));const list=el('div');w.body.append(list);
    const path=field(w,t('path')),mode=field(w,'Mode','0600',['0600','0640','0644','0700','0750','0755'].map(v=>[v,v])),file=field(w,t('file'),'',null,'file');
    const refresh=async()=>{try{const profile=await request(`/next-api/detail/profiles/${identity(item)}/profile`);if(!w.d.isConnected)return;
      list.replaceChildren(...(profile.work_files||[]).map(f=>el('div',{class:'branch-row'},el('code',{},`${f.path} · ${f.mode} · ${f.size??''}`),button(t('remove'),async()=>{if(!confirm(t('remove')+'?'))return;try{await action('profiles','delete-file',identity(item),{path:f.path});await refresh();}catch(e){w.status.textContent=e.message;}}))));
    }catch(e){w.status.textContent=e.message;}};
    commit(w,'profiles','upload-file',identity(item),async()=>{const data=await readFile(file,40*1024,true);return {path:path.value||data.file.name,mode:mode.value,content_base64:data.content};});
    w.watch(refresh);w.footer.append(button(t('refresh'),refresh));await refresh();
  }
  async function observer(item) {
    const w=dialog(t('observer'));try{const builds=await fetchItems('binaries');if(!w.d.isConnected)return;
      const build=field(w,'Build',item.normalized_build_id||identity(builds[0]||{}),buildChoices(builds,language()));
      commit(w,'configs','observe','',()=>({config_id:identity(item),build_id:build.value}));
    }catch(e){w.status.textContent=e.message;}
  }
  function password() {
    const w=dialog(t('password')),current=field(w,t('current'),'',null,'password'),next=field(w,t('new'),'',null,'password'),confirm=field(w,t('confirm'),'',null,'password');
    current.autocomplete='current-password';next.autocomplete=confirm.autocomplete='new-password';
    const save=button(t('password'),async()=>{save.disabled=true;try{const value=await request('/next-api/preferences',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({current_password:current.value,new_password:next.value,confirm_password:confirm.value})});setCsrf(value.csrf);current.value=next.value=confirm.value='';w.clean();w.status.textContent=value.message;}catch(e){w.status.textContent=e.message;}finally{save.disabled=false;}});
    w.footer.append(save);
  }
  async function admin(item=null) {
    const w=dialog(item?item.email:t('account'));
    try{const session=await request('/next-api/session');if(!w.d.isConnected)return;
      const own=item?.id===session.id,email=field(w,'Email',item?.email||'',null,'email');
      const password=own?null:field(w,t(item?'optionalPassword':'new'),'',null,'password');if(password)password.autocomplete='new-password';
      const disabled=own?null:field(w,t('status'),String(item?.disabled||false),[['false',t('enabled')],['true',t('disabled')]]);
      const save=button(t(item?'save':'create'),async()=>{save.disabled=true;try{
        await request('/next-api/admins'+(item?'/'+item.id:''),{method:item?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:email.value,...(password?.value?{password:password.value}:{}),...(disabled?{disabled:disabled.value==='true'}:{})})});
        if(password)password.value='';w.clean();w.d.close();w.d.remove();await fetchItems('admins');
      }catch(e){w.status.textContent=e.message;}finally{save.disabled=false;}});
      w.footer.append(save);
      if(item&&!own)w.footer.append(button(t('remove'),async()=>{if(!confirm(t('remove')+' '+item.email+'?'))return;try{await request('/next-api/admins/'+item.id,{method:'DELETE',headers:{'Content-Type':'application/json'},body:'{}'});w.clean();w.d.close();w.d.remove();await fetchItems('admins');}catch(e){w.status.textContent=e.message;}}));
    }catch(e){w.status.textContent=e.message;}
  }
  function qemu() {
    const w=dialog('QEMU image');const name=field(w,t('name')),description=field(w,'Description'),architecture=field(w,'Architecture','x86_64',[['x86_64','x86_64'],['aarch64','aarch64']]),machine=field(w,'Machine','q35',['q35','pc','virt'].map(v=>[v,v]));
    const disks=[],nics=[];
    const add=(kind)=>{
      const items=kind==='disk'?disks:nics;if(items.filter(r=>r.box.isConnected).length>=16)return;
      const box=el('fieldset'),row={box,controls:{}};w.body.append(box);const nested={body:box};
      const entry=(key,value='',options=null)=>row.controls[key]=field(nested,key,value,options);
      if(kind==='disk'){entry('source');entry('target',`vd${String.fromCharCode(97+disks.length)}`);entry('bus','virtio',['virtio','scsi','sata','ide'].map(v=>[v,v]));entry('role',disks.length?'data':'system',[['system','System'],['data','Data']]);}
      else{entry('model','virtio-net-pci',['virtio-net-pci','e1000','e1000e','rtl8139'].map(v=>[v,v]));entry('purpose','dataplane',[['dataplane','Dataplane'],['telemetry','Telemetry']]);}
      box.append(button('×',()=>{box.remove();w.touch?.();}));items.push(row);
    };
    w.body.append(el('div',{},button('+ Disk',()=>{add('disk');w.touch?.();}),button('+ NIC',()=>{add('nic');w.touch?.();})));add('disk');
    const enabled=field(w,'Forensic', 'true',[['true','✓'],['false','—']]),hash=field(w,'Hash','sha256',[['sha256','SHA256'],['sha512','SHA512']]);
    const artifacts=['disk-overlays','memory','pcap','qemu-log','serial-log','manifest'].map(key=>{const c=field(w,key,key==='serial-log'?'false':'true',[['true','✓'],['false','—']]);return [key,c];});
    const serialize=rows=>rows.filter(r=>r.box.isConnected).map(r=>Object.fromEntries(Object.entries(r.controls).map(([k,c])=>[k,c.value])));
    commit(w,'qemu','create','',()=>({name:name.value,description:description.value,architecture:architecture.value,machine:machine.value,disks:serialize(disks),nics:serialize(nics),forensic:{enabled:enabled.value==='true',hash:hash.value,artifacts:artifacts.filter(([,c])=>c.value==='true').map(([k])=>k)}}));
  }
  async function driveFiles(item) {
    const w=dialog(t('file'));const list=el('div');w.body.append(list);const path=field(w,t('path')),file=field(w,t('file'),'',null,'file');
    const refresh=async()=>{try{const value=await request(`/next-api/detail/test-drives/${identity(item)}/files`);if(!w.d.isConnected)return;
      list.replaceChildren(...value.files.map(f=>el('div',{class:'branch-row'},el('code',{},`${f.path} · ${f.size}`),el('a',{class:'download-link',href:`/test-drives/${identity(item)}/files/download?path=${encodeURIComponent(f.path)}`},t('download')))));
    }catch(e){w.status.textContent=e.message;}};
    commit(w,'test-drives','upload-file',identity(item),async()=>{const data=await readFile(file,16*1024*1024,true);return {path:path.value||data.file.name,content_base64:data.content};});w.watch(refresh);w.footer.append(button(t('refresh'),refresh));await refresh();
  }
  async function driveLogs(item) {
    const w=dialog(t('logs')),output=el('div');w.body.append(output);
    const viewer=logView(output,{el,button,language,copy:value=>copyText(value).then(()=>notice('✓')).catch(e=>w.status.textContent=e.message)});
    let busy=false;
    const refresh=async()=>{if(busy||!w.d.isConnected||viewer.paused()||window.getSelection()?.toString())return;busy=true;
      try{const data=await request(`/next-api/detail/test-drives/${identity(item)}/logs`);if(w.d.isConnected&&!window.getSelection()?.toString()){
        viewer.update(data.output||'');
        w.status.textContent=new Date().toLocaleTimeString();
      }}catch(e){w.status.textContent=e.message;}finally{busy=false;}};
    const timer=setInterval(refresh,3500);w.d.addEventListener('close',()=>clearInterval(timer),{once:true});w.watch(refresh);
    w.footer.append(button(t('refresh'),refresh));await refresh();
  }
  function toolbar(resource,bar) {
    if(resource==='certificates')bar.append(button('+ CA',()=>bundle()));
    if(resource==='endpoints')bar.append(button(t('import'),endpoint));
    if(resource==='exports')bar.append(button(t('create'),exportAppliance));
    if(resource==='qemu')bar.append(button(t('import'),qemu));
    if(resource==='preferences')bar.append(button(t('password'),password));
    if(resource==='admins')bar.append(button('+ Admin',()=>admin()));
  }
  function details(resource,item,bar) {
    const id=identity(item);
    if(resource==='admins')bar.append(button(item.email,()=>admin(item)));
    if(resource==='certificates')bar.append(button(t('generate'),()=>bundle(item)),button(t('import'),()=>bundle(item,true)),el('a',{href:`/cert-bundles/${id}/ca.pem`,class:'download-link'},'CA.pem'));
    if(resource==='exports')bar.append(el('a',{href:`/appliance-exports/${id}/download`,class:'download-link'},t('download')));
    if(['certificates','endpoints','exports','qemu'].includes(resource)){const remove=button(t('remove'),()=>{if(confirm(t('remove')+'?'))action(resource,'delete',id).catch(e=>notice(e.message,true));});if(resource==='endpoints'&&item.state!=='available'){remove.disabled=true;remove.title=t('exclusive');}bar.append(remove);}
    if(resource==='configs')bar.append(button(t('observer'),()=>observer(item)));
    if(resource==='test-drives')bar.append(button(t('file'),()=>driveFiles(item)),button(t('logs'),()=>driveLogs(item)));
    if(resource==='instances')bar.append(button(t('checkServices'),()=>action(resource,'check-services',id).catch(e=>notice(e.message,true))),button('00-start',()=>{const w=dialog('00-start');const enabled=field(w,'Enabled',String(item.system_start_enabled!==false),[['true','✓'],['false','—']]);commit(w,resource,'system-start',id,()=>({enabled:enabled.value==='true'}));}));
  }
  return {toolbar,details,files};
}
