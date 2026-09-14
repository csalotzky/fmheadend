# fmheadend V1 WebUI

Dashboard, Sources, Modulators, Audio Profiles and System. Shared layouts and
read-only diagnostics follow [the WebUI specification](../docs/webui-spec.md).

```sh
# Node/npm versions: package.json
npm ci
npm start
npm test
npm run build
```

Development serves localhost:4200 and proxies relative `/api` to localhost:8080.
Production uses built `dist/fmheadend-ui/browser` assets via daemon `--webui-dir`;
Node is not a runtime dependency. [M7B5](../docs/milestone7b5.md) documents startup
region plans and production deployment. No automatic transmitter frequency exists.
