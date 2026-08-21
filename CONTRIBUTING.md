# Contributing to rkcockpit

Thanks for your interest! This is a small, dependency-free project, so
contributions are easy to review and land.

## Ground rules

- The backend is **pure Python 3.9 stdlib** (no third-party packages); the
  frontend is **vanilla HTML/JS** (no bundler, no framework). Keep it that way
  unless there is a very strong reason.
- Backend error semantics: `4xx` = request error; business failure =
  `200` + `{"ok": false, "error": ...}`. Never return `5xx` for a
  per-device/peripheral collection failure.
- Read-only operations (video/usb/dmesg/peripherals) use a 10s in-memory cache;
  side-effecting operations (exec, fs write, deploy, stream test) are audited.

## Local development

```bash
python3 -m unittest discover -s tests   # run the suite
python3 -m portal.portal --sim          # smoke-run on http://127.0.0.1:8080
```

`node --check static/js/pages/<page>.js` for each frontend file you touch.

## Commit & PR

- Every change starts with a GitHub Issue that defines its scope and acceptance
  criteria.
- Work on a non-`main` branch and open a pull request that references or closes
  the Issue. Direct commits and pushes to `main` are not allowed.
- One logical change per commit; reference the Issue it fixes.
- Tests must stay green (`python3 -m unittest discover -s tests`).
- Review and required CI/test evidence must pass before the pull request is
  merged. Sign-off is not required.

## Branch and release flow

- Open feature and fix pull requests against `dev`.
- Release pull requests promote tested changes from `dev` to `main`.
- Release tags are created from `main`.
- Hotfixes based on `main` must also be merged back into `dev`.

See [Release and Versioning Policy](docs/RELEASE.md).
