# Security policy

## Supported versions

Security fixes target the latest release published on
[PyPI](https://pypi.org/project/cancellens/). Older releases do not have separate
maintenance branches. Upgrade to the latest release before checking whether an issue
still occurs. Development builds on `main` are not releases. See the
[changelog](CHANGELOG.md) for release history.

## Report a vulnerability

Email [the maintainer](mailto:zheltov_pe@mail.ru) with the subject
`[CancelLens security]` for suspected vulnerabilities, sensitive data exposure, or
unsafe behavior outside the documented test boundaries. Do not disclose exploit
details, credentials, or sensitive reports in a public issue or pull request.

Include the CancelLens version or commit, CPython version, operating system, affected
component, expected security boundary, potential impact, and a minimal reproduction
using disposable data. Remove credentials, private application data, and unnecessary
paths from attachments. If a saved schedule is involved, include the relevant source
and interpreter versions so its prefix can be evaluated correctly.

The maintainer will assess the report and coordinate any fix and disclosure with the
reporter. There is no guaranteed response time or bug bounty program. Ordinary bugs
without sensitive details can use the public bug report form.

## Safe use and trust boundaries

- Cases, oracles, teardown callbacks, and validation commands execute trusted code
  with the invoking user's permissions. CancelLens is not a sandbox for untrusted code.
- Use disposable resources. Cancellation and teardown can affect application state;
  the harness cannot roll back arbitrary external side effects. Do not run exploratory
  cases against shared or production resources without understanding those effects.
- In-process deadlines require a functioning event loop. The external watchdog has
  platform-specific termination limits described in [Validation](validation/README.md).
  Neither mechanism is a security isolation boundary.
- Runtime reports omit operation values, locals, and exception messages, but explicit
  invariant text is supplied by the case author. External validation evidence can also
  contain interpreter paths, package versions, and upstream metadata. Review files
  before sharing them; omission of selected fields is not a general secrets scanner.
- A FAIL identifying a resource invariant violation in a tested application is not by
  itself a vulnerability in CancelLens. A PASS covers only the evaluated schedules
  and oracle; it does not establish application security or general cancellation safety.
