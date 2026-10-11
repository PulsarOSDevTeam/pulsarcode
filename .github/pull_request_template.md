<!--
Read CONTRIBUTING.md before opening a pull request: an outside pull request is merged only when
the change was agreed with the maintainers in an issue first. Never include API keys, tokens or
logs that contain them.
-->

## What and why

<!-- One or two sentences: the change and the reason for it. Link the issue where it was agreed. -->

## How it was tested

<!-- Commands you ran and on which OS / Python / provider. -->

- [ ] `PULSAR_OFFLINE=1 python -m pytest -q`
- [ ] `python -m pyflakes pulsar tests tools`
- [ ] `node --test packaging/npm/test/pulsarcode.test.js` (if packaging/npm changed)
- [ ] `bash -n pulsarcode install.sh` (if shell changed)
- [ ] Tried it for real: `pulsarcode` with provider ______ and model ______

## Checklist

- [ ] Works on Python 3.9
- [ ] No `python -m pulsar` (child processes go through `pulsar.python_argv` / `pulsar.BOOT`)
- [ ] No em dash characters; no secrets, keys or tokens anywhere in the diff
- [ ] README / docs / CHANGELOG updated if behaviour changed
- [ ] New files carry the SPDX header (AGPL-3.0-or-later)
