// Explicit allowlist: never expose runtime secrets or arbitrary profile data.
export function spawnSummary(profile,builds,configs,identity) {
  const build=builds.find(item=>identity(item)===profile.build_id);
  const config=configs.find(item=>identity(item)===profile.config_id);
  return {
    application:profile.application||'smithproxy',
    ...(profile.build_id?{build:build?`${build.ref||build.branch||'—'} · ${(build.commit_id||profile.build_id).slice(0,12)} · ${build.build_type||'Release'}`:profile.build_id}:{}),
    ...(profile.config_id?{configuration:config?.name||profile.config_id}:{}),
    filesystem:profile.filesystem_mode||'rootfs',
    ...(profile.rootfs_variant?{rootfs_variant:profile.rootfs_variant}:{}),
    ...(profile.program_settings?.artifact_id?{artifact_id:profile.program_settings.artifact_id}:{}),
  };
}
