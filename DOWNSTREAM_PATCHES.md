# Temporary DSPy integration patches

Upstream release: lm15 v1.2.3 at
`fcc367caedbeca42831e4e64578d222827baf1c1`.
Contract pin: `8eedbae4f13dc80666024503256149a7bbf39388`.

Replay these commits in order on a newer eligible release, omitting a patch
only when its regression is covered by the upstream release. Keep fork main
unpatched and do not rewrite published integration branches.

1. `7ff9f18` (original `bf96a4fe32edfa72125a2f9c2ee7ea0cc206c655`):
   enforce the selected model for keyword requests as well as explicit Request
   values. Otherwise a bound client can silently request a different model.
   The regression in tests/test_login.py verifies matching spellings and
   rejection before dispatch for request, plan, complete and stream.
2. `9897ee2` (original `3a5cac83983aa462841f32a7f9842a085ac052ef`):
   classify renewal HTTP 429 as RateLimitError rather than LoginDenied. A
   temporary rate limit must not delete saved credentials. The regression in
   tests/test_login.py checks preserved credentials/revision, cleared in-flight
   state, no automatic retry and a successful later renewal.
3. `1becdc1` (original `dd0662c866dfd208ffed443f071e10123170ef0e`):
   keep credentials, account headers and endpoints in one per-operation
   snapshot. Retained adapters acquire the active connection for each
   operation; bound clients retain their selected identity. Dispatch checks
   the snapshot's generation before every transport handoff, including delayed
   streams and multi-wire operations. Renewal cannot silently change a known
   account ID. Regression coverage in tests/test_managed_dispatch.py includes
   sync/async replacement, renewal, logout races, batch upload/submit, explicit
   endpoints and offline planning.
4. `3a883ec` (original `046f2bfa5c12b55c6a199f388879e5d689397a87`):
   preserve the retained adapter's transport and adaptation policy when
   preparing an operation. Sync/async regressions verify the documented
   transport replacement escape hatch sends only through the caller's
   replacement transport.

All four patches remain necessary in v1.2.3. Its new provider auth-error
classification does not fix renewal HTTP 429 handling; its router changes
redact invalid credential names, without adding managed request snapshots.
The replay has no patch-content changes from the validated v1.2.1 stack.

The first two fixes originate on cmpnd-ai/lm15-python branch
fix/managed-auth-review. The routing fixes are published on
fix/managed-request-snapshots. Upstream PR submission was denied by GitHub
integration permissions; no patch is claimed to be merged upstream. Check
for an existing upstream PR before attempting submission again.

Managed websocket operations and raw request builders that bypass operation
preparation fail closed; explicit/unmanaged credentials retain their existing
behavior. No shared adapter fields are mutated during requests. A snapshot
admitted before logout may still reach the transport: logout cannot recall an
already admitted request.

Full source tests, strict conformance, the exact pinned contract harness,
type checks and DSPy integration tests must pass on every changed stack before
import. The only approved orb exceptions are the sync/async reserved-IP
connect-timeout tests, with passing exact-release upstream CI. Do not infer
that passing existing contract tests alone demonstrates the race regressions.
No live provider calls are needed.

## Additional Markolo DSPy integration corrections

These fixes retain the v1.2.3 contract and remove regressions in its public
provider namespace and the existing permanent underscore aliases. They are
published only on the temporary Markolo source branch, not claimed upstream.

5. `50aa5998339e3a56fce50e562827949354f7ed3f`: release the transport-package
   collision so the lazy root `providers` export returns the managed registry.
   Existing `tests/test_login.py` owns the public descriptor regression.
6. `9f7f97236a687565bec3475b75ef8b1b6a57496e`: use `_settings_entry` in managed
   dispatch and planning, keeping supplied underscore-spelled cloud settings
   ahead of profile/environment defaults. Sync/async regressions in
   `tests/test_managed_dispatch.py` cover both accepted spellings and planning.
7. `3d07d52f7bc0b40f556d7c6a26321d6625975012`: declare the root registry export
   for type checkers without loading login eagerly. The existing strict user
   authentication example now checks the same public descriptor API.

The new regressions fail on the pristine imported patch stack and pass with
these corrections. Validation: 3,712 source tests pass, 8 optional tests skip,
and the two previously documented orb reserved-IP timeout cases are deselected.
Strict offline conformance passes all six fixture/serde/doc lanes. The pinned
mypy 2.3.1 gate checks 32 user programs with zero errors and no package-baseline
increase. Source changes are imported through DSPy's vendoring updater; do not
edit the packaged vendor copy by hand.

### Final review corrections

