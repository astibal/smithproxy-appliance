(() => {
  const disks = document.querySelector('[data-disks]');
  const nics = document.querySelector('[data-nics]');
  if (!disks || !nics) return;
  const remove = `<button type="button" class="danger compact" data-remove>${window.sasTr('firewall.remove')}</button>`;
  const addDisk = () => {
    const index = disks.children.length;
    if (index >= 16) return;
    const row = document.createElement('div');
    row.className = 'network-profile-fields';
    row.innerHTML = `<label>${window.sasTr("ui.87772d80b490")}<input name="disk_source" required placeholder="router/system.qcow2"></label><label>Guest target<input name="disk_target" required value="vd${String.fromCharCode(97 + index)}" maxlength="16"></label><label>Bus<select name="disk_bus"><option>virtio</option><option>scsi</option><option>sata</option><option>ide</option></select></label><label>Role<select name="disk_role"><option value="${index ? 'data' : 'system'}">${index ? 'data' : 'system'}</option><option value="${index ? 'system' : 'data'}">${index ? 'system' : 'data'}</option></select></label>${remove}`;
    disks.append(row);
  };
  const addNic = () => {
    if (nics.children.length >= 16) return;
    const row = document.createElement('div');
    row.className = 'network-profile-fields';
    row.innerHTML = `<label>Model<select name="nic_model"><option>virtio-net-pci</option><option>e1000</option><option>e1000e</option><option>rtl8139</option></select></label><label>${window.sasTr("ui.689f4d2c1f4d")}<select name="nic_purpose"><option>dataplane</option><option>telemetry</option></select></label>${remove}`;
    nics.append(row);
  };
  document.querySelector('[data-add-disk]').addEventListener('click', addDisk);
  document.querySelector('[data-add-nic]').addEventListener('click', addNic);
  document.addEventListener('click', event => {
    if (event.target.matches('[data-remove]')) event.target.parentElement.remove();
  });
  addDisk();
})();
