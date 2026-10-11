#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 PulsarOS Intelligence Inc.
//
// `npx pulsarcode`: run Pulsar Code in one command.
//
// If Pulsar Code is already installed (~/.pulsarcode/app, or $PULSAR_HOME/app) this hands
// over to it with the same arguments, stdio, signals and exit code. Otherwise it first runs
// the official installer for this platform (the same one-liner the README documents) with
// PULSAR_NO_LAUNCH=1, then hands over. The installer comes from the git tag matching this
// package's version (pulsarcode@3.0.0 installs v3.0.0); PULSAR_REF overrides it.
//
// Zero dependencies, Node >= 18. Nothing here goes through a shell string built from the
// environment or argv: every child gets an argv array, and the installer URL reaches bash or
// PowerShell as an argument / environment variable, never as interpolated code.
//
// npm and npx put <cwd>/node_modules/.bin (and every parent's) first on PATH and export
// project .npmrc settings. A repository could plant node_modules/.bin/bash or .../claude and
// have them run as you, so every child gets a PATH without those entries and without npm's
// injected variables, and the installer's shell is started by absolute path.

'use strict';

const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const REPO = 'PulsarOSDevTeam/pulsarcode';
const RAW = 'https://raw.githubusercontent.com/' + REPO + '/';
const REF_RE = /^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$/;
const PKG_VERSION = (() => {
  try {
    return require('../package.json').version || '';
  } catch (_) {
    return '';
  }
})();

// Must stay identical to pulsar.BOOT (tests/test_packaging.py checks it). Running
// `python -m pulsar` would put the current directory first on sys.path, so a project that
// contains its own `pulsar/` (or a file shadowing the standard library) would run as you.
// This drops the current directory and pins the app dir before anything is imported.
const BOOT = "import sys;r=sys.argv.pop(1);m=sys.argv.pop(1);" +
  "sys.path[:]=[r]+[p for p in sys.path if p not in ('','.',r)];" +
  "import runpy;sys.argv[0]=m;runpy.run_module(m,run_name='__main__',alter_sys=True)";

function validateRef(ref) {
  if (typeof ref !== 'string' || !REF_RE.test(ref)) return false;
  // Git refuses these in ref names, and curl would normalise `..` segments in the raw URL
  // into a path outside this repository.
  if (ref.includes('..') || ref.includes('//') || ref.endsWith('/') || ref.endsWith('.')) return false;
  if (ref.split('/').some((part) => part.startsWith('.') || part.endsWith('.lock'))) return false;
  return true;
}

// npm versions are semver; Pulsar Code tags are v + the PEP 440 version. tools/release/
// update_manifests.py maps one way (3.1.0rc1 -> 3.1.0-rc.1), this maps back to the tag.
const PRE = { alpha: 'a', beta: 'b', rc: 'rc' };
function tagForVersion(version) {
  const m = /^(\d+\.\d+\.\d+)(?:-(.+))?$/.exec(String(version || ''));
  if (!m) return null;
  if (!m[2]) return 'v' + m[1];
  const parts = m[2].split('.');
  let out = m[1];
  if (parts.length >= 2 && PRE[parts[0]] && /^\d+$/.test(parts[1])) {
    out += PRE[parts[0]] + Number(parts[1]);
    parts.splice(0, 2);
  }
  if (parts.length === 2 && parts[0] === 'dev' && /^\d+$/.test(parts[1])) {
    out += '.dev' + Number(parts[1]);
    parts.splice(0, 2);
  }
  return parts.length ? null : 'v' + out;
}

function defaultRef(version) {
  return tagForVersion(version) || 'main';
}

