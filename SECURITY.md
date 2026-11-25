# Security

EconTone Studio runs entirely on the local machine and talks to no external service.

## Trust model

- The panel is served by the DSH web server on loopback; every API route checks the request origin
  (`Origin` / `Host`) against the DSH server origin and rejects cross-origin calls.
- The plugin never sends manuscripts, prompts or scores to a remote endpoint. There is no telemetry.
- `index.js` spawns `python engine/studio_bridge.py` locally to run the EconTone skills; the child
  process inherits the DSH environment and writes only inside the active workspace.
- Writes are limited to `<workspace>/.econtone/` (state, binding, run records) and the
  system temp directory used for engine invocations.
- The client fetches only `/econtone-studio/api/*` from the same origin.

## Reporting

Open a private security advisory on the GitHub repository, or email the maintainers listed there.
Please do not file public issues for vulnerabilities.

## Scope

In scope: the host routes, the client bundle, the Python bridge, and the packaging metadata.
Out of scope: the EconTone skill pack itself (a separate project) and the DSH host.
