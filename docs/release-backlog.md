# Release follow-up backlog

The first automated release uses the minimum activation checks in
[releases.md](releases.md). The following work is deferred at the maintainer's
request. An unchecked item records missing evidence, not a passing result. Known
secret leaks or serious security issues remain publication blockers.

| ID | Priority | Follow-up | Completion evidence |
| --- | --- | --- | --- |
| REL-1 | High | Installed startup on clean Intel macOS, Windows x64 and Linux x64 environments | GUI launch and installed native self-test without development Python, Node or FFprobe; queue survives reopen |
| REL-2 | High | Actual version-to-version upgrades on all supported platforms | Signed newer version installs/relaunches; queue survives; busy work blocks installation; signature, network, installer and restart failures remain retryable |
| REL-3 | High | Native WebView isolation checks | Remote pages cannot invoke local commands; CSP and private-network/subresource behavior checked in each native WebView |
| REL-4 | High | Complete component notice/source review | Inventory actual native libraries and compiler runtimes; verify matching notices and redistribution materials for each platform |
| REL-5 | High | Independent Windows installer and AppImage inspection | Decode NSIS payload/installed tree and independently inspect AppImage contents; record links, ownership, permissions and unexpected files |
| REL-6 | Medium | Supported OS baseline and installer behavior | Windows baseline verified; Debian elevation/cancellation and AppImage replacement/relaunch exercised |
| REL-7 | Medium | Platform signing and distribution experience | Review Apple Developer ID/notarization and Windows code-signing options; record what is actually signed and verified |
| REL-8 | Medium | Repository release governance | Review branch protection, required reviews and release-tag rules without blocking the intended automation |
| REL-9 | Medium | Privacy and process isolation review | Review persisted queue/query/destination data and inherited runtime environment; obtain approval before retention, sandbox or network-policy changes |
| REL-10 | Medium | End-to-end release retry and next-patch exercise | Published retry makes no changes; subsequent source publishes the next patch; an installed app receives it through the public feed |

Use [release-validation.md](release-validation.md) for the detailed native test
procedure. Keep private logs and machine identifiers out of public evidence.

Existing evidence: the build-only run at source `b4f944c` passed all three native
builds with signing/publication skipped. Downloaded packages matched their
manifests. The macOS DMG passed mounted content/signature/native self-tests; its ZIP
and updater TAR passed decoded scans. Debian control/data archives passed decoded
content and ownership checks. These checks do not complete the installed tests
above, and that run does not validate later source changes.
