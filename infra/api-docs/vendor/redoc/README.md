# Redoc 2.5.4 (vendored)

`redoc.standalone.js` is the standalone browser bundle of [Redoc](https://github.com/Redocly/redoc)
2.5.4, copied unchanged from the npm package (`redoc@2.5.4`, `bundles/redoc.standalone.js`).
`make api-docs` inlines it into a static API reference page, so the page fetches nothing
at run time.

* License: MIT (`LICENSE`, Rebilly, Inc. and Redocly Inc.). The bundle also contains React
  (MIT), DOMPurify (Apache-2.0 OR MPL-2.0) and other MIT and ISC code; their notices are in
  `redoc.standalone.js.LICENSE.txt`. All permissive, so they fit AGPL-3.0 and the
  commercial license (`NOTICE.md`, Bundled components).
* SHA-256 of `redoc.standalone.js`:
  `dcaf76612bc4a3fbcc923a8966dee2f6146a5f32e5ce1b6f02dd60cbbf89500b`
  (`make api-docs` refuses any other bundle, `REDOC_SHA256` in `scripts/build_api_docs.py`;
  `scripts/tests/test_build_api_docs.py` checks it too).

To update: `npm pack redoc@<version>`, copy `package/bundles/redoc.standalone.js`,
`package/bundles/redoc.standalone.js.LICENSE.txt` and `package/LICENSE` here, review the
bundled licenses, and update the version and checksum in this file, the build script, the
test and `scripts/third_party_notices.py`.
