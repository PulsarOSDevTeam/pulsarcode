# Distribution channels

Every channel installs the same engine (`pulsar/`) and the same `pulsarcode` command. Keys,
settings and the isolated Claude Code profile always live in `~/.pulsarcode` (or
`$PULSAR_HOME`), so you can switch channels without reconnecting anything. The curl and
PowerShell installers refuse a `PULSAR_HOME` that holds files Pulsar Code did not create (they
name what is in the way and write nothing there), and the command they install always runs
against the home it was installed into. `install.sh` does not replace a `pulsarcode` command that
another install made unless `PULSAR_FORCE=1`.

The installers download from GitHub, so they need the repository to be public; with a preview invitation, install a clone with
`bash install.sh --local` (or `.\install.ps1 -Local`). The PyPI, npm, Homebrew, Scoop and release
archive channels are coming with the first public release: none of them is published yet, and the
commands below are what they will be.

| Channel | Command | Updates with |
|---|---|---|
| Installer (macOS, Linux, WSL) | `curl -fsSL https://raw.githubusercontent.com/PulsarOSDevTeam/pulsarcode/main/install.sh \| bash` | `pulsarcode update` |
| Installer (Windows) | `irm https://raw.githubusercontent.com/PulsarOSDevTeam/pulsarcode/main/install.ps1 \| iex` | `pulsarcode update` |
| A clone | `gh repo clone PulsarOSDevTeam/pulsarcode && bash pulsarcode/install.sh --local` | `git pull`, then the same `install.sh --local` |
| PyPI via pipx (first public release) | `pipx install pulsarcode` | `pipx upgrade pulsarcode` |
| PyPI via uv (first public release) | `uv tool install pulsarcode` | `uv tool upgrade pulsarcode` |
| npm (first public release) | `npm install -g pulsarcode`, or `npx pulsarcode` in a folder you trust | runs the installer once, then `pulsarcode update` |
| Homebrew (first public release) | `brew install pulsarosdevteam/tap/pulsarcode` | `brew upgrade pulsarcode` |
| Scoop (first public release) | `scoop install pulsarcode` (from the Pulsar bucket) | `scoop update pulsarcode` |
| Release archive (first public release) | download `pulsarcode-app-<version>.tar.gz` or `.zip`, unpack, run `bash install.sh --local` / `.\install.ps1 -Local` inside it | rerun the installer |

The official Claude Code CLI is a separate install. The curl and PowerShell installers (and
therefore `npx pulsarcode`) fetch it with Anthropic's installer when it is missing; with pipx,
uv, Homebrew and Scoop install it yourself (`curl -fsSL https://claude.ai/install.sh | bash`).

## PyPI: use pipx or uv tool

```bash
pipx install pulsarcode
# or
uv tool install pulsarcode
```

The PyPI distribution is `pulsarcode`, but the Python import package inside it is `pulsar`.
Apache Pulsar's client library (`pulsar-client`) uses the same import name, so installing both
into one environment makes them overwrite each other. pipx and `uv tool` give Pulsar Code a
virtualenv of its own and avoid that entirely. Plain `pip install pulsarcode` into a shared
environment is not supported.

## npm: `npm install -g pulsarcode` / `npx pulsarcode`

`packaging/npm` is a zero-dependency Node (18+) wrapper. If Pulsar Code is installed
(`~/.pulsarcode/app`, `%USERPROFILE%\.pulsarcode\app`, or `$PULSAR_HOME/app`) it runs it with
your arguments, stdio, signals and exit code. Otherwise it runs the official installer for the
platform with `PULSAR_NO_LAUNCH=1` and then hands over.