8. `3b4e9443700c6a8dc7b147eb4b25d5687794b79e`: use the non-colliding
   `provider_descriptors` root export. `providers` remains the documented
   adapter package, including dotted imports. This supersedes the namespace
   cleanup approach in patch 5; the current tree has one descriptor registry.
9. `9effc16c020749263c56ee9ca42385340e59d446`: merge a saved cloud connection's
   host settings before explicit router overrides; no ambient profile may
   silently substitute for the saved project's settings.
10. `4fa1799346713f081aecee9570a8808f9ccc4dd4`: new/replacement connections
    derive settings only from the new inputs/result, and an unwinding login
    removes only its own cancellation event. Existing login/dispatch suites
    cover host replacement, saved cloud precedence and overlapping attempts.

Final validation retains zero new mypy findings and all strict offline
conformance lanes. The public registry and adapter import surfaces coexist;
no DSPy facade workaround is required.

11. `cde858320eb066c815ddc7d5adb43605f0023180`: initial cancellation propagation.
    This is an intermediate implementation, superseded by patches 12–18 below.
    Do not stop replay here: joining a blocked synchronous prompt is unsafe.

The final source suite passes 3,719 tests (8 optional skips, the same two
reserved-IP timeout cases deselected); all six strict conformance lanes and
the pinned 32-program mypy gate pass with no baseline increase.

## Subsequent review corrections (required replay continuation)

12. `a521c778ed9a8103f34c38a3f5eeb4dfaaa758c6`: transactional cancellation,
    attempt ownership, verification identity and malformed slot validation.
13. `e20203aee57097c63eae0c2da0147576aa996af0`: daemonize interactive login
    so an uninterruptible prompt cannot hold default-executor shutdown.
14. `01198ff15327a2b8ccc4a67ed695182917b02e00`: reject cancelled attempts
    inside the reservation transaction, including late worker scheduling.
15. `c6df77e6ec68f25bda712982ad6b542c46d3db28`: register the cancellation
    event inside its reservation transaction and clean up failed reservations.
16. `3ca8bfaffbba5d62131dd41b86cc2dddc4b2d1e4`: await the same durable
    cleanup operation despite repeated native task cancellation.
17. `8f02f8899e2ea146f4e16c3d70d813c3486cb938`: avoid Python 3.14 shield
    exception logging and surface failed durable cancellation cleanup.
18. `0e59602109dae9d0cf32fa0b305a17cc957c1233`: validate verification
    records and release configure reservations on unsuccessful exits.
19. `6334bfd5933279b9c13ff6fed4fc027f63fca8fa`: explicit UTF-8 in test
    fixtures and subprocesses for strict warning mode.
20. `64d42abcbb3e3ea04fc34ecaeea89da18009c7be`: require explicit live-test
    opt-in and model selection before any credential read; no model defaults.
21. `83641cd514b6f311fe4fa8d13c2712e8249bc970`: reject non-string stored
    connection settings at the typed storage boundary.
22. `0c19e6d4ea6a015f7b87b0c3fd1abc4fd9bdf16e`: regression for AUTH-19's
    commit-winning native cancellation, without deleting committed credentials.
23. `46c95a7c3c3e568fcd9658c5cff3d3e596670d67`: validate required recipe
    material before status or request dispatch; preserve malformed files.
24. `3e048b95b4d8e98369ca268f1cbf55964b3e369e`: separate the optional
    recipe-field variable from slot-field typing.

25. `4dc6dbd406b407461d5de35cfc0294c3d6289d56`: validate required OAuth
    access tokens, optional refresh/account identifiers and finite timestamps
    at the typed storage boundary; preserve malformed files.
26. `668ad88e7bd7f955709f6aaa5215e0faa801e396`: reject missing, unknown and
    non-string material kinds before status or request dispatch.

27. `12248612f4048f5b2d66fecee46b3e1236604574`: classify overflowing OAuth
    timestamps and unknown recipe discriminator values as storage errors;
    preserve native cancellation when unknown-provider discovery cannot reserve state.

28. `fde80cdbd4da61e6c56bef68e72b051c0fa7821c`: bind environment and external
    credential recipes to their declared providers, validate datetime expiry
    ranges, give canonical settings deterministic precedence, and offload initial
    adapter construction in async entry points with stream cleanup.

29. `66c0be9e5921eb5fa69196064aee21373cfaab32`: detach transport and cached
    adapters off the event loop when async close waits for construction; close
    the detached async transport on its owning event loop.

30. `262c781ecbfde592e52b594a25c22e7ba35a1b0c`: retain ownership of async
    transport detachment and close through repeated cancellation, then propagate
    cancellation after cleanup completes.

