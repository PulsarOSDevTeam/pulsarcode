// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 PulsarOS Intelligence Inc.
//
// node --test packaging/npm/test/pulsarcode.test.js
//
// Runs entirely offline against a fake HOME: a stub app/pulsarcode records its argv and exits
// with a chosen code, and a fake `curl` on PATH stands in for the network during the install
// path. The real ~/.pulsarcode is never touched. The npx test packs this folder and runs the
// tarball through npx from a folder with a booby-trapped node_modules/.bin.

'use strict';

const test = require('node:test');
const assert = require('node:assert');
const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const BIN = path.resolve(__dirname, '..', 'bin', 'pulsarcode.js');
const W = require(BIN);
const PKG = require(path.resolve(__dirname, '..', 'package.json'));
const POSIX = process.platform !== 'win32';
const NPM_CLI = path.join(path.dirname(process.execPath), '..', 'lib', 'node_modules', 'npm', 'bin', 'npm-cli.js');
const NPX_CLI = path.join(path.dirname(NPM_CLI), 'npx-cli.js');

function scratch() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'pulsarcode-npm-'));
}

const STUB = [
  '#!/bin/bash',
  'printf "%s\\0" "$@" > "$STUB_OUT"',
  'printf "PULSAR_HOME=%s\\n" "${PULSAR_HOME:-}" > "$STUB_OUT.env"',
  'exit "${STUB_EXIT:-0}"',
  '',
].join('\n');

function plantApp(home) {
  const app = path.join(home, 'app');
  fs.mkdirSync(app, { recursive: true });
  fs.writeFileSync(path.join(app, 'pulsarcode'), STUB, { mode: 0o755 });
  return app;
}

// A fake curl that "downloads" an installer which plants the stub app, like install.sh would.
function fakeCurl(dir, { fail = false } = {}) {
  const bin = path.join(dir, 'fakebin');
  fs.mkdirSync(bin, { recursive: true });
  const installer = [
    'set -e',
    '[ "$PULSAR_NO_LAUNCH" = "1" ] || { echo "PULSAR_NO_LAUNCH not set" >&2; exit 9; }',
    'home="${PULSAR_HOME:-$HOME/.pulsarcode}"',
    'mkdir -p "$home/app"',
    'cat > "$home/app/pulsarcode" <<\'STUBEOF\'',
    STUB.trimEnd(),
    'STUBEOF',
    'chmod 755 "$home/app/pulsarcode"',
    'echo "installed ref=$PULSAR_REF"',
  ].join('\n');
  fs.writeFileSync(path.join(dir, 'installer.sh'), installer + '\n');
  const curl = [
    '#!/bin/bash',
    'printf "%s\\n" "$@" > "$CURL_LOG"',
    fail ? 'exit 22' : 'cat "$(dirname "$0")/../installer.sh"',
    '',
  ].join('\n');
  fs.writeFileSync(path.join(bin, 'curl'), curl, { mode: 0o755 });
  return bin;
}

function baseEnv(home, extra) {
  return Object.assign({ HOME: home, PATH: '/usr/bin:/bin', LANG: 'C' }, extra || {});
}

function runWrapper(args, env) {
  return spawnSync(process.execPath, [BIN].concat(args), { env, encoding: 'utf8', timeout: 30000 });
}

test('PULSAR_REF validation', () => {
  for (const ok of ['main', 'v3.0.0', 'release/3.1', 'feature_x-1', 'a', 'A'.repeat(64)]) {
    assert.ok(W.validateRef(ok), ok);
  }
  for (const bad of ['', '-x', '.hidden', 'main;id', '$(id)', 'a b', 'x/../../evil', 'a//b', 'a/',
    'a.', 'a/.b', 'x.lock', 'A'.repeat(65), 'main\n', 'ma`in`', 'm"a', "m'a", 'a\\b', null, undefined]) {
    assert.ok(!W.validateRef(bad), String(bad));
  }
});

