# Licensing FAQ

**This is not legal advice.** It is a plain-language summary to help you understand your options. The license texts are what count: [LICENSE](LICENSE) for AGPL-3.0, and your signed agreement for a commercial license. If you are unsure, ask a lawyer.

Pairly AI is open source (AGPL-3.0) with a commercial license option. Proofly Technology is the licensor.

## Can I use Pairly at my company for free?

Yes. Anyone, including companies of any size, may use, modify and self-host Pairly for free under AGPL-3.0. Commercial use does not require payment.

If you run Pairly unmodified for your own staff, you have no extra duties beyond keeping the license notices. If you modify it, read the next two answers.

## When do I need a commercial license?

Only when you want to avoid the AGPL-3.0 conditions. The common cases are:

1. You modified Pairly, users interact with it over a network, and you want to keep your changes private.
2. You want to embed Pairly in a closed-source product.
3. You want to offer Pairly as a service to other organisations without AGPL-3.0 obligations.

See [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md) for details and how to contact Proofly Technology.

## What counts as offering it over a network?

Section 13 of the AGPL-3.0 applies when you **modify** Pairly and **users interact with your modified version remotely**, for example through a web browser or an API. Those users must be offered the source code of your modified version.

Some examples, as we read the license:

1. Your recruiters use an unmodified Pairly through their browser. No source sharing duty arises from Section 13, because you did not modify it.
2. You changed Pairly's screening code and your recruiters use it through their browser. Your recruiters are users interacting over a network, so you must offer them your modified source under AGPL-3.0.
3. Candidates or vendors use the portal of your modified Pairly. They are also users interacting over a network, so the same duty applies to them.
4. You host a modified Pairly as a service for other companies. The duty applies to all of their users.

Making the source available can be as simple as a "Source" link in the interface that points to your repository.

## Do I have to publish my configuration or data?

No. The AGPL-3.0 covers the program's source code. Your data (candidates, invoices, vendors, users) is not part of the program and is never covered.

Normal configuration, such as environment variables, policy settings, rules and templates entered through the product, is generally treated as your data, not as a modification of the program. Secrets and credentials are never part of the source you must share.

If you write new code that changes how Pairly works, for example a new workflow plugin that runs inside Pairly, that code may count as part of a modified version. Ask a lawyer if this matters to you.

## Why is there a CLA?

Pairly is dual licensed. For that to work, Proofly Technology must be able to offer all of the code under both AGPL-3.0 and a commercial license. Without a Contributor License Agreement (CLA), each contribution would be under AGPL-3.0 only, and Proofly Technology could not include it in the commercial license.

With the CLA, you keep the copyright in your contribution. You grant Proofly Technology a license to use it, including the right to relicense it. Proofly Technology pledges that Pairly will remain available under AGPL-3.0.

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CLA.md](CLA.md).

## What about third-party components?

Pairly uses open source libraries. Each one stays under its own license. See [NOTICE.md](NOTICE.md). We only use dependencies whose licenses are compatible with AGPL-3.0 and with commercial relicensing, and CI checks this.
