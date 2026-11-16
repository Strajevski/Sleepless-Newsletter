# Release-source privacy cleanup

This report covers the prospective release source in the development working tree, not its old Git history, another extracted ZIP, or a production installation. Commit the reviewed changes before using git archive: an archive of the previous commit still contains the older material.

## Changes

| Area | Treatment |
| --- | --- |
| Historical mail inspection, archive preparation/backfill, and issue-specific image repair scripts | Removed from release paths. Exact originals and runnable relocated copies are retained locally under ignored private/. The relocated copies adjust only their application-root imports/output paths; they remain private operational tools, not rehearsal commands. |
| Installation-specific operating notes | Preserved locally under ignored private/docs. Public documentation now describes configurable installations rather than a person's Windows account, mailbox, or production drive. |
| Relay identity | Public relays.json is empty. The existing development relay mapping was copied to ignored relays.local.json, which takes precedence at runtime. relays.example.json contains a reserved fictional address. |
| Task XML | Removed the personal Windows account and production path. The account is an explicit installer placeholder, and paths are generic templates. Installers already replace account/interpreter/script/working-directory values before registration. |
| Test data | Replaced the operational newsletter address and relay address with reserved example addresses. Replaced personal-looking fixture names/IDs with fictional ones. Positive onboarding scenarios explicitly mock readiness for selected reserved fixtures; the real placeholder-address rejection remains tested and unchanged. |
| Trial launcher | Removed automatic production/NAS root and interpreter fallback. It reads this installation by default; another source must be selected explicitly with -DataRoot or CIRCUS_DATA_ROOT. |
| Local caches/design material | Ignore rules exclude Windows thumbnail caches, local workflow/design files awaiting separate review, private operational material, and private relay configuration. These files remain local. |

Real settings.json, roster.json, encrypted credentials, runtime archives, journals, notification state, and models/schedule choices were not changed. Production was not deployed, accessed for data migration, or reconfigured. Existing development relay behaviour is preserved by the private override.

## Checks

The review checks the candidate file list derived from tracked existing files plus intended non-ignored additions, excluding working-tree deletions. Text checks cover the known operational addresses/identifiers and non-example email domains. Higher-signal credential-format checks distinguish the existing dummy password/webhook fixtures from real stored secrets. Snapshot checks verify that private settings, roster, credentials, environments, runtime folders, private tools/notes, and local workflow/cache files are absent.

The retained sample screenshots and filler preview artifacts were not changed by this cleanup. The earlier static snapshot audit found filler content and no hidden image/text metadata in those screenshots. That assessment does not apply automatically to new or replaced artifacts.

Regression tests cover normal pipeline behaviour, private relay override precedence, rejection of malformed private relay overrides, local-only default trial access, and explicit selection of another installation. No real messages or scheduled-task changes are used for these checks.

## Validation result

The final prospective working-tree snapshot contains 114 files. The candidate scan found no matches for the flagged private identifiers, non-example email domains, or higher-signal credential patterns. Private configuration, tools, operational notes, runtime data, environments, and local caches/design files were absent from the inspected ZIP. Both retained screenshots are byte-identical to the versions reviewed in the earlier visual/metadata audit. The local relay loader was checked against the preserved mapping and returns the same effective configuration.

All 155 regression tests passed with no skips when the temporary Windows-junction fixture had the required permission. No real email/Discord messages, model generations, scheduled-task mutations, or production deployment were performed.

## Release boundary

Ignore rules do not remove old commits. The private development history still contains the former files/values and has not been rewritten or cleared for public release. Use the reviewed snapshot to initialise a fresh public repository, or separately audit/rewrite any history that would be published. Do not push the existing private branch merely because its latest source files have been cleaned.

Create a new rehearsal ZIP from the new reviewed commit, inspect its contents, and discard the older candidate for release purposes. The existing extracted rehearsal copy is not updated by changes to development. A clean source scan is evidence for this particular snapshot, not a substitute for the final artifact review and controlled installation rehearsal.
