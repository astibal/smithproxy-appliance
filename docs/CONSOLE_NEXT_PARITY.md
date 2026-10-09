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
- Sortable catalogue columns (ascending/descending/original), natural name
  ordering and numeric/date ordering where appropriate. Search reset, no-match
  states and keyboard traversal do not activate a row until Enter/Space.
  Search ignores diacritics and matches data values rather than JSON field names.
  An explicit notice identifies a selected detail excluded by the current filter.
- Resource-scoped connection health, explicit manual-refresh busy state, task
  history filters, active-first queue, durations and direct task links.
- Non-layout-shifting dismissible notifications. Errors stay visible; queued
  messages link to their task rather than only presenting an opaque ID.
- Read-only log controls: pause, follow tail, wrap and copy. Scrolling away
  freezes the visible snapshot even when the server's rolling log window moves.
- Expandable terminal area without reconnecting; remembered font size, linked
  accessible tabs and independent socket lifetimes.
- Unsaved-change indicators and sticky dialog controls. Wiring/QEMU row add and
  remove operations participate in draft protection, not only text input.
  Configuration drafts can be explicitly downloaded locally without a backend
  request; this does not bypass native validation/approval for library saves.
- Session recovery in place: sign in separately, explicitly resume with the same
  admin account, renew the CSRF token and retain drafts. Failed operations are
  never automatically replayed. API HTTP errors remain structured JSON.
- Collapsible desktop navigation remembers only that UI preference; mobile
  navigation remains independently controlled. No drafts or secrets are stored
  in browser persistence.

## Verification on 2026-10-08

- 252 Python tests completed successfully (3 skipped), JS syntax checks and seven Node suites:
  model/filtering, row selection, terminal isolation, workflow payload contracts,
  diagnostic formatting and usage links/shared form labels.
- UX regression coverage adds list ordering, log snapshot/follow behaviour,
  clipboard fallback focus/selection restoration, task durations and terminal
  expansion/font persistence.
- Browser UX acceptance: ascending/descending/original sort; catalogue failures
  cannot be masked by successful queue polling; manual refresh reports busy;
  copying does not shift page geometry; task links and failed-task filtering;
  live log pause/wrap; language changes with diagnostics already open; terminal
  expand/hide/reveal retains both CLI and NetNS sessions. No terminal commands
  are sent and the appliance's member PIDs are compared before/after.
- Real session-expiry acceptance removes only the temporary test browser's
  cookies, signs back into the temporary test account in a separate tab, resumes
  the existing form and verifies both its draft and the renewed CSRF token.
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

### Configuration, Wiring and detail UX (2026-10-08)

- The parity audit remains open: route coverage alone is not acceptance of
  every form field or state transition. The legacy console remains available.
- Configuration editors separate identity, content and validation/save intent.
  Updating the original and creating a copy are explicit choices. A missing
  validation build or copy name is rejected locally without losing the draft;
  the runner still performs native validation and requires diff approval.
- Native diffs distinguish file headers from added/removed content, show
  change counts, and support changes-only, wrapping and copying. These are
  presentation controls only; the approved preview is unchanged.
- Wiring address inventory supports IPv4/IPv6 filters, text search, usage
  counts, address copying and links to cables/instances. Refresh preserves
  expanded branches; search does not create an unsaved-edit warning.
- Profile network selectors respect a duplex profile's `consumes` metadata:
  selecting it binds both sides, and changing the owning side releases the
  other selector rather than leaving a hidden duplex binding.
- Instance/Test Drive details have a prominent runtime state and live TTL
  strip. Stopped records do not continue displaying an active countdown.
- Terminals explicitly select a monospace font with modest line spacing.
  Boolean secret-presence indicators remain visible; secret values remain
  redacted from generic detail rendering.
- Helmut browser acceptance covered explicit save modes, missing build/name
  validation, a real native preview and rejection without committing changes,
  editor-asset failure fallback, inventory filters/expansion, 390px layouts,
  and independent real CLI/NetNS connections across SPA navigation.
  Temporary browser accounts were deleted. No appliance service was restarted.

### Drawer editors and floating terminals (2026-10-08)

- Editors now slide in from the right, using the workspace width up to the
  sidebar. Mobile editors use the full width. Sticky headers/save controls and
  unsaved-change protection remain in place.
- The profile editor includes runtime settings, Wiring and `/work` files.
  Wiring is part of the profile save; file upload/removal remains an explicitly
  separate task in the same editor, preserving the existing API contract.
- CLI, NetNS, GDB and Test Drive terminal sessions use independent floating
  windows and a persistent tray above Tasks. Move/resize supports pointer and
  keyboard; maximize and minimize never replace sockets or xterm buffers.
- SPA navigation, language changes and minimizing preserve live connections.
  Explicit Close disconnects only that session. Reloading/closing the browser
  document still ends its connections and prompts before leaving live sessions.
- Verified against Helmut: desktop and 390px mobile editor/window geometry,
  profile dirty-close protection, two real terminal sockets across navigation,
  minimize/restore, move/resize, font changes and independent disconnect.
  Test accounts were removed; appliance services were not restarted.

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

### Task handoff audit (2026-10-08)

- Workflow dialogs show a direct task link immediately after enqueueing,
  rather than a generic validation message. The task opens separately so the
  current draft is not displaced.
- Terminal task states retain a task link; successful non-preview operations
  offer the existing Result viewer. Native preview approval is unchanged.
- Browser acceptance used real Helmut catalogues and browser-only intercepted
  task responses to exercise queued → failed → explicit retry → succeeded →
  result. Failure retained the selected profile and re-enabled submission.
  No real instance was created by this test; this is UI-state coverage, not
  renewed end-to-end runner lifecycle acceptance. All 19 Node suites passed.

No polling-driven page reload, no discarded editor content on failed tasks,
no terminal teardown on route changes. Do not substitute raw JSON forms or
embedded legacy pages for feature parity. Keep authentication/CSRF and native
configuration approval enforced server-side. Migration completion requires
testing workflows against the real runner, not just endpoint mapping tests.
