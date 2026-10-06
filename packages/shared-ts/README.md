# @pairly/shared-ts

TypeScript types for the Pairly API, generated from its OpenAPI document with
openapi-typescript.

```bash
pnpm gen:api                                                  # from the API code, no server needed (uses uv)
pnpm gen:api -- --url http://localhost:8000/v1/openapi.json   # from a running API
```

Both commands write two committed files:

- `openapi/v1.json`: the schema snapshot, keys sorted so diffs stay small.
- `src/generated/api.ts`: the generated `paths`, `components` and `operations` types.
  Never edit it by hand.

`src/index.ts` re-exports them and adds a few named aliases (`Me`, `Problem`,
`ORG_HEADER`). Run `pnpm gen:api` after any API change that touches a route or a response
model, and commit the result.
