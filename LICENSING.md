# Licensing FAQ

**This is not legal advice.** It is a plain-language summary to help you understand your options. The license texts are what count: [LICENSES/](LICENSES/) holds every license used in this repository ([LICENSE](LICENSE) at the root is the AGPL-3.0 text), and a signed agreement is what counts for a commercial license. If you are unsure, ask a lawyer.

Proofly Technology owns the copyright in Pairly AI and is the licensor.

## The split in one table

| Part of the repository | License | Why |
|---|---|---|
| The server and the web app: `apps/`, `packages/`, `scripts/`, `infra/`, `evals/`, `synth/` and everything else not listed below | `AGPL-3.0-only OR LicenseRef-Proofly-Commercial` | Open source under AGPL-3.0, or a commercial license from Proofly Technology, at your choice |
| The SDKs: `sdks/python` and `sdks/typescript` (including the API types the TypeScript SDK copies in at build time) | `Apache-2.0` | Client code you embed in your own application, under any license |
| The quickstart examples: `docs/quickstarts` | `Apache-2.0` | Examples you copy into your own application, under any license |
| Third-party code and data (for example `infra/api-docs/vendor/`, the receipt font) | Its own license | See [NOTICE.md](NOTICE.md) |

In plain words: the server and the web app are dual licensed. Anyone may use them for free under AGPL-3.0, and anyone who does not want the AGPL-3.0 conditions can buy a commercial license. The SDKs and the examples are Apache-2.0, so you can put them inside a closed-source product, a script or another open source project without any AGPL-3.0 duty. As we read the AGPL, calling the Pairly API from your own code does not make your code subject to it.

## How each file says its license

Every source file starts with an [SPDX](https://spdx.dev/learn/handling-license-info/) header, written in the comment syntax of the file type. A server file:

```
SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
Copyright (c) 2026 Proofly Technology
```

A file in `sdks/python`, `sdks/typescript` or `docs/quickstarts`:

```
SPDX-License-Identifier: Apache-2.0
Copyright (c) 2026 Proofly Technology
```

`OR` means you may choose either license. `LicenseRef-Proofly-Commercial` is the SPDX name for the commercial license; [LICENSES/LicenseRef-Proofly-Commercial.txt](LICENSES/LicenseRef-Proofly-Commercial.txt) says where its terms come from. Package metadata uses the same expressions: `license = "AGPL-3.0-only OR LicenseRef-Proofly-Commercial"` in the server `pyproject.toml` files (PEP 639), `"license": "(AGPL-3.0-only OR LicenseRef-Proofly-Commercial)"` in the server `package.json` files, and `Apache-2.0` for the SDKs and the examples. Each SDK package ships the Apache-2.0 `LICENSE` and a `NOTICE` file.

`scripts/spdx_headers.py --check` enforces the policy (it runs in `make lint` and in CI), and `--fix` inserts a missing header or corrects one that names the wrong license for its folder. One exception: the prompt files (`prompts/<task>/v<n>.md`) are pinned by the SHA-256 of the whole file, so a published version keeps the `AGPL-3.0-only` header it was published with. Proofly Technology offers those files under the same dual license as the rest of the server, and new prompt versions carry the dual header.

## Can I use Pairly at my company for free?

Yes. Anyone, including companies of any size, may use, modify and self-host Pairly for free under AGPL-3.0. Commercial use does not require payment.

If you run Pairly unmodified for your own staff, you have no extra duties beyond keeping the license notices. If you modify it, read the next two answers.

## When do I need a commercial license?

Only when you want to avoid the AGPL-3.0 conditions for the server or the web app. The common cases are:

1. You modified Pairly, users interact with it over a network, and you want to keep your changes private.
2. You want to embed the Pairly server or web app in a closed-source product.
3. You want to offer Pairly as a service to other organisations without AGPL-3.0 obligations.

You never need a commercial license for the SDKs or the quickstart examples: Apache-2.0 already lets you use them in closed-source software.

See [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md) for details and how to contact Proofly Technology.

## What counts as network use?

AGPL-3.0 is the GPL plus one extra rule, in Section 13: if you **modify** the program and **users interact with your modified version remotely**, for example through a web browser or an API, you must offer those users the source code of your modified version. That is what "network use" means here. Just running the program on a server is not distribution under the GPL, so Section 13 closes that gap for server software.

Some examples, as we read the license:

1. Your recruiters use an unmodified Pairly through their browser. No source sharing duty arises from Section 13, because you did not modify it.
2. You changed Pairly's screening code and your recruiters use it through their browser. Your recruiters are users interacting over a network, so you must offer them your modified source under AGPL-3.0.
3. Candidates or vendors use the portal of your modified Pairly. They are also users interacting over a network, so the same duty applies to them.
4. You host a modified Pairly as a service for other companies. The duty applies to all of their users.
5. Your own application calls the Pairly API through the Apache-2.0 SDK. Your application is not a modified Pairly, and the SDK is not under the AGPL, so Section 13 does not reach your application.

Making the source available can be as simple as a "Source" link in the interface that points to your repository.

## Do I have to publish my configuration or data?

No. The AGPL-3.0 covers the program's source code. Your data (candidates, invoices, vendors, users) is not part of the program and is never covered.

Normal configuration, such as environment variables, policy settings, rules and templates entered through the product, is generally treated as your data, not as a modification of the program. Secrets and credentials are never part of the source you must share.

If you write new code that changes how Pairly works, for example a new workflow plugin that runs inside Pairly, that code may count as part of a modified version. Ask a lawyer if this matters to you.

## Why is there a CLA?

Pairly is dual licensed. For that to work, Proofly Technology must be able to offer all of the server code under both AGPL-3.0 and a commercial license, and the SDKs and examples under Apache-2.0. Without a Contributor License Agreement (CLA), each contribution would be under the license of its folder only, and Proofly Technology could not include it in the commercial license.

With the CLA, you keep the copyright in your contribution. You grant Proofly Technology a license to use it, including the right to relicense it under AGPL-3.0, Apache-2.0 or a commercial license. Proofly Technology pledges that Pairly will remain available under AGPL-3.0.

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CLA.md](CLA.md).