30a. `40996bdd556901b062a9b6845929cde4bc18521d`: correct no-home portability
     fixtures for POSIX passwd fallback; replay between patches 30 and 31.

31. `e098f5bdfda19b132a8909d1ed128d26f20c4371`: retain and drain initial
    async adapter construction through repeated cancellation before propagating
    native cancellation to the caller.

32. `af502a8bec56f6f5c9c97a339b9979d7f39d45d8`: pin catalog discovery to the
    captured connection and generation before constructing its router.
33. `b37edbb1af6bebf02772ce9cda43e8ca652441e6`: preserve saved identity in bound,
    catalog and verification APIs; drain mutating async workers and OpenAI-shaped
    construction; validate material kinds against provider flows.
34. `d10a00595cc8a80fed1866c18bc8ef36afa22406`: preserve generic construction
    result typing and normalize whitespace.

35. `d1fdef4e419b2275ba963635affde2eea1b3c703`: keep read-only async status
    and connections promptly cancellable while mutating workers remain drained.

36. `bdb149c6c6a8e1273400341ab2317b82814a9bf8`: isolate cancellable reads
    from default-executor shutdown and validate stored connection identifiers.

37. `6124527c510ba9369e9c816ad168653be9db8861`: bound daemon read workers
    across instances and loops; require positive, non-boolean attempt lifetimes.

38. `64fdd2d20d51f9cea1fee461fe613967a1ed824e`: bound abandoned login workers
    across instances and loops with a separate cancellable capacity gate.
39. `a76f58f405555c11e2f6ccb13dcfb730921f8de2`: preserve caller context variables
    in daemon read and login workers.
40. `55f020806da908ad4a3712b6c079713c47235a12`: supply the required test UI
    in the bounded-login recovery fixture.

41. `c2f11d98e8d0174df132efa1ca33f1dec67f926a`: honor externally supplied
    cancellation events while waiting for login capacity and after acquisition.
42. `41c385a1f7d5fa95bd4fb419f04dd808d912616f`: reject boolean login
    lifetimes and drain managed async credential preparation before cancellation.
43. `72ec677ddf3c223b1daacc1fd8581be327aa8335`: match the empty connections
    tuple contract in regression assertions.

44. `225d1aaa6388ad61b6d89e9c1707fd9b59e7b9cf`: reject boolean stored attempt
    start timestamps before interpreting reservation age.

45. `d33218f802c66c163837dae682450a07da0aa8ef`: reject nonfinite login
    lifetimes, stop queued logins after Auth closure, and drain managed admission.

46. `58969ecaddce97506da75d3983267f8a9f8ee2ee`: preserve unrelated provider
    credential schemas in the shared file while validating recognized providers.
47. `c32e5b4a50f40c33bbcf6980ce133c9d721c9813`: specify UTF-8 in the shared
    credential preservation regression.

48. `e2d60d95d3239af975997927c40e993508541b15`: preserve cancellation on
    transport cleanup failure and reject login reservation after Auth closure.
49. `d96422a9343f86835b9908def211728e3633e0da`: enforce closure under the
    reservation lock for configure operations without a cancellation event.

50. `ef025038f0fc0ae9944179fcbebade0fc0f33ca8`: preserve unrelated scalar,
    list and null credentials before managed shape validation; restore replay 30a.

51. `fdc9a77cc4fe53e254b1331831461bc10fdbe72f`: keep bound connection display
    pinned to the selected identity and reject unknown stored lifecycle states.
52. `fb9b552f00be86aacd2db196d6ec37150be1d809`: commit the generation change
    in the replacement test fixture.

Patches 12–52 are ordered descendants of patch 11 and must be replayed too.
The earlier suite counts above describe historical intermediate stacks.
At patch 24, strict offline login/subscription validation passed 113 tests,
with two live tests excluded, zero external network attempts, zero new typing
findings, 32 clean user programs, and passing signatures/lock/diff checks.
Future replay must rerun the full qualification described above; these counts
are not evidence for a newer upstream base. Keep this replay manifest and
DSPy's generated provenance aligned whenever another correction is added.

At patch 26, all 222 offline login/router tests pass, including 13 focused
malformed-material regressions. All 32 user typing programs remain clean,
with the unchanged package baseline. Syntax and diff checks pass. This is
focused correction evidence, not a new full-suite or upstream qualification.

At patch 27, 229 offline login/router tests and 20 focused boundary tests pass.
All supported recipe discriminator values are covered without external probes.
The 32 user typing programs remain clean with no package-baseline increase;
imports, syntax and diff checks pass.