* **Pinned to its own version.** `pulsarcode@3.0.0` fetches the installer, and therefore the
  app, from the `v3.0.0` tag, not from `main` (pre-releases map back the same way:
  `3.1.0-rc.1` installs `v3.1.0rc1`). `PULSAR_REF` overrides it and is validated against
  `^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$` (plus git's own ref rules) before use. An existing
  install is used as it is; when it is older than the npm package the wrapper says so and
  suggests `pulsarcode update`.
* **No shell strings.** Every child gets an argv array; the installer URL reaches bash or
  PowerShell as an argument or environment variable, never as interpolated code. bash and
  PowerShell are started by absolute path.
* **npm's PATH is cleaned.** npm and npx put `<cwd>/node_modules/.bin` (and every parent's,
  the npx cache and npm's `node-gyp-bin`) first on PATH and export project `.npmrc` settings
  as `npm_config_*`/`NODE_OPTIONS`. The wrapper removes those PATH entries (and empty or
  relative ones), the `npm_*` variables, and an npm-injected `NODE_OPTIONS` from everything it
  starts, so a repository's `node_modules/.bin/bash`, `python3` or `claude` never runs.

**Do not run `npx pulsarcode` inside a repository you do not trust.** Before any package code
runs, npm itself resolves `node` and `sh` through that same poisoned PATH, applies the
project's `.npmrc` (which can set `node-options=--require ./evil.js` or point `registry=` at
another server, so `npx pulsarcode` would download someone else's package). No package can
prevent that. Install once from a folder you trust and use the global command everywhere:

```bash
npm install -g pulsarcode     # from your home folder, then `pulsarcode` in any project
```

```bash
node --test packaging/npm/test/pulsarcode.test.js   # offline: fake HOME, fake curl, npx run
```

## Homebrew and Scoop

`homebrew/pulsarcode.rb` and `scoop/pulsarcode.json` are the sources for the tap
(`PulsarOSDevTeam/homebrew-tap`, `Formula/pulsarcode.rb`) and the Scoop bucket. Both install
from the release archives. Their `url`, `sha256`/`hash`, `version` and `extract_dir` fields are
rewritten by `tools/release/update_manifests.py`; the zeros in the checked-in copies are
placeholders until the first release run fills them in.

## Release archives

`tools/release/build_archives.py` writes `pulsarcode-app-<version>.tar.gz` and `.zip`: the
engine, the `pulsarcode` shim, both installers, `requirements.txt`, `pyproject.toml`, README,
CHANGELOG, SECURITY and LICENSE under one `pulsarcode-<version>/` folder. The archives are
reproducible (sorted entries, normalised owners and modes, `SOURCE_DATE_EPOCH` timestamps).

## Cutting a release

1. Bump `__version__` in `pulsar/__init__.py` (PEP 440: `3.1.0`, `3.1.0rc1`, `3.1.0.post1`)
   and `version` in `packaging/npm/package.json` to its semver spelling, which
   `python tools/release/update_manifests.py --npm-version 3.1.0rc1` prints (`3.1.0-rc.1`;
   a post-release keeps the npm version of its base release). Then check:
   `python tools/release/update_manifests.py --check-tag v3.1.0rc1`.
2. Push the tag (only repository admins can, see the tag ruleset below).
   `.github/workflows/release.yml` then:
   * re-runs the tests and audits and checks that the tag, `__version__` and the npm version
     agree;
   * builds the sdist and wheel with the hash-pinned backend in
     `tools/release/requirements-build.txt`, both app archives and `SHA256SUMS`, records build
     provenance attestations for all of them (only when the repository is public) and uploads
     them, before anything else runs;
   * checks them in a separate job without write or OIDC access: checksums, `twine check`
     with the same twine as the publish action (`tools/release/requirements-check.txt`), and a
     fresh-venv install from a hostile working directory;
   * creates the GitHub release with every file attached (marked pre-release for a/b/rc/dev);
     the files of an existing release are never replaced: a re-run uploads only missing files
     and fails when a published file differs from the build;
   * once the GitHub release exists, publishes to PyPI with trusted publishing (environment
     `pypi`, no token);
   * once the GitHub release exists, also publishes to npm with trusted publishing (environment
     `npm`, no token, provenance attached when the repository is public) when the repository
     variable `NPM_PUBLISH` is `true`; pre-releases go to the `next` dist-tag, post-releases are
     not published to npm;
   * for stable releases only, opens a pull request that points the Homebrew formula, Scoop
     manifest and npm version at the new release (the files are also uploaded as a workflow
     artifact) and starts the tests workflow on it with `workflow_dispatch`. Pull requests
     opened with `GITHUB_TOKEN` trigger no workflow runs by themselves; if its checks do not
     appear, close and reopen the pull request.
3. Copy `homebrew/pulsarcode.rb` into the tap and `scoop/pulsarcode.json` into the bucket once
   that pull request is merged.

One-time setup (`tools/github/setup.sh --dry-run` shows what the API part does):

* GitHub: `tools/github/setup.sh` creates the `pypi` and `npm` environments (deployments from
  `v*` tags only; `--reviewer LOGIN` adds a required approval to both) and a tag ruleset that
  lets only repository admins create, move or delete `v*` tags, so a collaborator with write
  access cannot publish by pushing a tag. `--community` turns on Issues and Discussions, which
  `CONTRIBUTING.md` and the issue templates rely on. GitHub Pages (the site in `docs/`) is opt-in
  with `--pages`, which refuses while the repository is private.
* PyPI: add a trusted publisher for project `pulsarcode` with owner `PulsarOSDevTeam`,
  repository `pulsarcode`, workflow `release.yml`, environment `pypi`.
* npm: no token is stored in GitHub. Publish the first version by hand (npm only offers
  trusted publishing for an existing package) with `npm publish --access public`; the package
  does not require provenance, so a manual publish works. Then add the trusted publisher on
  npmjs.com (workflow `release.yml`, environment `npm`, allow `npm publish`), set the package to
  "Require two-factor authentication and disallow tokens", and run
  `gh variable set NPM_PUBLISH --body true`.
* Allow GitHub Actions to create pull requests (Settings, Actions, General) if you want the
  manifest pull request; without it the job falls back to the uploaded artifact.
* Turn on release immutability (Settings, General, Releases: "Enable release immutability"), so
  the files and the tag of a published release can never change.

## Verifying a download

The checksum shows a file is intact; the attestation, recorded for releases published while the
repository is public, shows it was built by this repository's release workflow.

```bash
sha256sum -c SHA256SUMS --ignore-missing
gh attestation verify pulsarcode-app-3.0.0.tar.gz --repo PulsarOSDevTeam/pulsarcode
npm audit signatures            # inside a project that depends on pulsarcode
```

## The working-directory guarantee

No channel ever runs `python -m pulsar`: that would put the current directory first on
`sys.path`, so a repository containing its own `pulsar/` (or a `json.py`) would run as you the
moment you start Pulsar Code inside it. The shim (installers and Homebrew), the Windows
launchers, the Scoop launcher and the npm wrapper all use the bootstrap in `pulsar.BOOT` and
start Python with `-E`, so `PYTHONPATH` is ignored too; the npm wrapper also drops npm's
`node_modules/.bin` PATH entries before starting anything. The PyPI console script (pipx, uv
tool) does not put the current directory on its path either, but it is pip's own launcher and
honours `PYTHONPATH`: an empty or relative entry there brings the current folder back, so keep
them out of `PYTHONPATH` (`smoke.py --no-pythonpath` skips that check for such launchers). The
other gap no package can close is npm's own start-up when you type `npx` inside an untrusted
repository (see the npm section). `tools/release/smoke.py` proves the rest for any launcher by
running it from a folder full of booby-trapped modules, a planted `node_modules/.bin` and a
`PYTHONPATH` that names that folder:

```bash
python tools/release/smoke.py --status ~/.local/bin/pulsarcode
python tools/release/smoke.py node packaging/npm/bin/pulsarcode.js
npm pack ./packaging/npm --pack-destination /tmp && \
  python tools/release/smoke.py npx --yes --package /tmp/pulsarcode-3.0.0.tgz pulsarcode
```

Relative launcher paths are made absolute before the run moves into the hostile folder.
