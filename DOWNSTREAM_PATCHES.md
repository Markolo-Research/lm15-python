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