## What about third-party components?

Pairly uses open source libraries, and bundles a few third-party files (the Redoc viewer, a subset of the DejaVu Sans font, the pdf.js worker and fonts, PDFium inside pypdfium2, a disposable email domain list). Each one stays under its own license. [NOTICE.md](NOTICE.md) lists them with links to the license texts, which are in [LICENSES/third-party/](LICENSES/third-party/) or next to the bundled files. We only use dependencies whose licenses are compatible with AGPL-3.0 and with commercial relicensing, and CI checks this (`scripts/check_licenses.py`, with reviewed exceptions in `scripts/license-exceptions.yaml`).

## Items for legal review before the first commercial sale

These points are our own reading. A lawyer must review each one before Proofly Technology signs its first commercial license.

1. **CLA text.** [CLA.md](CLA.md) is a draft (version 0.1). It needs review of the license grant (including the new sentence that lets Proofly Technology license contributions under Apache-2.0 for the SDK and example folders), the patent license, the employer clause, governing law and venue, and how signatures are recorded (the CLA Assistant workflow).
2. **The pledge to keep AGPL availability.** CLA.md section 5 and [CONTRIBUTING.md](CONTRIBUTING.md) promise that Pairly stays available under AGPL-3.0. Confirm the wording binds Proofly Technology as intended, how long it lasts, whether it survives a sale of the company or of the project, and that it does not limit the commercial license.
3. **The commercial license itself.** [LICENSES/LicenseRef-Proofly-Commercial.txt](LICENSES/LicenseRef-Proofly-Commercial.txt) only points to a signed agreement. The agreement template (grant, warranty, indemnity, support, term) does not exist yet.
4. **LGPL libraries: psycopg and sharp-libvips.** psycopg, psycopg-binary and psycopg-pool (LGPL-3.0-only, pulled in by the workflow checkpointer) and `@img/sharp-libvips-*` (LGPL-3.0-or-later, used by Next.js image optimisation) are used unmodified, installed as separate packages and loaded at run time, so a user can replace them with their own build. Our reading is that LGPL-3.0 allows this from software under any license, including a commercial license. Confirm this, including what a commercial licensee who ships the container images must provide (the LGPL notices, the library source or an offer for it, and the ability to relink or replace the library). The reasons are recorded in `scripts/license-exceptions.yaml`.
5. **caniuse-lite (CC-BY-4.0) exception.** caniuse-lite is browser support data that browserslist reads at build time (Next.js, PostCSS, Babel). CC-BY-4.0 asks only for attribution, which NOTICE.md gives. Confirm the attribution is enough, and whether any of the data ends up in the shipped web build.
6. **Liberation fonts in pdf.js.** The pdf.js `standard_fonts` folder that the web app serves (copied from `pdfjs-dist` by `apps/web/scripts/copy-pdfjs.mjs`) contains the Liberation Sans fonts under GPL-2.0 with a font embedding exception (the license text ships next to them and is in [LICENSES/third-party/](LICENSES/third-party/)). They are separate files served to the browser, not linked into Pairly code. Confirm this is mere aggregation and fits the commercial license, or replace them.
7. **SPDX `LicenseRef` and the prompt files.** Confirm that the `LicenseRef-Proofly-Commercial` identifier and its text are an adequate statement of the dual license, and that the hash-pinned prompt files that still say `AGPL-3.0-only` are covered by the repository-level statement in this file.
