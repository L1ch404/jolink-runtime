<!-- jolink-java-verification:begin -->
## Java change verification

For implementation tasks that change Java behavior or build/runtime configuration,
verification is part of the task. Do not wait for a separate request to test.

Load the installed jolink-java skill when selecting verification for these changes.
Use joLink when available and suitable, while preserving required native build checks.
Choose checks based on the changed behavior, impact, and unverified assumptions—not
self-rated confidence alone.

Reuse relevant tests; add focused coverage when needed. Verify application behavior
when correctness depends on runtime integration. Use debugging when existing evidence
cannot explain a failure, then re-verify the fix.

Check coherent changes, not every individual edit. For clearly non-behavioral changes,
use proportionate static checks. Respect explicit user opt-outs, project restrictions,
and tool approvals. Use known development/test environments; clarify consequential
or uncertain side effects before execution.

At handoff, state what was actually checked, the observed results, and anything still
unverified. If verification is blocked, report the blocker rather than claiming success.
<!-- jolink-java-verification:end -->