// The PATH entries npm/npx add (<dir>/node_modules/.bin for the cwd and each parent, the npx
// cache, npm's node-gyp-bin), plus empty and relative entries, which mean "the current
// directory". Everything else stays in its original order.
function isUnsafePathEntry(entry) {
  if (!entry) return true;
  const e = entry.replace(/\\/g, '/').replace(/\/+$/, '');
  if (!(e.startsWith('/') || /^[A-Za-z]:\//.test(e) || e.startsWith('//'))) return true;
  const low = e.toLowerCase();
  return low.endsWith('/node_modules/.bin') || low.includes('/node-gyp-bin');
}

function cleanPath(value, platform) {
  const sep = platform === 'win32' ? ';' : ':';
  return String(value || '').split(sep).filter((p) => !isUnsafePathEntry(p)).join(sep);
}

// The environment every child gets: the caller's, minus what npm injected.
function childEnv(env, platform) {
  const out = {};
  const fromNpm = Boolean(env.npm_execpath || env.npm_command || env.npm_lifecycle_event ||
    env.npm_config_user_agent);
  for (const [key, value] of Object.entries(env)) {
    if (value === undefined) continue;
    if (/^npm_/i.test(key)) continue;
    if (fromNpm && (key === 'INIT_CWD' || key === 'NODE')) continue;
    out[key] = value;
  }
  // A project .npmrc `node-options=` arrives as NODE_OPTIONS (--require ./evil.js). It must not
  // follow us into Claude Code, which is a Node program too.
  if (env.npm_config_node_options && env.NODE_OPTIONS === env.npm_config_node_options) {
    delete out.NODE_OPTIONS;
  }
  for (const key of Object.keys(out)) {
    if (/^path$/i.test(key)) out[key] = cleanPath(out[key], platform);
  }
  return out;
}

function isExecutable(p) {
  try {
    fs.accessSync(p, fs.constants.X_OK);
    return fs.statSync(p).isFile();
  } catch (_) {
    return false;
  }
}

// bash for the POSIX installer, by absolute path: never whatever `bash` comes first on PATH.
function findBash(env) {
  for (const p of ['/bin/bash', '/usr/bin/bash', '/usr/local/bin/bash', '/opt/homebrew/bin/bash',
    '/run/current-system/sw/bin/bash']) {
    if (isExecutable(p)) return p;
  }
  for (const dir of String(env.PATH || '').split(':')) {
    if (!isUnsafePathEntry(dir) && isExecutable(path.join(dir, 'bash'))) return path.join(dir, 'bash');
  }
  return null;
}

// Windows PowerShell by absolute path (libuv would otherwise look in the current directory
// before PATH for a bare `powershell.exe`).
function powershellPath(env) {
  const root = env.SystemRoot || env.SYSTEMROOT || env.windir || env.WINDIR || 'C:\\Windows';
  return path.win32.join(root, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
}

function pulsarHome(env, platform) {
  if (env.PULSAR_HOME) return path.resolve(env.PULSAR_HOME);
  const home = platform === 'win32' ? (env.USERPROFILE || env.HOME) : env.HOME;
  return path.join(home ? path.resolve(home) : os.homedir(), '.pulsarcode');
}

function isFile(p) {
  try {
    return fs.statSync(p).isFile();
  } catch (_) {
    return false;
  }
}

// What to run for an installed Pulsar Code, or null when it is not installed.
function findLauncher(env, platform) {
  const home = pulsarHome(env, platform);
  const app = path.join(home, 'app');
  if (platform === 'win32') {
    // install.ps1 layout: app\pulsar\ + venv\Scripts\python.exe (+ a .cmd shim we avoid,
    // because .cmd files can only be started through cmd.exe and its quoting rules).
    const py = path.join(home, 'venv', 'Scripts', 'python.exe');
    if (!isFile(path.join(app, 'pulsar', '__init__.py')) || !isFile(py)) return null;
    return {
      command: py,
      // Same flags as every installed shim: -E so a PYTHONPATH naming the current folder
      // cannot run its sitecustomize.py before BOOT, -X utf8 instead of PYTHONUTF8.
      args: ['-E', '-X', 'utf8', '-B', '-c', BOOT, app.split('\\').join('/'), 'pulsar'],
      env: {},
    };
  }
  const shim = path.join(app, 'pulsarcode');
  if (!isFile(shim)) return null;
  return { command: shim, args: [], env: {} };
}

// The official installer for this platform, as an argv array plus extra environment.
// `env` must already be the cleaned child environment (it is used to locate the shell).
function installerCommand(ref, platform, env) {
  if (!validateRef(ref)) throw new Error('invalid PULSAR_REF');
  env = env || {};
  if (platform === 'win32') {
    const url = RAW + ref + '/install.ps1';
    // Constant script text; the URL arrives through the environment.
    const script =
      '$ErrorActionPreference = "Stop"; ' +
      '[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072; ' +
      'Invoke-RestMethod -Uri $env:PULSARCODE_INSTALL_URL | Invoke-Expression';
    return {
      command: powershellPath(env),
      args: ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script],
      env: { PULSARCODE_INSTALL_URL: url, PULSAR_REF: ref, PULSAR_NO_LAUNCH: '1' },
      url,
    };
  }
  const url = RAW + ref + '/install.sh';
  // Constant script text; the URL is positional parameter $1, and the downloaded installer
  // runs in the same bash ("$BASH" is set by bash itself to its own absolute path).
  const script =
    'set -o pipefail; ' +
    'if command -v curl >/dev/null 2>&1; then curl -fsSL --retry 3 "$1"; ' +
    'elif command -v wget >/dev/null 2>&1; then wget -qO- "$1"; ' +
    'else echo "pulsarcode: curl or wget is required" >&2; exit 127; fi | "$BASH"';
  return {
    command: findBash(env) || '/bin/bash',
    args: ['-c', script, 'pulsarcode-install', url],
    env: { PULSAR_REF: ref, PULSAR_NO_LAUNCH: '1' },
    url,
  };
}

// Version of an installed app (pulsar/__init__.py), or '' when it cannot be read.
function installedVersion(env, platform) {
  try {
    const src = fs.readFileSync(path.join(pulsarHome(env, platform), 'app', 'pulsar', '__init__.py'), 'utf8');
    const m = /^__version__\s*=\s*["']([^"']+)["']/m.exec(src);
    return m ? m[1] : '';
  } catch (_) {
    return '';
  }
}

function releaseTuple(version) {
  const m = /^v?(\d+)\.(\d+)\.(\d+)/.exec(String(version || ''));
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}

// True when `installed` is an older X.Y.Z than `wanted` (pre-release suffixes ignored).
function isOlder(installed, wanted) {
  const a = releaseTuple(installed);
  const b = releaseTuple(wanted);
  if (!a || !b) return false;
  for (let i = 0; i < 3; i++) {
    if (a[i] !== b[i]) return a[i] < b[i];
  }
  return false;
}

function signalNumber(sig) {
  return (os.constants && os.constants.signals && os.constants.signals[sig]) || 1;
}

// Run a child with inherited stdio. Signals: Ctrl-C and Ctrl-\ typed in a terminal already
// reach the whole foreground process group, so forwarding them would deliver them twice
// (Claude Code treats a second Ctrl-C as "quit"); they are only relayed when there is no
// terminal. SIGTERM and SIGHUP aimed at this process are relayed to the child.
function runChild(command, args, env, platform) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(command, args, { stdio: 'inherit', env, windowsHide: false });
    } catch (err) {
      resolve({ code: null, signal: null, error: err });
      return;
    }
    const tty = Boolean(process.stdin.isTTY);
    const handlers = [];
    const on = (sig, fn) => {
      try {
        process.on(sig, fn);
        handlers.push([sig, fn]);
      } catch (_) {
        // signal not supported on this platform
      }
    };
    if (platform === 'win32') {
      // The console delivers Ctrl-C / Ctrl-Break to every attached process, and
      // child.kill() would terminate rather than signal. Stay alive and let the child decide.
      on('SIGINT', () => {});
      on('SIGBREAK', () => {});
    } else {
      for (const sig of ['SIGINT', 'SIGQUIT']) {
        on(sig, () => {
          if (!tty) {
            try { child.kill(sig); } catch (_) { /* already gone */ }
          }
        });
      }
      for (const sig of ['SIGTERM', 'SIGHUP']) {
        on(sig, () => {
          try { child.kill(sig); } catch (_) { /* already gone */ }
        });
      }
    }
    const cleanup = () => {
      for (const [sig, fn] of handlers) process.removeListener(sig, fn);
    };
    child.on('error', (err) => {
      cleanup();
      resolve({ code: null, signal: null, error: err });
    });
    child.on('exit', (code, signal) => {
      cleanup();
      resolve({ code, signal, error: null });
    });
  });
}