test('installer commands are argv arrays with constant script text and absolute shells', () => {
  const posix = W.installerCommand('v9.9.9', 'linux', { PATH: '/usr/bin:/bin' });
  assert.ok(path.isAbsolute(posix.command), posix.command);
  assert.strictEqual(path.basename(posix.command), 'bash');
  assert.strictEqual(posix.args[0], '-c');
  assert.ok(!posix.args[1].includes('v9.9.9'), 'ref must not be interpolated into the script');
  assert.ok(posix.args[1].endsWith('| "$BASH"'), 'the installer must run in the same, absolute bash');
  assert.strictEqual(posix.args[3], 'https://raw.githubusercontent.com/PulsarOSDevTeam/pulsarcode/v9.9.9/install.sh');
  assert.strictEqual(posix.env.PULSAR_NO_LAUNCH, '1');
  assert.strictEqual(posix.env.PULSAR_REF, 'v9.9.9');

  const win = W.installerCommand('main', 'win32', { SystemRoot: 'D:\\WINNT' });
  assert.strictEqual(win.command, 'D:\\WINNT\\System32\\WindowsPowerShell\\v1.0\\powershell.exe');
  assert.strictEqual(W.installerCommand('main', 'win32', {}).command,
    'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe');
  assert.deepStrictEqual(win.args.slice(0, 4), ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command']);
  assert.ok(!win.args[4].includes('raw.githubusercontent'), 'URL must come from the environment');
  assert.strictEqual(win.env.PULSARCODE_INSTALL_URL,
    'https://raw.githubusercontent.com/PulsarOSDevTeam/pulsarcode/main/install.ps1');
  assert.strictEqual(win.env.PULSAR_NO_LAUNCH, '1');

  assert.throws(() => W.installerCommand('x;id', 'linux'));
});

test('package version maps to the release tag', () => {
  const cases = {
    '3.0.0': 'v3.0.0',
    '3.1.0-rc.1': 'v3.1.0rc1',
    '3.1.0-alpha.2': 'v3.1.0a2',
    '3.1.0-beta.3': 'v3.1.0b3',
    '3.1.0-dev.4': 'v3.1.0.dev4',
    '3.1.0-rc.1.dev.2': 'v3.1.0rc1.dev2',
    '10.20.30': 'v10.20.30',
  };
  for (const [v, tag] of Object.entries(cases)) assert.strictEqual(W.tagForVersion(v), tag, v);
  for (const bad of ['', 'latest', '3.0', '3.1.0-foo.1', '3.1.0-rc', '3.1.0-rc.1.post.1', null]) {
    assert.strictEqual(W.tagForVersion(bad), null, String(bad));
  }
  assert.strictEqual(W.defaultRef('weird'), 'main');
  assert.strictEqual(W.PKG_VERSION, PKG.version);
  assert.ok(W.validateRef(W.defaultRef(PKG.version)));
  assert.strictEqual(W.defaultRef(PKG.version), W.tagForVersion(PKG.version));
});

test('child environment: npm PATH entries and injected variables are removed', () => {
  const env = {
    PATH: ['/home/u/.npm/_npx/abc/node_modules/.bin', '/repo/node_modules/.bin', '/node_modules/.bin/',
      '/usr/lib/node_modules/npm/node_modules/@npmcli/run-script/lib/node-gyp-bin', '', '.', 'bin',
      '/usr/local/bin', '/usr/bin', '/opt/node_modules/.binx'].join(':'),
    HOME: '/home/u',
    npm_config_node_options: '--require=./evil.js',
    NODE_OPTIONS: '--require=./evil.js',
    npm_execpath: '/usr/lib/node_modules/npm/bin/npm-cli.js',
    npm_config_registry: 'https://evil.invalid/',
    INIT_CWD: '/repo',
    NODE: '/usr/bin/node',
    PULSAR_HOME: '/x',
  };
  const out = W.childEnv(env, 'linux');
  assert.strictEqual(out.PATH, '/usr/local/bin:/usr/bin:/opt/node_modules/.binx');
  for (const k of ['NODE_OPTIONS', 'npm_execpath', 'npm_config_registry', 'npm_config_node_options', 'INIT_CWD', 'NODE']) {
    assert.ok(!(k in out), k);
  }
  assert.strictEqual(out.PULSAR_HOME, '/x');
  assert.strictEqual(out.HOME, '/home/u');
  // A NODE_OPTIONS the user set themselves (not npm's) is kept, and so are NODE/INIT_CWD
  // when npm did not start us.
  const own = W.childEnv({ PATH: '/usr/bin', NODE_OPTIONS: '--max-old-space-size=4096', NODE: '/n' }, 'linux');
  assert.strictEqual(own.NODE_OPTIONS, '--max-old-space-size=4096');
  assert.strictEqual(own.NODE, '/n');
  const win = W.childEnv({ Path: 'C:\\repo\\node_modules\\.bin;C:\\Windows\\system32;;relative;D:\\Tools' }, 'win32');
  assert.strictEqual(win.Path, 'C:\\Windows\\system32;D:\\Tools');
});

test('findLauncher honours HOME and PULSAR_HOME', () => {
  const dir = scratch();
  const home = path.join(dir, 'home');
  assert.strictEqual(W.findLauncher({ HOME: home }, 'linux'), null);
  plantApp(path.join(home, '.pulsarcode'));
  assert.strictEqual(W.findLauncher({ HOME: home }, 'linux').command, path.join(home, '.pulsarcode', 'app', 'pulsarcode'));
  assert.strictEqual(W.pulsarHome({ USERPROFILE: home }, 'win32'), path.join(home, '.pulsarcode'));
  const custom = path.join(dir, 'custom');
  assert.strictEqual(W.findLauncher({ PULSAR_HOME: custom }, 'linux'), null);
  plantApp(custom);
  const l = W.findLauncher({ PULSAR_HOME: custom }, 'linux');
  assert.strictEqual(l.command, path.join(custom, 'app', 'pulsarcode'));
  assert.deepStrictEqual(l.args, []);

  // Windows layout: app\pulsar\__init__.py + venv\Scripts\python.exe, run through BOOT.
  const wh = path.join(dir, 'winhome');
  fs.mkdirSync(path.join(wh, 'app', 'pulsar'), { recursive: true });
  fs.writeFileSync(path.join(wh, 'app', 'pulsar', '__init__.py'), '');
  assert.strictEqual(W.findLauncher({ PULSAR_HOME: wh }, 'win32'), null);
  fs.mkdirSync(path.join(wh, 'venv', 'Scripts'), { recursive: true });
  fs.writeFileSync(path.join(wh, 'venv', 'Scripts', 'python.exe'), '');
  const wl = W.findLauncher({ PULSAR_HOME: wh }, 'win32');
  assert.strictEqual(wl.command, path.join(wh, 'venv', 'Scripts', 'python.exe'));
  assert.deepStrictEqual(wl.args.slice(0, 6), ['-E', '-X', 'utf8', '-B', '-c', W.BOOT]);
  assert.strictEqual(wl.args[7], 'pulsar');
  assert.deepStrictEqual(wl.env, {});
  assert.ok(!wl.args.includes('-m'), 'never python -m pulsar');
  fs.rmSync(dir, { recursive: true, force: true });
});

test('forwards argv verbatim and the exit code (fake HOME)', { skip: !POSIX }, () => {
  const dir = scratch();
  const home = path.join(dir, 'home');
  plantApp(path.join(home, '.pulsarcode'));
  const out = path.join(dir, 'argv.bin');
  const args = ['--continue', 'two words', '$(touch pwned)', ';', '"quoted"', "it's", '', '-p', 'ünï ✓'];
  const r = runWrapper(args, baseEnv(home, { STUB_OUT: out, STUB_EXIT: '7' }));
  assert.strictEqual(r.status, 7, r.stderr);
  const got = fs.readFileSync(out, 'utf8').split('\0');
  got.pop();
  assert.deepStrictEqual(got, args);
  assert.ok(!fs.existsSync(path.join(dir, 'pwned')));
  assert.ok(!fs.existsSync(path.join(home, 'pwned')));

  const ok = runWrapper(['version'], baseEnv(home, { STUB_OUT: out, STUB_EXIT: '0' }));
  assert.strictEqual(ok.status, 0);
  fs.rmSync(dir, { recursive: true, force: true });
});

test('PULSAR_HOME wins over HOME', { skip: !POSIX }, () => {
  const dir = scratch();
  const home = path.join(dir, 'home');
  fs.mkdirSync(home);
  const custom = path.join(dir, 'elsewhere');
  plantApp(custom);
  const out = path.join(dir, 'argv.bin');
  const r = runWrapper(['status'], baseEnv(home, { PULSAR_HOME: custom, STUB_OUT: out, STUB_EXIT: '3' }));
  assert.strictEqual(r.status, 3, r.stderr);
  assert.strictEqual(fs.readFileSync(out, 'utf8'), 'status\0');
  assert.match(fs.readFileSync(out + '.env', 'utf8'), new RegExp('PULSAR_HOME=' + custom));
  fs.rmSync(dir, { recursive: true, force: true });
});

test('a child killed by a signal kills the wrapper the same way', { skip: !POSIX }, () => {
  const dir = scratch();
  const home = path.join(dir, 'home');
  const app = plantApp(path.join(home, '.pulsarcode'));
  fs.writeFileSync(path.join(app, 'pulsarcode'), '#!/bin/bash\nkill -TERM $$\nsleep 5\n', { mode: 0o755 });
  const r = runWrapper([], baseEnv(home));
  assert.strictEqual(r.signal, 'SIGTERM', 'status=' + r.status + ' stderr=' + r.stderr);
  fs.rmSync(dir, { recursive: true, force: true });
});

test('SIGTERM sent to the wrapper reaches the child', { skip: !POSIX }, async () => {
  const dir = scratch();
  const home = path.join(dir, 'home');
  const app = plantApp(path.join(home, '.pulsarcode'));
  const ready = path.join(dir, 'ready');
  const got = path.join(dir, 'got');
  fs.writeFileSync(path.join(app, 'pulsarcode'), [
    '#!/bin/bash',
    'trap \'echo TERM > "$GOT"; exit 42\' TERM',
    'touch "$READY"',
    'for i in $(seq 1 200); do sleep 0.05; done',
    'exit 0',
    '',
  ].join('\n'), { mode: 0o755 });
  const child = spawn(process.execPath, [BIN], { env: baseEnv(home, { READY: ready, GOT: got }), stdio: 'ignore' });
  const deadline = Date.now() + 10000;
  while (!fs.existsSync(ready) && Date.now() < deadline) await new Promise((r) => setTimeout(r, 25));
  assert.ok(fs.existsSync(ready), 'stub never started');
  child.kill('SIGTERM');
  const [code, signal] = await new Promise((resolve) => child.on('exit', (c, s) => resolve([c, s])));
  assert.strictEqual(signal, null);
  assert.strictEqual(code, 42);
  assert.strictEqual(fs.readFileSync(got, 'utf8').trim(), 'TERM');
  fs.rmSync(dir, { recursive: true, force: true });
});

test('not installed: runs the installer with PULSAR_NO_LAUNCH=1 and PULSAR_REF, then hands over', { skip: !POSIX }, () => {
  const dir = scratch();
  const home = path.join(dir, 'home');
  fs.mkdirSync(home);
  const bin = fakeCurl(dir);
  const out = path.join(dir, 'argv.bin');
  const log = path.join(dir, 'curl.log');
  const env = baseEnv(home, { PATH: bin + ':/usr/bin:/bin', CURL_LOG: log, STUB_OUT: out, STUB_EXIT: '5',
    PULSAR_REF: 'v9.9.9' });
  const r = runWrapper(['pick', 'a b'], env);
  assert.strictEqual(r.status, 5, r.stderr);
  const curlArgs = fs.readFileSync(log, 'utf8').trim().split('\n');
  assert.strictEqual(curlArgs[curlArgs.length - 1],
    'https://raw.githubusercontent.com/PulsarOSDevTeam/pulsarcode/v9.9.9/install.sh');
  assert.ok(fs.existsSync(path.join(home, '.pulsarcode', 'app', 'pulsarcode')));
  assert.strictEqual(fs.readFileSync(out, 'utf8'), 'pick\0a b\0');
  assert.match(r.stdout, /installed ref=v9\.9\.9/);

  // Second run: already installed, the installer is not touched again.
  fs.unlinkSync(log);
  const again = runWrapper(['version'], env);
  assert.strictEqual(again.status, 5);
  assert.ok(!fs.existsSync(log));
  fs.rmSync(dir, { recursive: true, force: true });
});

test('defaults to the tag of this package version and respects PULSAR_HOME during install', { skip: !POSIX }, () => {
  const dir = scratch();
  const home = path.join(dir, 'home');
  fs.mkdirSync(home);
  const custom = path.join(dir, 'ph');
  const bin = fakeCurl(dir);
  const log = path.join(dir, 'curl.log');
  const out = path.join(dir, 'argv.bin');
  const r = runWrapper([], baseEnv(home, { PATH: bin + ':/usr/bin:/bin', CURL_LOG: log, STUB_OUT: out,
    PULSAR_HOME: custom }));
  assert.strictEqual(r.status, 0, r.stderr);
  const tag = W.tagForVersion(PKG.version);
  assert.ok(tag && tag.startsWith('v'), tag);
  assert.strictEqual(fs.readFileSync(log, 'utf8').trim().split('\n').pop(),
    'https://raw.githubusercontent.com/PulsarOSDevTeam/pulsarcode/' + tag + '/install.sh');
  assert.match(r.stderr, new RegExp('installer \\(' + tag.replace(/\./g, '\\.') + '\\)'));
  assert.ok(fs.existsSync(path.join(custom, 'app', 'pulsarcode')));
  assert.ok(!fs.existsSync(path.join(home, '.pulsarcode')));
  fs.rmSync(dir, { recursive: true, force: true });
});

test('rejects a bad PULSAR_REF before any download', { skip: !POSIX }, () => {
  const dir = scratch();
  const home = path.join(dir, 'home');
  fs.mkdirSync(home);
  const bin = fakeCurl(dir);
  const log = path.join(dir, 'curl.log');
  for (const ref of ['main; touch pwned', 'x/../../../evil/repo/main', '$(id)', '-oops']) {
    const r = runWrapper([], baseEnv(home, { PATH: bin + ':/usr/bin:/bin', CURL_LOG: log, PULSAR_REF: ref }));
    assert.strictEqual(r.status, 2, ref);
    assert.match(r.stderr, /PULSAR_REF/);
    assert.ok(!fs.existsSync(log), 'curl must not run for ' + ref);
  }
  assert.ok(!fs.existsSync(path.join(home, 'pwned')));
  fs.rmSync(dir, { recursive: true, force: true });
});

test('a failed install is reported and nothing is launched', { skip: !POSIX }, () => {
  const dir = scratch();
  const home = path.join(dir, 'home');
  fs.mkdirSync(home);
  const bin = fakeCurl(dir, { fail: true });
  const log = path.join(dir, 'curl.log');
  const r = runWrapper(['version'], baseEnv(home, { PATH: bin + ':/usr/bin:/bin', CURL_LOG: log }));
  assert.notStrictEqual(r.status, 0);
  assert.match(r.stderr, /installer failed/);
  assert.ok(!fs.existsSync(path.join(home, '.pulsarcode', 'app')));
  fs.rmSync(dir, { recursive: true, force: true });
});

// ---------------------------------------------------------------- hostile working directory

// A folder like a hostile repository: node_modules/.bin holds commands that leave a marker.
// `node` and `sh` are left out when npm itself runs the package: npm resolves both through
// that same PATH before any package code starts (see tools/release/smoke.py).
function hostileRepo(dir, { forNpm = false } = {}) {
  const repo = path.join(dir, 'hostile repo');
  const nbin = path.join(repo, 'node_modules', '.bin');
  fs.mkdirSync(nbin, { recursive: true });
  const mark = path.join(dir, 'PWNED.marker');
  const names = ['bash', 'env', 'claude', 'python3', 'curl', 'wget', 'dirname', 'readlink'];
  if (!forNpm) names.push('sh', 'node');
  for (const name of names) {
    fs.writeFileSync(path.join(nbin, name),
      '#!/bin/sh\necho "' + name + '" >> "' + mark + '"\necho "PWNED by node_modules/.bin/' + name + '"\nexit 66\n',
      { mode: 0o755 });
  }
  return { repo, nbin, mark };
}

// An app shim like the real one (#!/usr/bin/env bash), reporting what it would run.
const ENV_STUB = [
  '#!/usr/bin/env bash',
  'printf "%s\\0" "$@" > "$STUB_OUT"',
  'printf "PATH=%s\\n" "$PATH" > "$STUB_OUT.env"',
  'printf "claude=%s\\n" "$(command -v claude || echo none)" >> "$STUB_OUT.env"',
  'printf "python3=%s\\n" "$(command -v python3 || echo none)" >> "$STUB_OUT.env"',
  'printf "NODE_OPTIONS=%s\\n" "${NODE_OPTIONS:-}" >> "$STUB_OUT.env"',
  'exit "${STUB_EXIT:-0}"',
  '',
].join('\n');

test('npm-style PATH: node_modules/.bin commands never run (installed app)', { skip: !POSIX }, () => {
  const dir = scratch();
  const home = path.join(dir, 'home');
  const app = plantApp(path.join(home, '.pulsarcode'));
  fs.writeFileSync(path.join(app, 'pulsarcode'), ENV_STUB, { mode: 0o755 });
  const { repo, nbin, mark } = hostileRepo(dir);
  const out = path.join(dir, 'argv.bin');
  const env = baseEnv(home, {
    PATH: nbin + ':' + path.join(repo, '..', 'node_modules', '.bin') + ':/usr/bin:/bin',
    STUB_OUT: out, STUB_EXIT: '4', npm_execpath: '/x/npm-cli.js',
    // what npm exports for a project .npmrc `node-options=` (harmless value: it also applies to
    // this wrapper process here)
    npm_config_node_options: '--max-old-space-size=1234', NODE_OPTIONS: '--max-old-space-size=1234',
  });
  const r = spawnSync(process.execPath, [BIN, 'status'], { env, cwd: repo, encoding: 'utf8', timeout: 30000 });
  assert.strictEqual(r.status, 4, r.stdout + r.stderr);
  assert.ok(!fs.existsSync(mark), 'planted command ran: ' + (fs.existsSync(mark) ? fs.readFileSync(mark, 'utf8') : ''));
  assert.doesNotMatch(r.stdout + r.stderr, /PWNED/);
  const seen = fs.readFileSync(out + '.env', 'utf8');
  assert.match(seen, /^PATH=\/usr\/bin:\/bin$/m);
  assert.doesNotMatch(seen, /node_modules/);
  assert.match(seen, /^NODE_OPTIONS=$/m);
  fs.rmSync(dir, { recursive: true, force: true });
});

test('npm-style PATH: the installer runs with an absolute bash and a clean PATH', { skip: !POSIX }, () => {
  const dir = scratch();
  const home = path.join(dir, 'home');
  fs.mkdirSync(home);
  const bin = fakeCurl(dir);
  const { repo, nbin, mark } = hostileRepo(dir);
  const log = path.join(dir, 'curl.log');
  const out = path.join(dir, 'argv.bin');
  const env = baseEnv(home, { PATH: nbin + ':' + bin + ':/usr/bin:/bin', CURL_LOG: log, STUB_OUT: out,
    PULSAR_REF: 'v9.9.9' });
  const r = spawnSync(process.execPath, [BIN, 'version'], { env, cwd: repo, encoding: 'utf8', timeout: 30000 });
  assert.strictEqual(r.status, 0, r.stdout + r.stderr);
  assert.ok(!fs.existsSync(mark), 'planted command ran: ' + (fs.existsSync(mark) ? fs.readFileSync(mark, 'utf8') : ''));
  assert.ok(fs.existsSync(log), 'the safe curl was used');
  assert.strictEqual(fs.readFileSync(out, 'utf8'), 'version\0');
  fs.rmSync(dir, { recursive: true, force: true });
});

test('an older installed app is still used, with a note', { skip: !POSIX }, () => {
  const dir = scratch();
  const home = path.join(dir, 'home');
  const app = plantApp(path.join(home, '.pulsarcode'));
  fs.mkdirSync(path.join(app, 'pulsar'));
  fs.writeFileSync(path.join(app, 'pulsar', '__init__.py'), '__version__ = "0.0.1"\n');
  const out = path.join(dir, 'argv.bin');
  const r = runWrapper(['version'], baseEnv(home, { STUB_OUT: out }));
  assert.strictEqual(r.status, 0, r.stderr);
  assert.match(r.stderr, /Pulsar Code 0\.0\.1 is installed; this npm package is /);
  assert.ok(W.isOlder('2.9.9', '3.0.0') && !W.isOlder('3.0.0', '3.0.0') && !W.isOlder('3.1.0', '3.0.0'));
  assert.ok(!W.isOlder('', '3.0.0') && W.isOlder('3.0.0', '3.0.1-rc.1'));
  fs.writeFileSync(path.join(app, 'pulsar', '__init__.py'), '__version__ = "99.0.0"\n');
  const quiet = runWrapper(['version'], baseEnv(home, { STUB_OUT: out }));
  assert.strictEqual(quiet.stderr, '');
  fs.rmSync(dir, { recursive: true, force: true });
});

test('npx --package <tarball> from a hostile folder runs nothing from node_modules/.bin',
  { skip: !POSIX || !fs.existsSync(NPX_CLI) ? 'needs POSIX and the npm bundled with this node' : false, timeout: 120000 },
  () => {
    const dir = scratch();
    const home = path.join(dir, 'home');
    fs.mkdirSync(home);
    const app = plantApp(path.join(dir, 'ph'));
    fs.writeFileSync(path.join(app, 'pulsarcode'), ENV_STUB, { mode: 0o755 });
    const packDir = path.join(dir, 'pack');
    fs.mkdirSync(packDir);
    const npmEnv = { HOME: home, PATH: path.dirname(process.execPath) + ':/usr/bin:/bin', LANG: 'C',
      npm_config_cache: path.join(dir, 'npm-cache'), npm_config_update_notifier: 'false',
      npm_config_audit: 'false', npm_config_fund: 'false' };
    const pack = spawnSync(process.execPath, [NPM_CLI, 'pack', '--silent', '--pack-destination', packDir],
      { cwd: path.resolve(__dirname, '..'), env: npmEnv, encoding: 'utf8', timeout: 60000 });
    assert.strictEqual(pack.status, 0, pack.stderr);
    const tgz = path.join(packDir, fs.readdirSync(packDir).find((f) => f.endsWith('.tgz')));
    const { repo, mark } = hostileRepo(dir, { forNpm: true });
    const out = path.join(dir, 'argv.bin');
    const r = spawnSync(process.execPath, [NPX_CLI, '--yes', '--package', tgz, 'pulsarcode', 'status', 'a b'], {
      cwd: repo, encoding: 'utf8', timeout: 90000,
      env: Object.assign({}, npmEnv, { PULSAR_HOME: path.join(dir, 'ph'), STUB_OUT: out, STUB_EXIT: '3' }),
    });
    assert.strictEqual(r.status, 3, r.stdout + r.stderr);
    assert.ok(!fs.existsSync(mark), 'planted command ran: ' + (fs.existsSync(mark) ? fs.readFileSync(mark, 'utf8') : ''));
    assert.strictEqual(fs.readFileSync(out, 'utf8'), 'status\0a b\0');
    const seen = fs.readFileSync(out + '.env', 'utf8');
    assert.doesNotMatch(seen, /node_modules|node-gyp-bin/);
    fs.rmSync(dir, { recursive: true, force: true });
  });
