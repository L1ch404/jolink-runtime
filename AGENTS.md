# joLink Product Development Principles

joLink is an open-source Java Runtime Interface for Coding Agents. It is not
a repository-specific tool and must not be optimized only for one company's
project.

## First principle: eliminate uncertainty with real execution evidence

joLink exists to replace uncertain reasoning about program behavior with
observed runtime facts. joLink development must follow the same rule.

- Prefer a real repository, real JDK, real Maven/Gradle invocation, real MCP
  stdio subprocess, real JVM, real filesystem, and real lifecycle over mocks or
  static reasoning whenever the behavior crosses those boundaries.
- Unit tests and mocks are useful for local invariants, race control, and
  failure injection, but they are not acceptance evidence for integration,
  distribution, configuration resolution, process cleanup, encoding,
  cross-platform behavior, or product compatibility.
- Before claiming a capability works, exercise the user-visible path from its
  real input through its real external systems to its observable result. For
  build/test/reload/debug features this normally means a real project and a
  real Java process, not only a fixture that mirrors the expected shape.
- When the required real environment is unavailable, report an evidence gap.
  Do not replace missing execution evidence with a confident inference, a
  green mock, or a newly documented boundary.
- Every conclusion must distinguish observed facts, inferences, and unknowns.
  A safe rejection proves safety; it does not prove compatibility or
  usability.
- Benchmarks and dogfood are compatibility discovery tools. Investigate why a
  valid mainstream project did not reach the Fast Path instead of treating a
  structured `UNSUPPORTED` result as success.

The preferred evidence order is:

```text
real user/project reproduction
→ real cross-boundary E2E
→ contract/invariant tests
→ focused unit tests and mocks
→ static reasoning only as supporting evidence
```

## Improve the product through real use, not speculative perfection

- Prefer the smallest coherent implementation that can be exercised in a real
  project. Harden it from observed failures instead of trying to predict every
  possible edge case before users can run it.
- It is acceptable to move a little aggressively: joLink compiles, launches,
  tests, reloads, and observes development code. Reversible and clearly
  reported failures are often more useful than permanent complexity built for
  hypothetical scenarios.
- Do not build elaborate state machines, abstractions, or fallback layers only
  to make the first version look complete. Every durable mechanism should pay
  for itself in a real workflow.
- This does not relax the non-negotiable boundaries: never report false
  success, leak secrets, leave a JVM/thread/suspension behind, or corrupt user
  source and formal build outputs.

## Review before committing

- Do not automatically commit or push immediately after implementation.
  Explain the behavior changes, affected files, real validation, and remaining
  issues so the user can review first.
- Commit and push only when the user explicitly requests it for the current
  work. An explicit request to validate and then push is sufficient.

## Generality is a product requirement

- The expected user is a stranger with an ordinary, previously unseen Java
  repository. They should not need a joLink maintainer to diagnose setup, edit
  their POM/Gradle build, or add repository-specific workarounds.
- For the defined mainstream Java scope, persistent JDT compilation, Fast
  Test, reload, and Runtime observation are core product promises, not optional
  demonstrations.
- A structured fail-closed result prevents false confidence, but it is only
  the minimum safety requirement. `UNSUPPORTED` is not evidence of product
  completion.
- Do not describe a run as broadly successful when most real projects did not
  enter the Fast Path, even if every rejection was safe and well structured.

## How to handle a newly discovered boundary

For every new `UNSUPPORTED`, `UNVERIFIED`, or fallback result:

1. Determine whether the configuration is common inside the documented
   mainstream Java scope.
2. If it is common, treat it as a product compatibility gap and continue the
   investigation; do not close the work merely by documenting the rejection.
3. Model the underlying Maven, Gradle, javac, JDT, test-runner, or Runtime
   semantics through a reusable abstraction. Never branch on a repository
   name, company, or one exact POM shape.
4. Validate the general solution against company dogfood, public repositories,
   and benchmark repositories.
5. Keep a formal-build fallback only as a safety mechanism for clearly
   out-of-scope/custom build systems or disaster recovery. Fallback does not
   count as Fast Path support.

## Coverage and reporting

Reports must distinguish:

- **Safety:** no false success, corrupt generation, leaked process, or stale
  observation.
- **Usability:** an unfamiliar supported project can use joLink without manual
  project edits or maintainer assistance.
- **Fast Path coverage:** the project actually reaches persistent JDT/Fast
  Test/reload rather than a formal Maven/Gradle fallback.

The product target is at least 98% Fast Path coverage inside a frozen,
documented mainstream Java scope. The denominator and exclusions must be
explicit and evidence based. Do not inflate coverage with environment
failures, invalid benchmark instances, or formal-build fallback.

See `docs/product-generality-principles.zh-CN.md` for the durable product
decision and detailed acceptance criteria.
