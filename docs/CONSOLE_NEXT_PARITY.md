# Responsive console migration

Preview: `sas-console-next.service`, port 6001. Existing console on 5000 stays
available. Legacy console workflows are now represented in the new console;
the old service has deliberately not been retired or redirected. No runner
lifecycle or CLI contract changes are required by the UI migration.

Code ownership: `console_next/` is an independent application. Authentication,
runner transport, terminal bridges and vendor assets are in `console_shared/`.
See [Console Next deployment notes](../console_next/README.md). The service uses
`console_next.app:create_app()`; it does not construct the legacy application.

## Implemented paths

- Persistent navigation, filtered keyed lists, selection-safe polling.
- Persistent CLI, GDB and NetNS terminal across navigation.
- Task queue, terminal-state refresh, task results and native diff approval.
- Instance start from profile, stop, restart, delete record, cleanup, extension,
  configuration extraction/download and debugger helper controls.
- Smithproxy/Tuntom build, ref fetch, branch list/Attic, binary deletion;
  Smithproxy default extraction and rootfs preparation.
- Configuration import/edit using existing bundled CodeMirror, validation,
  copy, explicit preview approval/rejection, download and deletion.
- Test Drive start from binary, restart, extension, config RO/RW, extraction,
  dirty upgrade, delete, CLI and shell.
- Basic profile editor, binaries/config/bundle/network selection, rootfs,
  restart policy and optional TTL; program artifact import/upload.
- Wiring endpoint attachment/addressing/inventory, profile bindings and
  per-instance microservice/00-start controls.
- Active network-driver editor, firewall authorization/selectors/topology,
  global network settings.
- Certificate generation/import, Fabric endpoint creation, profile work files,
  QEMU catalogue and appliance export forms.
- Standalone instance form, configuration placeholders and runtime parameters.
- Test Drive file management/logs and configuration observer request.
- Administrator JSON catalogue (create/edit/disable/delete/reset password),
  own-password Preferences, session revocation and cross-process write lock.
- Independent terminal tabs, reconnect/font controls and separate terminal windows.
- Diagnostic sections, member PIDs/RSS, rootfs paths and debugger hints.
- Profile newer-build adoption; grouped build choices with build/code age.
- Configuration source filters and profile/application/state filters.
- Retired-driver warnings, exclusive Fabric package state, localized form labels.
- Live build logs and Test Drive logs; open read-only views refresh after tasks.
- Dirty-editor guards, revision-aware task completion and CodeMirror disposal.
- Usage links in configuration/profile editors and library details; binary
  usage intentionally lists profiles, not historical instances.
- Mobile drawer with an inert closed menu, backdrop and accessible expanded
  state; explicit row selection scrolls the narrow layout to its detail.
- Network-driver explanatory overlays: click or 2-second hover, no layout jump.

## Verification on 2026-10-08

- 245 Python tests completed successfully (3 skipped), JS syntax checks and six Node suites:
  model/filtering, row selection, terminal isolation, workflow payload contracts,
  diagnostic formatting and usage links/shared form labels.
- All 19 authenticated catalogue reads passed against Helmut's real runner.
- Empty test cable create/read/queued deletion, with no network endpoints.
- Temporary router profile create/read/update/delete.
- Isolated router spawn, readiness, live restart in the same namespace, stop,
  record deletion and profile cleanup. A second isolated run attached a temporary
  cable and applied `192.0.2.2/30` and `2001:db8:5a5::2/126` through SAS addressing;
  both were verified on the actual namespace interface. No external traffic.
  Endpoint release is asynchronous: the final cleanup waited for detachment.
- Native config preview, explicit approval, new copy, readback and deletion.
- Temporary CA and leaf certificate generation; Test Drive creation, extension,
  file upload, log read and config RO switch; plain and rootfs archive exports.
  Test Drive, export and certificate bundle removed after successful checks.
- All scratch objects cleaned up; original appliance member PIDs unchanged.
- Browser: whole-row navigation; simultaneous CLI and NetNS sessions; closing
  one session does not close the other; configuration editor, unchanged native
  preview and rejection; profile filter; French navigation and form labels.
- Independent headless browser: layouts at 1440, 1024 and 390 pixels; no document
  overflow; failed native validation retained the edited configuration; all
  18 menu catalogue pages loaded in CZ/EN/FR with no JavaScript page errors.
- Live polling preserved both the selected text and its original row node.
  Network help waited at least 1.9 seconds and did not change diagram height.
- Temporary browser-test admin accounts were deleted afterwards. The root-run
  fixture exposed an ownership regression in atomic admin JSON replacement;
  ownership was restored and writes now preserve the service owner for both
  the catalogue and lock file. A regression test covers this path.

Integration checks discovered and fixed a UI contract error: ordinary stopped
instances cannot be restarted in the same container. That action is now only
offered for running instances. Test Drive retains its separate restart action.
The runner and existing appliance were not restarted for these deployments.

## Acceptance scope and remaining limits

- Artifact import is covered by existing backend and route tests, not repeated
  against live immutable storage (no delete operation for safe test cleanup).
- Existing build/compiler implementations were not changed; a fresh full
  compilation was not run as part of this UI acceptance. Build/fetch task
  routing and form contracts are covered, and live catalogue/status reads pass.
- Legacy runtime/profile/config/network/Wiring/library form options were
  compared, including work files, native approval, rootfs variants, optional
  TTL, restart policies and source selectors. Retired or design-only drivers
  remain explicitly labelled; the UI does not claim to implement them.
- Native backend errors, technical identifiers, user-authored names/configs
  and logs retain their original text; navigation and form labels use CZ/EN/FR.
- QEMU remains a catalogue, as in the old console; this migration does not add
  a VM runtime. CLI parity is unchanged because the runner API is unchanged.

Persisted editor drafts are an optional UX extension, not a claim of parity.
Current drafts remain in the open dialog on failed requests/tasks; closing or
leaving a dirty editor asks for confirmation. Secrets are not cached in storage.

## Acceptance criteria

No polling-driven page reload, no discarded editor content on failed tasks,
no terminal teardown on route changes. Do not substitute raw JSON forms or
embedded legacy pages for feature parity. Keep authentication/CSRF and native
configuration approval enforced server-side. Migration completion requires
testing workflows against the real runner, not just endpoint mapping tests.
