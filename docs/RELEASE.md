# Release and Versioning Policy

[中文版](RELEASES.zh-CN.md)

This document describes how rkcockpit versions, tests, publishes, and supports
releases.

The policy describes project intent rather than a guaranteed service-level
agreement. Release quality takes priority over a fixed calendar.

## Versioning

rkcockpit follows Semantic Versioning where practical.

While public interfaces and deployment behavior are still stabilizing, releases
use the `0.x.y` series:

- `0.MINOR.0`: backward-compatible features or significant improvements
- `0.MINOR.PATCH`: backward-compatible bug and security fixes
- `1.0.0`: the first release with explicitly stable public interfaces

After `1.0.0`:

- `MAJOR`: incompatible API, configuration, storage, deployment, or behavior
  changes
- `MINOR`: backward-compatible features
- `PATCH`: backward-compatible fixes

Pre-release versions may use `-alpha.N`, `-beta.N`, or `-rc.N`.

## Release Cadence

Releases are readiness-based rather than strictly calendar-based.

- Patch releases are published as needed for confirmed bugs and security fixes.
- Minor releases are targeted approximately every 4–8 weeks when a coherent set
  of changes is ready.
- Major releases are published only when incompatible changes are necessary.
- Release candidates should be used for significant authentication, storage,
  deployment, transport, or migration changes.

The schedule is a target, not a guarantee.

## Branches and Tags

- `main` contains stable, release-ready code.
- `dev` is the integration branch for ongoing development.
- Feature and fix branches should normally target `dev`.
- A release pull request promotes tested changes from `dev` to `main`.
- Release tags use the form `vX.Y.Z` and are created from `main`.
- Every stable tag should have a corresponding GitHub Release.
- Hotfixes start from `main` and must be merged back into `dev`.

Tags should be annotated. Signed tags may be used when maintainer signing is
configured consistently.

## Compatibility

Each release must state its tested compatibility for:

- Python versions
- Host operating systems
- SSH and ADB transports
- Configuration and stored-data formats
- HTTP API behavior
- Supported browsers
- systemd and reverse-proxy deployment

The current CI matrix tests Python 3.9–3.12 on Ubuntu. Platforms not covered by
CI should be described as best-effort unless separately verified.

Any required migration must be called out prominently in the release notes.

## Support Policy

During the `0.x` phase:

- The latest minor release receives bug and security fixes.
- Older minor releases are supported on a best-effort basis.
- Pre-release versions have no long-term support guarantee.

A broader maintenance window may be introduced after `1.0.0`.

## Release Requirements

Before publishing a release:

1. The complete intended unit-test suite passes.
2. Python and JavaScript syntax checks pass.
3. Security-sensitive path, authentication, and deployment tests pass.
4. English and Chinese documentation are synchronized.
5. Upgrade and rollback behavior is reviewed.
6. The release commit is present on `main`.

## Release Notes

Each GitHub Release should include:

- Summary
- User-visible changes
- Breaking changes
- Upgrade instructions
- Rollback instructions
- Security fixes
- Known issues
- Tested platforms and Python versions
- Migration requirements
- Comparison link to the previous release

## Initial Release

The project should begin with the `0.x.y` series. The first tag should be
created only after the current test-discovery and macOS pathguard issues are
resolved or clearly documented as known limitations.
