# Security policy

Pairly handles hiring and accounts payable data, so we take security reports seriously. Thank you for helping keep Pairly and its users safe.

## How to report a vulnerability

Please report privately. **Do not open a public issue, pull request or discussion** for a security problem.

Use one of these channels:

1. **GitHub private vulnerability reporting** (preferred): go to the repository's **Security** tab and choose **Report a vulnerability**.
2. **Email**: ghattas@mail.proofly.ae

Please include:

1. A description of the problem and its impact.
2. Steps to reproduce, or a proof of concept.
3. The version or commit you tested.
4. Your name or handle if you want to be credited.

Use synthetic data only in your report. Do not send real personal data.

## Scope

In scope:

1. Code in this repository: the core engine, Pairly Hire, Pairly Pay, the API, the worker, the MCP server, the web app and the SDKs.
2. The default configuration and deployment files in this repository (for example the docker compose setup).
3. Problems specific to AI agents in Pairly, for example:
   1. Prompt injection that makes an agent take an action it should not, or skip a check.
   2. A way for an agent to execute a final action (reject, send an offer, approve a payment, change bank details, delete data) without a human approval.
   3. Data leaking across tenants, or out through a model or trace.
   4. Bypass of fraud checks such as duplicate invoice or bank detail change detection.
4. Authentication, authorisation, tenant isolation, signed links, webhooks and the audit trail.

Out of scope:

1. Vulnerabilities in third-party dependencies that are already public. Report those upstream. Tell us if Pairly is exposed in a way the upstream fix does not cover.
2. Deployments run by other people or organisations. Contact the operator.
3. Findings that need a compromised host, physical access or a malicious administrator.
4. Denial of service by volume, spam, and social engineering of maintainers or users.
5. Reports from automated scanners without a working impact.
6. Missing security headers or best practices with no demonstrated impact.

## What to expect

These are targets, not guarantees. We are a small team and will tell you if we expect to miss them.

| Step | Target |
| --- | --- |
| Acknowledge your report | Within 3 business days |
| First assessment and severity | Within 10 business days |
| Fix for critical and high severity issues | Within 30 days of confirmation |
| Fix for medium and low severity issues | Within 90 days of confirmation |

We will keep you updated while we work on it. We will agree a disclosure date with you, normally when a fix is released. We will credit you in the advisory unless you ask us not to.

## Safe harbour

If you act in good faith under this policy, we will consider your research authorised. We will not take legal action against you or ask others to do so, as long as you:

1. Test only against your own installation of Pairly, or one you have permission to test.
2. Use synthetic data and do not access, change or delete other people's data.
3. Do not degrade services for other users.
4. Give us reasonable time to fix the problem before you disclose it publicly.
5. Do not ask for payment in exchange for not disclosing.

If you are unsure whether something is allowed, ask us first through a private channel above.

This statement covers only Proofly Technology and this project. It cannot authorise testing of systems run by others.

## Bug bounty

There is no bug bounty program. We cannot pay for reports. We are grateful for them and will credit you publicly if you want.

## Supported versions

Pairly has not had a stable release yet. Security fixes go into the `main` branch. This section will list supported versions once releases start.
