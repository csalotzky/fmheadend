# Source-linked RDS profiles

RDS content/policy is an owned child, not a shared profile library. SQLite schema
**1** has `rds_profiles(id, data)` using the existing JSON record convention. Its
data contains only PI, ECC, PTY, TP, PS, AF, fallback RT, RT enabled/termination and
metadata-to-RT policy. Validation/defaults are defined once in `RdsProfileConfig`.

- An InputSource optionally owns one row through nullable `rds_profile_id`.
- Every physical Modulator owns its persistent override through
  `rds_override_profile_id`. The FK is nullable defensively; a missing effective
  override is an explicit configuration error.
- `rds_enabled` remains a physical Modulator/TX setting. `rds_follow_source` chooses
  Source content or Custom override. ANALOG must use Custom. Follow requires
  DIGITAL, an assigned Source and a Source profile, even when RDS/RF is disabled.
- Source/profile creation, updates and removal are one SQLite transaction. Profiles
  cannot be assigned by arbitrary IDs through the API. Normal operations never share
  owned rows or leave orphans. Startup validates ownership/references without repair.

Fresh databases create eight override rows: PI **FFF0..FFF7**, one PS item
**"TX 0".."TX 7"**, duration **5 seconds**, empty fallback RT and **RT disabled**. Other defaults are unchanged.
Follow is false initially. Generic profile defaults remain neutral (PI zero and
`FM TX`); they never derive identity from a Source ID. The single physical seed
in Database initialization replaces the old `FM TX <id>` seed. Source-editor
**Fill from source** fills only PS and fallback RT from the Source name in the
unsaved draft. It never generates or changes PI.

## API ownership and effective reads

Existing Source create/PATCH accepts nullable `rds_profile` with the content fields.
For PATCH, omitted means unchanged, null removes the owned profile, and an object
creates/updates it. Validation/persistence failure commits neither part of a mixed save.
Reads return the nested content and its read-only `rds_profile_id`. Modulator
PATCH/Save-and-Resume accepts `rds_override_profile` and `rds_follow_source`.
Reads return override content, its read-only ID, and the existing flat RDS fields
as **effective content**, resolved by `Database.effective_rds_profile`. Flat legacy
Modulator writes still edit the override; they never write a Source profile.
The Angular editor loads `rds_override_profile` and sends nested override data.
Legacy flat compatibility is asymmetric: reads can be inherited effective content,
but writes target Custom. Clients must not blindly echo effective flat fields into
a Custom update.

No standalone profile CRUD, names, library, or sharing API exists. Switching to
Follow retains the override exactly; switching back exposes it again. Adding a
Source profile does not automatically switch any persisted Modulator.

Removing Source RDS is rejected with HTTP **409 / RDS_PROFILE_FOLLOWED** if any
assigned TX follows it, including stopped/RDS-disabled TXs. The message lists TX
IDs and asks the user to switch them to Custom. No cloning or silent conversion
occurs. Referenced Source deletion remains forbidden; allowed deletion atomically
deletes its owned profile and Source.

## Apply behavior

The existing serialized control executor persists the complete request first.
Profile-only Source saves do not rebuild audio, including when an earlier audio
apply failed. Normal/mixed Source saves retain the current save/rebuild contract,
shared PCM output, consumers and explicit saved-but-not-applied failure response.
After persistence/audio apply, changed Source content targets only its followers.

Modulator override/Follow-only changes are hot, through PATCH or Save-and-Resume.
Hardware/source changes, including physical `rds_enabled`, retain the normal OFF
checks and Save-and-Resume lifecycle. Runtime enabled preferences/STOP ALL holds
are unchanged.

Hot apply invalidates the affected refill channel's content signature/pending
batch and uses the existing acknowledged **RDS_CLEAR -> scheduler rebuild/refill**
boundary. CLEAR failure/fencing remains authoritative; no RF enable/disable,
SET_TX_CONFIG or audio restart is issued for profile-only edits. Unrelated TX
state is untouched. Runtime RT overrides, Text A/B, carousel position, allocation,
pending batches and FIFO state remain per-TX. Encoder, character conversion,
quotas, lookahead and wire protocol are unchanged.

Effective RT remains: per-TX runtime override > enabled source metadata > effective
profile fallback. Status readers consume published observations without hardware
or PCM locks. Dashboard markup/styles are unchanged and use the effective fields.

## Editors

Source's **Configure RDS for this source** is UI-only nullable ownership, not
physical RDS enable. Save creates the row; merely toggling creates no DB record.
Unchecking hides the editor without confirmation and changes only the draft.
Re-enabling before Save restores its values. Only Save requests removal; leaving
without Save does not change persistence. Follower rejection preserves the draft.

The shared Angular content editor preserves PI/ECC/PTY/TP, PS carousel controls,
AF and RT validation. Source RDS is a bordered section alongside Source and Audio
processing, above the page-level Save/Cancel actions. Its Fill from source action
changes only the unsaved PS/RT draft; replacing a carousel requires confirmation.
Modulator retains physical enable and Follow/Custom selection, with no fill action.
Identification, PS, AF and RT have separate groups in the shared editor.
Selecting a DIGITAL Source with RDS chooses Follow
in the draft; one without RDS chooses Custom. ANALOG selects Custom. Follow
shows ownership guidance rather than editing Source content from the TX form.

## Preproduction storage

No migration or version bump is provided. An older incompatible schema-1 database
is rejected untouched under the existing preproduction policy; it must be recreated
explicitly outside normal operation. Tests use isolated databases. No operator
database is rewritten by this feature or its tests.

## Apply failure observability

A handled audio rebuild failure after a mixed save leaves the entire desired
configuration persisted and returns `applied=false` with `apply_error`. Changed
RDS content is still offered to followers after that result; there is no rollback
of saved RDS or audio settings. Unexpected uncaught exceptions are not this contract.

All followers are fenced before refill runs. A target response error is handled
per TX; a transport failure can suspend further work until existing recovery runs.
Already completed target operations are not rolled back. Per-TX runtime RDS errors
remain authoritative; Source Save does not aggregate follower acknowledgements.
The current tests cover multi-follower success and refill error handling separately;
there is no dedicated mixed-audio-failure-plus-RDS or multi-follower partial-failure
integration test. This editor change does not introduce a new apply-failure policy.
