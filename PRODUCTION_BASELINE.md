# Production baseline — 2026-10-03

`nexa-guardian.service` runs Python module `nexa_guardian` from `/opt/projects/nexa-guardian/runtime/releases/20260910T234253Z-604ed20b3f5c`.

Its release record names Git revision `604ed20b3f5c201b497a04b9a840e55160a663b8`. The retained wheel SHA-256 is `d4a685f9e0baaa494084da7885b861c375a69392cb6b7666b9b18bf228868207`, matching the release record. All 24 package Python files in the wheel match the installed files, and all 24 installed Python files match GitHub main source.

No runtime application source update is needed for this verified scope. This does not assert production configuration, database state, dependency versions, or in-memory code equality. Those are outside this source comparison. No environment, credentials, database or runtime user data was read or copied; existing configuration examples remain templates. No service was restarted or deployed.

Main is preserved in `backup/main-before-production-baseline-20261003` at the revision above. `production-source-manifest.json` contains the source hash comparison and release verification.
