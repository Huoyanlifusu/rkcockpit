# Release Policy

[中文版](RELEASES.zh-CN.md)

## Versioning

rkcockpit follows Semantic Versioning and currently uses the `0.x.y` series:

- `0.MINOR.0`: backward-compatible features or significant improvements
- `0.MINOR.PATCH`: backward-compatible bug and security fixes
- `1.0.0`: the first release with explicitly stable public interfaces

After `1.0.0`, `MAJOR` versions may introduce incompatible changes.

## Cadence

Releases are readiness-based. Patch releases are published as needed, while
minor releases target a 4–8 week cadence. Significant authentication, storage,
deployment, transport, or migration changes should receive a release candidate.

The schedule is a target, not a guarantee.

## Branches and Tags

- `dev` is the integration branch; feature and fix pull requests target it.
- Release pull requests promote tested changes from `dev` to `main`.
- `main` contains stable, release-ready code.
- Stable releases use annotated `vX.Y.Z` tags from `main` and a matching GitHub
  Release.
- Hotfixes start from `main` and are merged back into `dev`.

## Compatibility and Support

During the `0.x` phase, the latest minor release receives fixes. Older minors
and pre-releases are supported on a best-effort basis.

Each release documents tested Python versions, host operating systems,
transports, browsers, deployment modes, and any configuration or data migration.
Platforms not covered by CI are best-effort unless separately verified.

## Release Checklist

Before publishing a release:

1. The intended tests and Python/JavaScript syntax checks pass.
2. Security-sensitive path, authentication, and deployment tests pass.
3. English and Chinese documentation are synchronized.
4. Upgrade, migration, and rollback behavior is reviewed.
5. The release commit is on `main`.

Release notes include user-visible and breaking changes, upgrade and rollback
instructions, security fixes, known issues, tested platforms, migrations, and a
comparison link to the previous release.