function finish(res) {
  if (res.error) {
    const code = res.error.code === 'ENOENT' ? 127 : 126;
    process.stderr.write('pulsarcode: could not start ' + (res.error.path || 'child') + ': ' +
      res.error.message + '\n');
    process.exit(code);
  }
  if (res.signal) {
    // Die the same way the child did, so shells and supervisors see the real cause.
    const fallback = 128 + signalNumber(res.signal);
    setTimeout(() => process.exit(fallback), 1000).unref();
    try {
      process.kill(process.pid, res.signal);
    } catch (_) {
      process.exit(fallback);
    }
    return;
  }
  process.exit(typeof res.code === 'number' ? res.code : 1);
}

async function main(argv, env, platform, pkgVersion) {
  const version = pkgVersion === undefined ? PKG_VERSION : pkgVersion;
  const cenv = childEnv(env, platform);
  let launcher = findLauncher(env, platform);
  if (!launcher) {
    const ref = env.PULSAR_REF ? env.PULSAR_REF : defaultRef(version);
    if (!validateRef(ref)) {
      process.stderr.write('pulsarcode: PULSAR_REF must be a branch or tag name ' +
        '(letters, digits, . _ / -; at most 64 characters).\n');
      return { code: 2, signal: null, error: null };
    }
    const inst = installerCommand(ref, platform, cenv);
    process.stderr.write('pulsarcode: Pulsar Code is not installed yet. Running the official ' +
      'installer (' + ref + ').\n  ' + inst.url + '\n\n');
    const res = await runChild(inst.command, inst.args, Object.assign({}, cenv, inst.env), platform);
    if (res.error || res.signal || res.code !== 0) {
      if (!res.error && !res.signal) {
        process.stderr.write('pulsarcode: the installer failed (exit ' + res.code + ').\n');
      }
      return res;
    }
    launcher = findLauncher(env, platform);
    if (!launcher) {
      process.stderr.write('pulsarcode: the installer finished but ' +
        path.join(pulsarHome(env, platform), 'app') + ' is still missing.\n');
      return { code: 1, signal: null, error: null };
    }
  } else if (!env.PULSAR_REF && isOlder(installedVersion(env, platform), version)) {
    // An older install is used as is (it may hold local changes); say how to catch up.
    process.stderr.write('pulsarcode: Pulsar Code ' + installedVersion(env, platform) +
      ' is installed; this npm package is ' + version + '. Run `pulsarcode update` to upgrade.\n');
  }
  return runChild(launcher.command, launcher.args.concat(argv),
    Object.assign({}, cenv, launcher.env), platform);
}

module.exports = {
  BOOT, REF_RE, PKG_VERSION, validateRef, tagForVersion, defaultRef, isUnsafePathEntry, cleanPath,
  childEnv, findBash, powershellPath, pulsarHome, findLauncher, installerCommand, installedVersion,
  isOlder, main,
};

if (require.main === module) {
  main(process.argv.slice(2), process.env, process.platform).then(finish, (err) => {
    process.stderr.write('pulsarcode: ' + (err && err.message ? err.message : String(err)) + '\n');
    process.exit(1);
  });
}