At patch 28, 306 broader offline tests pass (three optional live Ollama tests
excluded), including 13 focused security and off-loop construction regressions.
All 32 user typing examples remain clean with unchanged package baseline.
Imports, syntax and diff checks pass; no real credentials or providers are used.

At patch 29, 332 offline async/login/router tests pass (three live tests
excluded). The close regression fails on the prior base and passes with the
correction. All 32 user typing examples remain clean; package typing matches
the unchanged 451-finding baseline. No live provider calls were made.

At patch 30, 334 offline async/login/router tests and eight focused async tests
pass. Both cancellation regressions fail on the prior base. Three live tests
remain excluded; 32 typing examples are clean and the package baseline is
unchanged. No live provider calls were made.

At patch 31, 3,796 full offline tests pass, with two Windows-only skips and
seven live exclusions. Ten focused async cases, all six strict conformance
checks, signatures, typing and the contract harness pass on macOS/Python 3.12.
The no-home fixture correction is commit 40996bdd556901b062a9b6845929cde4bc18521d.
No provider calls were made.

At patch 34, 3,814 full offline tests and 354 focused tests pass, with two
Windows-only skips and seven live exclusions. Strict conformance, signatures
and the contract harness pass (1,904 cases, 56 fixture skips). The original
451-finding package typing baseline is unchanged; 32 user examples are clean.
Catalog replacement and saved-identity override regressions are included.
No provider calls were made.

At patch 35, 3,816 offline and 356 focused tests pass, including both read-only
cancellation regressions (red on parent). Mutation draining remains covered.
Strict checks, unchanged typing baseline and all 1,904 harness cases pass.

At patch 36, 3,826 offline and 366 focused tests pass. Both subprocess shutdown
regressions and eight malformed-identity cases fail on the parent and pass here.
Strict conformance, signatures, unchanged typing and 1,904 harness cases pass.

At patch 37, 3,830 offline and 370 focused tests pass. Cross-loop blocked reads
stay at four workers while the parent grows from eight to sixteen. Cancellation,
capacity recovery and start-failure release pass. Strict checks, unchanged typing
and 1,904 contract-harness cases pass without provider calls.

At patch 40, 3,834 full offline tests pass, with two Windows-only skips and
seven live exclusions. Bounded login recovery and three caller-context
regressions pass. Strict conformance, signatures and all 1,904 contract-harness
cases pass (56 fixture skips). The 451-finding package typing baseline remains
unchanged and all 32 user examples are clean. No provider calls were made.

At patch 43, all seven targeted cases, 3,841 full offline tests and 381 focused
tests pass, with two Windows skips and seven live exclusions. Prior-source
regressions reproduce queued cancellation, boolean-True acceptance and four
managed-preparation cancellation cases. Strict conformance, signatures and
1,904 contract checks pass. Typing retains the unchanged 451 baseline with
32 clean user examples. No provider calls were made.

At patch 44, both timestamp regressions fail on parent and pass on candidate,
preserving stored bytes. Full offline: 3,843 passed, two Windows skips and seven
live exclusions. Focused: 383 passed; malformed-slot controls: 26 passed.
Unchanged typing, strict conformance, signatures and 1,904 contract checks pass.

At patch 45, 12 supplied regressions and 17 independent probes pass, including
real complete/send and stream preparation/admission cancellation with worker
errors. Full offline: 3,848 passed, two Windows skips and seven live exclusions;
focused: 388 passed. Typing remains unchanged; strict checks and 1,904 contract
cases pass. Cancelled calls send nothing; no provider calls were made.

At patch 47, 3,850 offline tests and 390 focused tests pass, with two Windows
skips and seven live exclusions. Both shared-store regressions and 73 independent
store probes pass, preserving unrelated entries while malformed recognized
provider material fails closed. Unchanged typing, strict checks and all 1,904
contract cases pass. No provider calls were made.

At patch 49, 3,857 full offline tests, 397 focused tests, seven supplied
regressions and seven real race probes pass. The parent reproduces the configure
race; strict gates and the unchanged typing baseline pass. No provider calls.

At patch 50, 3,861 offline tests, 401 focused tests, six preservation cases and
85 independent probes pass. Prior source reproduces four new failures;
recognized providers still fail closed. Strict gates, unchanged typing and all
1,904 contract cases pass. Replay 30a ordering matches generated provenance.

At patch 52, 3,871 full offline tests, 411 focused tests and 16 independent
controls pass. Nine regressions reproduce on the prior published source.
Valid lifecycle states and same-identity renewal remain intact. Typing retains
the 451-finding baseline and 32 clean examples; no provider calls were made.
