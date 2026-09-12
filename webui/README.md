# fmheadend Web UI (M7A)

Standalone Angular operational Dashboard. See [Milestone 7A](../docs/milestone7a.md)
for structure, status semantics, test results and real-backend smoke instructions.

```sh
# Node 24.15+ (24 LTS), npm 11/12
npm ci
npm start
npm test
npm run typecheck
npm run build
```

Development UI: http://localhost:4200. The dev proxy forwards relative `/api/**`
to http://127.0.0.1:8080. Production hosting is deferred to M7B.
