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

## Local media sources

In Sources, choose **Local media**, then **Folder** or **Playlist**. Enter an absolute
path on the Linux FMHeadend host, not on the browser's computer. No upload or file
browser is provided. Paths are saved unchanged; unavailable NAS/USB storage does
not prevent configuration from being saved. Playback errors remain runtime feedback.

New LOCAL forms default to Folder, Sequential order, Recursive off and an empty
path. Recursive is shown only for Folder; switching to Playlist clears it while
retaining the path. Existing playlists with an ignored recursive flag are saved
with that flag false. Sequential/Random use the existing backend behavior.
