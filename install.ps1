# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 PulsarOS Intelligence Inc.
#
# Pulsar Code installer for Windows (Windows PowerShell 5.1+ and PowerShell 7+):
#
#   irm https://raw.githubusercontent.com/PulsarOSDevTeam/pulsarcode/main/install.ps1 | iex
#
# Fetches uv (for a private Python), the Pulsar Code engine, Claude Code (official installer)
# if missing, and puts `pulsarcode` on your PATH for PowerShell, cmd.exe and Git Bash.
# Knobs: $env:PULSAR_REF (tag, branch or commit), $env:PULSAR_HOME, $env:PULSAR_BIN_DIR,
#        $env:PULSAR_NO_LAUNCH = "1", $env:PULSAR_SKIP_CLAUDE = "1", $env:PULSAR_NO_PATH = "1",
#        $env:NO_COLOR
#
# Which source gets installed (same rules as install.sh):
#   PULSAR_REF=v3.1.0 (a release tag): the release's own zip, checked against the release's
#       SHA256SUMS before it is unpacked. A mismatch, a missing entry or a SHA256SUMS that cannot be
#       fetched stops the install. Only a tag whose release publishes no SHA256SUMS at all (older
#       tags) falls back to GitHub's tag archive, with a warning.
#   PULSAR_REF=main (the default) or any branch or commit: GitHub's archive over HTTPS, without a
#       checksum (a branch has none). `pulsarcode update` installs the latest release tag.
#   -Local / $env:PULSAR_SRC: a checkout or an unpacked release archive you already have.
#
# Install a checkout or an unpacked release archive instead of downloading:
#   .\install.ps1 -Local        (or $env:PULSAR_SRC = "C:\path\to\pulsarcode")
# Only on request, and only from a folder you own: the folder this script sits in (%TEMP% during
# `pulsarcode update`) is never taken as the source by itself. $env:PULSAR_NO_LOCAL_SRC = "1" (set by
# `pulsarcode update`) forbids local sources.
#
# This file is pure ASCII on purpose: Windows PowerShell 5.1 reads a script file without a BOM in
# the ANSI code page, where UTF-8 glyphs turn into stray quote characters and the whole script fails
# to parse. Glyphs are built from [char] codes at run time instead.
#
# Everything runs inside one script block, so under `irm | iex` nothing is left behind in your
# session (no variables, functions, $ErrorActionPreference, StrictMode or environment changes;
# only PATH gains the bin folder unless PULSAR_NO_PATH=1), and a failure prints the reason instead
# of closing the window. Run as a file, a failure ends the script with exit code 1. Nested
# installers (uv, Claude Code) run in their own PowerShell process for the same reason.
#
# Ctrl+C and cmd.exe: a batch file that is interrupted asks "Terminate batch job (Y/N)?" once the
# program it started returns, and no line inside a .cmd can prevent that. So PowerShell gets
# pulsarcode.ps1 (no batch file involved) whenever the execution policy allows scripts, Git Bash
# gets a POSIX sh launcher, and only cmd.exe itself uses pulsarcode.cmd. Claude Code reads Ctrl+C
# as a key while it runs, so the prompt can only appear for a Ctrl+C during Pulsar's own screens;
# either answer is harmless there, because the launcher has nothing left to do.

& {
Set-StrictMode -Off
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# Run as a file (.\install.ps1, powershell -File) or as text (irm | iex)?
$AsFile = [bool]({}.File)
$ScriptDir = ''
if ($AsFile) { $ScriptDir = Split-Path -Parent ({}.File) }

$Repo = 'PulsarOSDevTeam/pulsarcode'
$Ref = if ($env:PULSAR_REF) { $env:PULSAR_REF } else { 'main' }
# uv is pinned to one release and its installer is checked against this digest before it runs.
$UvVersion = '0.12.22'
$UvInstallerSha256 = '68E1F80E025C21E418F82C817DE9BCBF96CE7FED36CBD7C40985A0B3C0055DEA'
$UvMinMinor = 5      # an older uv on PATH lacks flags used below: a private one is fetched instead
# Same as pulsar.BOOT: drop the current directory from sys.path and pin the app dir before any import,
# so a project folder that ships its own pulsar\ (or shadows the standard library) never runs as you.
$Boot = "import sys;r=sys.argv.pop(1);m=sys.argv.pop(1);sys.path[:]=[r]+[p for p in sys.path if p not in ('','.',r)];import runpy;sys.argv[0]=m;runpy.run_module(m,run_name='__main__',alter_sys=True)"
$PulsarHome = if ($env:PULSAR_HOME) { $env:PULSAR_HOME } else { Join-Path $HOME '.pulsarcode' }
$BinDir = if ($env:PULSAR_BIN_DIR) { $env:PULSAR_BIN_DIR } else { Join-Path $HOME '.local\bin' }
$App = Join-Path $PulsarHome 'app'
$Venv = Join-Path $PulsarHome 'venv'
$Py = Join-Path $Venv 'Scripts\python.exe'
$Marker = '.pulsarcode-home'
$Work = @{ Tmp = $null }                 # what the finally block below has to clean up
$SavedEnv = @{}                          # process environment to restore on the way out

# ------------------------------------------------------------------ look & feel
$Fancy = $false
try { $Fancy = (-not $env:NO_COLOR) -and ($Host.Name -eq 'ConsoleHost') -and (-not [Console]::IsOutputRedirected) } catch { }
# Check marks and bullets only where the font is known to have them (Windows Terminal, VS Code);
# the classic console gets ASCII.
$Glyphs = [bool]($env:WT_SESSION -or ($env:TERM_PROGRAM -eq 'vscode'))
if ($Glyphs) {
    $GOk = [string][char]0x2713; $GBad = [string][char]0x2717; $GMark = [string][char]0x25C9; $GDot = [string][char]0x00B7
} else {
    $GOk = '+'; $GBad = 'x'; $GMark = '*'; $GDot = '-'
}
$Esc = [char]27

function Paint($r, $g, $b, $text) {
    if ($Fancy) { return "$Esc[38;2;${r};${g};${b}m$text$Esc[0m" }
    return "$text"
}
function Ok($text) { Write-Host ('  ' + (Paint 80 250 170 $GOk) + ' ' + $text) }
function Step($text) { Write-Host ('  ' + (Paint 54 226 255 $GMark) + ' ' + $text) }
function Warn($text) { Write-Host ('  ' + (Paint 255 106 0 '!') + ' ' + $text) }
function Die($text) {
    Write-Host ''
    Write-Host ('  ' + (Paint 255 70 90 ($GBad + ' ' + $text)))
    throw 'pulsarcode-install-failed'
}

# ------------------------------------------------------------------ helpers
function Set-ScopedEnv([string]$name, $value) {
    # Changes the environment of this process (and so of the programs started below) only until
    # the installer returns.
    if (-not $SavedEnv.ContainsKey($name)) { $SavedEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
    Set-ProcessEnv $name $value
}

function Set-ProcessEnv([string]$name, $value) {
    # $null removes the variable. PowerShell hands $null to a .NET string parameter as '', and since
    # .NET 9 (PowerShell 7.5) '' sets an empty variable instead of removing it: pass a real null.
    if ($null -eq $value) { [Environment]::SetEnvironmentVariable($name, [NullString]::Value, 'Process') }
    else { [Environment]::SetEnvironmentVariable($name, [string]$value, 'Process') }
}

function Invoke-Quiet([scriptblock]$block) {
    # A native command's output as one string. 'Continue' here: under 'Stop', Windows PowerShell 5.1
    # turns every line a native program writes to a redirected stderr into a terminating error.
    $ErrorActionPreference = 'Continue'
    try { return ((& $block 2>&1 | ForEach-Object { "$_" }) -join "`n") } catch { return '' }
}

function Get-Download([string]$url, [string]$dest) {
    # HTTP status of the download: 200 when the file arrived, 0 when no server answered. Like install.sh
    # (curl --proto-redir =https), a download that a redirect took away from HTTPS is refused: the file
    # is deleted and 0 returned. Windows PowerShell 5.1 follows such a redirect; PowerShell 7 does not.
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $dest -PassThru
        $final = $null
        try { $final = $r.BaseResponse.ResponseUri } catch { }                                # 5.1
        if (-not $final) { try { $final = $r.BaseResponse.RequestMessage.RequestUri } catch { } }  # 7
        if ($final -and $final.Scheme -ne 'https') {
            Remove-Item -Force -LiteralPath $dest -ErrorAction SilentlyContinue
            Warn "download  refused: $url was redirected to a non-HTTPS address"
            return 0
        }
        return 200
    } catch {
        $code = 0
        try { $code = [int]$_.Exception.Response.StatusCode } catch { $code = 0 }
        if (Test-Path -LiteralPath $dest) { Remove-Item -Force -LiteralPath $dest -ErrorAction SilentlyContinue }
        return $code
    }
}

function Find-Sum([string]$file, [string]$name) {
    # The lowercase SHA-256 that SHA256SUMS lists for $name ("hash  name" or "hash *name"), or $null.
    foreach ($line in [IO.File]::ReadAllLines($file)) {
        $parts = $line.Trim() -split '\s+', 2
        if ($parts.Count -eq 2 -and $parts[1].TrimStart('*') -ceq $name -and $parts[0] -match '^[0-9A-Fa-f]{64}$') {
            return $parts[0].ToLowerInvariant()
        }
    }
    return $null
}

function Test-UvUsable([string]$path) {
    try { $v = Invoke-Quiet { & $path --version } } catch { return $false }
    if ("$v" -match '^uv (\d+)\.(\d+)') { return ([int]$Matches[1] -gt 0 -or [int]$Matches[2] -ge $UvMinMinor) }
    return $false
}

function Get-WindowsPowerShell {
    # Windows PowerShell by absolute path (never a powershell.exe found next to the user's files),
    # else the PowerShell running this script.
    $sys = [Environment]::SystemDirectory
    if ($sys) {
        $p = Join-Path $sys 'WindowsPowerShell\v1.0\powershell.exe'
        if (Test-Path -LiteralPath $p) { return $p }
    }
    return (Get-Process -Id $PID).Path
}

function Test-Ascii([string]$s) { return -not ($s -cmatch '[^\x20-\x7E]') }

function Read-Utf8([string]$path, [bool]$strict) {
    # The file's bytes as UTF-8, the way pulsar/config.py reads them: no BOM detection (a UTF-16 file is not
    # decoded as text). A byte that is not UTF-8 becomes U+FFFD, or with $strict makes the result $null (as
    # it does for a file that cannot be read).
    try { return (New-Object System.Text.UTF8Encoding($false, $strict)).GetString([IO.File]::ReadAllBytes($path)) }
    catch { return $null }
}

function Get-JsonObject([string]$path) {
    # pulsar/config.py _json_dict: the file holds one JSON object (UTF-8 without a BOM), else $null.
    # ConvertFrom-Json also takes comments, single quotes and trailing commas, which Python's json refuses:
    # where System.Text.Json exists (PowerShell 7) it must parse the text as strict JSON first.
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    $text = Read-Utf8 $path $true
    if (-not $text -or [int]$text[0] -eq 0xFEFF) { return $null }
    try {
        $strict = 'System.Text.Json.JsonDocument' -as [type]
        if ($strict) { $strict::Parse($text, (New-Object System.Text.Json.JsonDocumentOptions)).Dispose() }
        $obj = ConvertFrom-Json -InputObject $text -ErrorAction Stop
    } catch { return $null }
    # [System.Management.Automation.PSCustomObject], not [pscustomobject]: that one also matches a wrapped string
    if ($obj -is [System.Management.Automation.PSCustomObject]) { return $obj }
    return $null
}

function Get-RelativePath([string]$from, [string]$to) {
    # Relative path from folder $from to folder $to ('..\..\.pulsarcode'), or $null when they do
    # not share a drive (or are UNC paths). Windows PowerShell 5.1 has no [IO.Path]::GetRelativePath.
    if ($from.StartsWith('\\') -or $to.StartsWith('\\')) { return $null }
    $a = @([IO.Path]::GetFullPath($from).TrimEnd('\', '/') -split '[\\/]+')
    $b = @([IO.Path]::GetFullPath($to).TrimEnd('\', '/') -split '[\\/]+')
    if ($a.Count -eq 0 -or $b.Count -eq 0 -or -not ($a[0] -ieq $b[0])) { return $null }
    $i = 0
    while ($i -lt $a.Count -and $i -lt $b.Count -and ($a[$i] -ieq $b[$i])) { $i++ }
    $parts = @()
    for ($j = $i; $j -lt $a.Count; $j++) { $parts += '..' }
    for ($j = $i; $j -lt $b.Count; $j++) { $parts += $b[$j] }
    if ($parts.Count -eq 0) { return '.' }
    return ($parts -join '\')
}

function New-CmdShim([string]$binDir, [string]$pulsarHome, [string]$boot) {
    # pulsarcode.cmd for cmd.exe. cmd.exe reads a batch file in the console code page, so every line it
    # executes stays ASCII: the Pulsar home is found relative to the .cmd itself (%~dp0), which also
    # works for profile folders with accents, Cyrillic or CJK letters. Only a home on another drive
    # with a non-ASCII name is spelled out, read under code page 65001, and the caller's code page is
    # restored before Python starts. -E: a PYTHONPATH naming the current folder
    # cannot slip a sitecustomize.py in before the bootstrap; -X utf8 replaces PYTHONUTF8, which -E
    # ignores. Never `-m pulsar`, which would import from the current folder.
    $rel = Get-RelativePath $binDir $pulsarHome
    $full = [IO.Path]::GetFullPath($pulsarHome)
    # The home is also named in a comment, which is how `pulsarcode uninstall` recognises the file as
    # its own. A comment is never executed, so a name cmd.exe would misread there is harmless; the
    # closing quote keeps the line ending in ASCII.
    $utf8 = -not (Test-Ascii $full)
    $lines = @('@echo off', 'rem Pulsar Code launcher for cmd.exe (written by install.ps1)',
               ('rem Pulsar Code home: "' + ($full -replace '%', '%%') + '"'), 'setlocal')
    if ($rel -and (Test-Ascii $rel)) {
        $lines += ('for %%I in ("%~dp0' + ($rel -replace '%', '%%') + '") do set "PULSAR_APP_HOME=%%~fI"')
    } elseif (Test-Ascii $full) {
        $lines += ('set "PULSAR_APP_HOME=' + ($full -replace '%', '%%') + '"')
    } else {
        $lines += 'for /f "tokens=2 delims=:." %%c in (''chcp'') do set "PULSAR_CP=%%c"'
        $lines += 'chcp 65001 >nul'
        $lines += ('set "PULSAR_APP_HOME=' + ($full -replace '%', '%%') + '"')
        $lines += 'if defined PULSAR_CP chcp %PULSAR_CP% >nul'
    }
    # The engine runs against this launcher's home unless PULSAR_HOME names another one, so an install
    # into a custom PULSAR_HOME never uses the state of the default home (setlocal keeps it here).
    $lines += 'if not defined PULSAR_HOME set "PULSAR_HOME=%PULSAR_APP_HOME%"'
    # Python is the last line: nothing runs after it, and its exit code is the launcher's.
    $lines += ('"%PULSAR_APP_HOME%\venv\Scripts\python.exe" -E -X utf8 -B -c "' + $boot + '" "%PULSAR_APP_HOME%\app" pulsar %*')
    return @{ Text = (($lines -join "`r`n") + "`r`n"); Utf8 = $utf8 }
}

function New-Ps1Shim([string]$binDir, [string]$pulsarHome, [string]$boot) {
    # pulsarcode.ps1: what PowerShell runs when scripts are allowed. No batch file in between, so
    # Ctrl+C goes to Claude Code and never ends in a "Terminate batch job (Y/N)?" prompt.
    $q = { param($t) "'" + ($t -replace "'", "''") + "'" }
    $rel = Get-RelativePath $binDir $pulsarHome
    $full = [IO.Path]::GetFullPath($pulsarHome)
    if ($rel) { $where = '[IO.Path]::GetFullPath((Join-Path $PSScriptRoot ' + (& $q $rel) + '))' }
    else { $where = (& $q $full) }
    # The engine gets this home as PULSAR_HOME unless one is set. A script shares the caller's
    # environment, so the variable is removed again on the way out (Ctrl+C included).
    $lines = @(
        '# Pulsar Code launcher for PowerShell (written by install.ps1)',
        ('# Pulsar Code home: ' + $full),
        ('$PulsarAppHome = ' + $where),
        '$PulsarSetHome = -not $env:PULSAR_HOME',
        'if ($PulsarSetHome) { $env:PULSAR_HOME = $PulsarAppHome }',
        'try {',
        ('    & (Join-Path $PulsarAppHome ''venv\Scripts\python.exe'') -E -X utf8 -B -c ' + (& $q $boot) +
            ' (Join-Path $PulsarAppHome ''app'') pulsar @args'),
        '} finally {',
        '    if ($PulsarSetHome) { Remove-Item -LiteralPath Env:PULSAR_HOME -ErrorAction SilentlyContinue }',
        '}',
        'exit $LASTEXITCODE'
    )
    return (($lines -join "`r`n") + "`r`n")
}

function New-ShShim([string]$binDir, [string]$pulsarHome, [string]$boot) {
    # `pulsarcode` (no extension) for Git Bash, MSYS2 and Cygwin, which do not run .cmd files by bare
    # name. LF line endings, found relative to itself like the .cmd.
    $sq = { param($t) "'" + ($t -replace "'", "'\''") + "'" }
    $rel = Get-RelativePath $binDir $pulsarHome
    $full = [IO.Path]::GetFullPath($pulsarHome)
    if ($rel) {
        $find = 'ph=$(cd -- "$here"/' + (& $sq ($rel -replace '\\', '/')) + ' 2>/dev/null && pwd) || ph=""'
    } else {
        $find = 'ph=$(cygpath -u ' + (& $sq $full) + ' 2>/dev/null) || ph=' + (& $sq $full)
    }
    $lines = @(
        '#!/bin/sh',
        '# Pulsar Code launcher for Git Bash, MSYS2 and Cygwin (written by install.ps1)',
        ('# Pulsar Code home: ' + ($full -replace '[\r\n]', '')),
        'unset CDPATH',
        'here=$(cd -- "$(dirname -- "$0")" && pwd)',
        $find,
        'py="$ph/venv/Scripts/python.exe"',
        'if [ -z "$ph" ] || [ ! -f "$py" ]; then',
        '    echo "pulsarcode: Pulsar Code is not installed where this launcher expects it; re-run install.ps1" >&2',
        '    exit 127',
        'fi',
        'app="$ph/app"',
        'if command -v cygpath >/dev/null 2>&1; then app=$(cygpath -w "$app"); fi',
        '# The engine runs against this home unless PULSAR_HOME names another one.',
        'if [ -z "${PULSAR_HOME:-}" ]; then',
        '    PULSAR_HOME=$ph',
        '    if command -v cygpath >/dev/null 2>&1; then PULSAR_HOME=$(cygpath -w "$ph"); fi',
        '    export PULSAR_HOME',
        'fi',
        ('boot=' + (& $sq $boot)),
        '# mintty without ConPTY hands Windows programs a pipe instead of a console: winpty bridges that.',
        'if [ -t 0 ] && [ -t 1 ] && command -v winpty >/dev/null 2>&1 &&',
        '   ! "$py" -E -c "import sys; sys.exit(0 if sys.stdin.isatty() else 1)" 2>/dev/null; then',
        '    exec winpty "$py" -E -X utf8 -B -c "$boot" "$app" pulsar "$@"',
        'fi',
        'exec "$py" -E -X utf8 -B -c "$boot" "$app" pulsar "$@"'
    )
    return (($lines -join "`n") + "`n")
}

function Write-TextFile([string]$path, [string]$text, [string]$encoding) {
    switch ($encoding) {
        'ascii' { $enc = New-Object System.Text.ASCIIEncoding }
        'utf8bom' { $enc = New-Object System.Text.UTF8Encoding($true) }
        default { $enc = New-Object System.Text.UTF8Encoding($false) }
    }
    [IO.File]::WriteAllText($path, $text, $enc)
}

function Add-UserPath([string]$dir) {
    # The raw user PATH from the registry: [Environment]::GetEnvironmentVariable would expand
    # %USERPROFILE%-style entries and SetEnvironmentVariable would store them back frozen as REG_SZ.
    # Returns $true when the folder was added.
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
    if (-not $key) { $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment') }
    try {
        $raw = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $kind = [Microsoft.Win32.RegistryValueKind]::ExpandString
        if ($key.GetValueNames() -contains 'Path') {
            $kind = $key.GetValueKind('Path')
            if ($kind -ne [Microsoft.Win32.RegistryValueKind]::String) { $kind = [Microsoft.Win32.RegistryValueKind]::ExpandString }
        }
        $want = $dir.TrimEnd('\')
        foreach ($entry in ($raw -split ';')) {
            if (-not $entry) { continue }
            $expanded = [Environment]::ExpandEnvironmentVariables($entry).TrimEnd('\')
            if (($expanded -ieq $want) -or ($entry.TrimEnd('\') -ieq $want)) { return $false }
        }
        $new = if ($raw) { $dir + ';' + $raw } else { $dir }
        $key.SetValue('Path', $new, $kind)
    } finally {
        $key.Close()
    }
    # Tell Explorer (and so every terminal opened from now on) that the environment changed:
    # SetEnvironmentVariable broadcasts WM_SETTINGCHANGE.
    try {
        [Environment]::SetEnvironmentVariable('PULSARCODE_PATH_REFRESH', '1', 'User')
        [Environment]::SetEnvironmentVariable('PULSARCODE_PATH_REFRESH', [NullString]::Value, 'User')
    } catch { }
    return $true
}

function Test-OwnSource($dir) {
    foreach ($rel in @('pulsar\__init__.py', 'pulsarcode', 'requirements.txt')) {
        if (-not (Test-Path -LiteralPath (Join-Path $dir $rel) -PathType Leaf)) { return $false }
    }
    $full = (Resolve-Path -LiteralPath $dir).Path.TrimEnd('\')
    if ($full -eq ([IO.Path]::GetTempPath()).TrimEnd('\')) { return $false }   # the shared temp folder itself
    try {
        $me = [Security.Principal.WindowsIdentity]::GetCurrent()
        $owner = (Get-Acl -LiteralPath $dir).Owner
        $mine = @($me.Name, 'BUILTIN\Administrators', 'NT AUTHORITY\SYSTEM')
        if ($owner -and -not ($mine -contains $owner)) { return $false }
    } catch { }
    return $true
}

function Read-JsonLikeEngine([string]$path) {
    # A JSON file as the engine reads it (pulsar/config.py _read_json): UTF-8 bytes without a BOM, then
    # strict JSON. Get-Content drops a BOM and ConvertFrom-Json in PowerShell 7 accepts comments; the engine
    # refuses both. @{ Kind = 'Object', 'Array' ...; Props = name -> kind } for a file it reads, else $null.
    try {
        $bytes = [IO.File]::ReadAllBytes($path)
        if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) { return $null }
        $text = (New-Object Text.UTF8Encoding($false, $true)).GetString($bytes)
    } catch { return $null }
    $props = New-Object 'Collections.Generic.Dictionary[string,string]' ([StringComparer]::Ordinal)
    if (-not ('System.Text.Json.JsonDocument' -as [type])) {
        try { Add-Type -AssemblyName System.Text.Json -ErrorAction Stop } catch { }
    }
    if ('System.Text.Json.JsonDocument' -as [type]) {
        try { $doc = [System.Text.Json.JsonDocument]::Parse($text) } catch { return $null }
        try {
            $root = $doc.RootElement
            $kind = [string]$root.ValueKind
            if ($kind -eq 'Object') {
                foreach ($p in $root.EnumerateObject()) { $props[$p.Name] = [string]$p.Value.ValueKind }
            }
            return @{ Kind = $kind; Props = $props }
        } finally { $doc.Dispose() }
    }
    # Windows PowerShell 5.1 has no System.Text.Json: its own ConvertFrom-Json decides
    try { $j = $text | ConvertFrom-Json -ErrorAction Stop } catch { return $null }
    if (-not ($j -is [Management.Automation.PSCustomObject])) { return @{ Kind = 'Other'; Props = $props } }
    foreach ($p in $j.PSObject.Properties) {
        $props[$p.Name] = if ($p.Value -is [Management.Automation.PSCustomObject]) { 'Object' } else { 'Other' }
    }
    return @{ Kind = 'Object'; Props = $props }
}

function Test-JsonObject([string]$path) {
    # $true when the file holds a JSON object the engine reads, as keys.json and settings.json always do.
    $j = Read-JsonLikeEngine $path
    return ($null -ne $j -and $j.Kind -eq 'Object')
}

function Test-PulsarConfig([string]$path) {
    # Pulsar Code's config.json: a JSON object with a "profiles" object and an "active" key (the rule of
    # install.sh and of the engine). A file that merely mentions "profiles" is not one.
    $j = Read-JsonLikeEngine $path
    if ($null -eq $j -or $j.Kind -ne 'Object') { return $false }
    return ($j.Props.ContainsKey('active') -and $j.Props.ContainsKey('profiles') -and $j.Props['profiles'] -eq 'Object')
}

function Get-MarkerKind([string]$markPath) {
    # 'trusted', 'legacy' (a marker without provenance) or '' (none, or unreadable), by the engine's line rule
    # (pulsar/config.py marker_kind): the bytes read as UTF-8 without BOM detection, lines end at LF only (one
    # CR before it is dropped), and a line after the first whose first word is 'created' or 'adopted' is
    # provenance. Get-Content also ends a line at a lone CR and decodes a UTF-16 marker. The word is compared
    # ordinally: -ceq is culture-aware and ignores NUL, so a UTF-16 marker read as UTF-8 would pass it.
    try { $bytes = [IO.File]::ReadAllBytes($markPath) } catch { return '' }
    $lines = (New-Object Text.UTF8Encoding($false, $false)).GetString($bytes).Split([char]10)
    for ($i = 1; $i -lt $lines.Count; $i++) {
        $line = $lines[$i]
        if ($line.Length -gt 0 -and $line[$line.Length - 1] -eq [char]13) { $line = $line.Substring(0, $line.Length - 1) }
        $word = $line.Split([char]32)[0]
        if ([string]::Equals($word, 'created', [StringComparison]::Ordinal) -or
            [string]::Equals($word, 'adopted', [StringComparison]::Ordinal)) { return 'trusted' }
    }
    return 'legacy'
}

function Get-HomeKind([string]$dir) {
    # What an existing PULSAR_HOME is, by the rule of install.sh and of the engine (pulsar/config.py): Kind
    # 'ours' (a marker with provenance), 'created' (an empty folder, the only kind uninstall may remove
    # whole), 'adopted' (an earlier install with Pulsar Code's own evidence, next to nothing else),
    # 'unproven' (only names Pulsar Code uses but none of its evidence: settings.json, logs and cache are
    # just as likely another tool's) or 'foreign'. Foreign names what is in the way.
    $markPath = Join-Path $dir $Marker
    # A marker without provenance (written by a development build into any folder) proves nothing: only a
    # 'created' or 'adopted' line after the first skips the scan.
    if ((Get-MarkerKind $markPath) -eq 'trusted') { return @{ Kind = 'ours'; Foreign = @() } }
    $stateNames = @('run', 'cache', 'logs', 'plugin', 'claude_config', 'stage', 'canonical', 'claude-install.sh', $Marker,
                    'nim.key', 'nim.key.migrated', 'active_model', 'model_history', 'onboarding_complete',
                    'nim_sonar_catalog.json')
    $names = @()      # everything there, the engine's atomic-write temp files aside
    $foreign = @()
    foreach ($item in @(Get-ChildItem -LiteralPath $dir -Force)) {
        $n = $item.Name
        $p = $item.FullName
        if ($n -match '^\.(config|keys|settings)\.json\.') { continue }
        $names += $n
        if ($stateNames -contains $n) { continue }
        if ($n -eq 'config.json') {
            if (Test-PulsarConfig $p) { continue }
        } elseif (@('keys.json', 'settings.json') -contains $n) {
            if (Test-JsonObject $p) { continue }
        } elseif ($n -eq 'venv') {
            if (Test-Path -LiteralPath (Join-Path $p 'pyvenv.cfg')) { continue }      # an interrupted install
        } elseif (@('app', 'app.new', 'app.old') -contains $n) {
            if (Test-Path -LiteralPath (Join-Path $p 'pulsar\__init__.py')) { continue }
        } elseif ($n -eq 'bin') {
            $mine = @('uv.exe', 'uvx.exe', 'uvw.exe', 'uv-install.ps1')
            if ($item.PSIsContainer -and -not @(Get-ChildItem -LiteralPath $p -Force | Where-Object { $mine -notcontains $_.Name })) { continue }
        }
        $foreign += $n
    }
    if ($names.Count -eq 0) { return @{ Kind = 'created'; Foreign = @() } }
    # An earlier install: its engine, or, in a folder without any marker, a v1 key, the isolated Claude
    # Code profile or a Pulsar config. A marker without provenance proves nothing, so then only app\ counts.
    $evidence = Test-Path -LiteralPath (Join-Path $dir 'app\pulsar\__init__.py')
    if (-not $evidence -and -not (Test-Path -LiteralPath $markPath)) {
        $evidence = (Test-Path -LiteralPath (Join-Path $dir 'nim.key') -PathType Leaf) -or
                    (Test-Path -LiteralPath (Join-Path $dir 'claude_config') -PathType Container) -or
                    (Test-PulsarConfig (Join-Path $dir 'config.json'))
    }
    if ($evidence -and $foreign.Count -eq 0) { return @{ Kind = 'adopted'; Foreign = @() } }
    if ($foreign.Count -eq 0) { return @{ Kind = 'unproven'; Foreign = $names } }
    return @{ Kind = 'foreign'; Foreign = $foreign }
}

function Remove-Tree([string]$path) {
    # Deletes a folder of this installer's. A junction or symbolic link is removed as a link only:
    # Windows PowerShell 5.1's Remove-Item -Recurse would follow it and empty the folder it points to.
    $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    if (-not $item) { return }
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        if ($item.PSIsContainer) { [IO.Directory]::Delete($item.FullName, $false) } else { [IO.File]::Delete($item.FullName) }
        return
    }
    Remove-Item -Recurse -Force -LiteralPath $path
}

function Test-Claude {
    if (Get-Command claude -CommandType Application -ErrorAction SilentlyContinue) { return $true }
    return (Test-Path -LiteralPath (Join-Path $HOME '.local\bin\claude.exe'))
}

function Get-PersistentExecutionPolicy {
    # The policy a NEW PowerShell window will have. Get-ExecutionPolicy alone includes the Process
    # scope, which is Bypass whenever this installer was started with -ExecutionPolicy Bypass
    # (pulsarcode update, the Git Bash hand-off): that says nothing about the user's own windows.
    # Precedence: MachinePolicy, UserPolicy, CurrentUser, LocalMachine; the Process scope is skipped.
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { return 'Unrestricted' }
    $byScope = @{}
    try {
        foreach ($row in @(Get-ExecutionPolicy -List)) { $byScope["$($row.Scope)"] = "$($row.ExecutionPolicy)" }
    } catch { }
    foreach ($scope in @('MachinePolicy', 'UserPolicy', 'CurrentUser', 'LocalMachine')) {
        $v = $byScope[$scope]
        if ($v -and $v -ne 'Undefined') { return $v }
    }
    # Nothing set: Windows clients default to Restricted, Windows Server to RemoteSigned.
    try {
        $kind = (Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -Name InstallationType -ErrorAction Stop).InstallationType
        if ("$kind" -like 'Server*') { return 'RemoteSigned' }
    } catch { }
    return 'Restricted'
}

function Stop-RunningCore([string]$appFwd) {
    # A Core started from the old engine keeps its folder busy (Windows cannot replace a process's
    # current directory) and would keep serving old code to new launches. Ask it to stop first.
    $desc = Join-Path $PulsarHome 'run\core.json'
    if (-not (Test-Path -LiteralPath $desc)) { return }
    if (-not (Test-Path -LiteralPath (Join-Path $App 'pulsar\__init__.py')) -or -not (Test-Path -LiteralPath $Py)) { return }
    Set-ScopedEnv 'PULSAR_HOME' $PulsarHome
    $out = Invoke-Quiet { & $Py -E -X utf8 -B -c $Boot $appFwd pulsar stop }
    for ($i = 0; $i -lt 50 -and (Test-Path -LiteralPath $desc); $i++) { Start-Sleep -Milliseconds 100 }
    if (Test-Path -LiteralPath $desc) {
        Warn 'core      a Pulsar Core is still running with the old engine: it stops once its open sessions end (pulsarcode stop --force ends it now)'
    } elseif ("$out" -match 'shutting down') {
        Ok 'core      stopped the running Pulsar Core (restart open sessions to use the new engine)'
    }
}

# ------------------------------------------------------------------ install
$Failed = $false
$Launch = $null
$SavedProtocol = $null
try { $SavedProtocol = [Net.ServicePointManager]::SecurityProtocol } catch { }
try {
    if ($null -ne $SavedProtocol) {
        [Net.ServicePointManager]::SecurityProtocol = $SavedProtocol -bor [Net.SecurityProtocolType]::Tls12
    }
    # TLS-inspecting proxies: uv's bundled roots do not know the corporate CA, the Windows store does.
    if (-not $env:UV_NATIVE_TLS) { Set-ScopedEnv 'UV_NATIVE_TLS' '1' }
    # uv ignores pip's configuration; carry a corporate index or wheel folder over.
    if ($env:PIP_INDEX_URL -and -not ($env:UV_DEFAULT_INDEX -or $env:UV_INDEX_URL)) { Set-ScopedEnv 'UV_DEFAULT_INDEX' $env:PIP_INDEX_URL }
    if ($env:PIP_EXTRA_INDEX_URL -and -not ($env:UV_INDEX -or $env:UV_EXTRA_INDEX_URL)) { Set-ScopedEnv 'UV_INDEX' $env:PIP_EXTRA_INDEX_URL }
    if ($env:PIP_FIND_LINKS -and -not $env:UV_FIND_LINKS) { Set-ScopedEnv 'UV_FIND_LINKS' $env:PIP_FIND_LINKS }

    Write-Host ''
    $stops = @(@(255, 106, 0), @(255, 46, 136), @(157, 77, 255), @(54, 226, 255))
    $word = 'P U L S A R   C O D E'
    $line = ''
    for ($i = 0; $i -lt $word.Length; $i++) {
        $t = $i / [Math]::Max(1, $word.Length - 1) * 3
        $s = [Math]::Min(2, [Math]::Floor($t)); $f = $t - $s
        $a = $stops[$s]; $c = $stops[$s + 1]
        $line += Paint ([int]($a[0] + ($c[0] - $a[0]) * $f)) ([int]($a[1] + ($c[1] - $a[1]) * $f)) ([int]($a[2] + ($c[2] - $a[2]) * $f)) $word[$i]
    }
    Write-Host ('  ' + $line)
    Write-Host ('  ' + (Paint 150 146 172 ('any intelligence  ' + $GDot + '  one terminal')))
    Write-Host ''

    if (-not ($Ref -cmatch '^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}\z') -or $Ref.Contains('..') -or $Ref.Contains('//') -or
        $Ref.EndsWith('/') -or $Ref.EndsWith('.') -or $Ref.EndsWith('.lock')) {
        Die 'PULSAR_REF must be a tag, branch or commit (letters, digits, . _ / - only)'
    }
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        Die 'install.ps1 installs on Windows. On macOS and Linux run: curl -fsSL https://raw.githubusercontent.com/PulsarOSDevTeam/pulsarcode/main/install.sh | bash'
    }

    # A relative PULSAR_HOME or PULSAR_BIN_DIR names a folder below PowerShell's current location, the
    # way Test-Path reads it; the .NET calls below would resolve it against the process folder instead.
    $PulsarHome = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($PulsarHome)
    $BinDir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($BinDir)
    $App = Join-Path $PulsarHome 'app'
    $Venv = Join-Path $PulsarHome 'venv'
    $Py = Join-Path $Venv 'Scripts\python.exe'

    # --- home -------------------------------------------------------------------
    # Never move into a folder that holds someone else's files: a mistaken PULSAR_HOME (a project, your
    # profile folder) would get Pulsar's files, and `pulsarcode uninstall` would later empty it.
    $markKind = ''
    if (Test-Path -LiteralPath $PulsarHome) {
        if (-not (Test-Path -LiteralPath $PulsarHome -PathType Container)) { Die "PULSAR_HOME=$PulsarHome is not a folder" }
        $realHome = (Resolve-Path -LiteralPath $PulsarHome).Path.TrimEnd('\')
        if ($realHome -eq (Resolve-Path -LiteralPath $HOME).Path.TrimEnd('\') -or $realHome -match '^[A-Za-z]:$') {
            Die "PULSAR_HOME=$PulsarHome is your home folder or a drive root"
        }
        $found = Get-HomeKind $realHome
        if (@('foreign', 'unproven') -contains $found.Kind) {
            $shown = (@($found.Foreign) | Select-Object -First 5 | ForEach-Object { $_ -replace '[^\x20-\x7E]', '?' }) -join ', '
            if ($shown) { $shown = " ($shown)" }
            $why = if ($found.Kind -eq 'unproven') { 'cannot tell are its own' } else { 'did not create' }
            Die "PULSAR_HOME=$PulsarHome already holds files Pulsar Code $why$shown. Nothing was written there. Point PULSAR_HOME at a new or empty folder."
        }
        if ($found.Kind -ne 'ours') { $markKind = $found.Kind }
    } else {
        $markKind = 'created'
    }
    [void][IO.Directory]::CreateDirectory($PulsarHome)
    [void][IO.Directory]::CreateDirectory($BinDir)
    if ($markKind) {
        $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        Write-TextFile (Join-Path $PulsarHome $Marker) ("Pulsar Code home. ``pulsarcode uninstall`` only removes folders that carry this file.`n" +
                                                        "$markKind $stamp`n") 'ascii'
    }

    # --- uv -----------------------------------------------------------------------
    $uv = $null
    $onPath = (Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1)
    foreach ($c in @($(if ($onPath) { $onPath.Source }), (Join-Path $PulsarHome 'bin\uv.exe'))) {
        if ($c -and (Test-Path -LiteralPath $c) -and (Test-UvUsable $c)) { $uv = $c; break }
    }
    if (-not $uv) {
        $uvDir = Join-Path $PulsarHome 'bin'
        Step "fetching uv $UvVersion (private copy)"
        [void][IO.Directory]::CreateDirectory($uvDir)
        $uvInst = Join-Path $uvDir 'uv-install.ps1'
        $code = Get-Download "https://astral.sh/uv/$UvVersion/install.ps1" $uvInst
        if ($code -ne 200) { Die "could not download the uv installer (HTTP $code)" }
        $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $uvInst).Hash
        if ($got -ne $UvInstallerSha256) { Remove-Item -Force -LiteralPath $uvInst; Die "uv installer checksum mismatch ($got)" }
        Set-ScopedEnv 'UV_INSTALL_DIR' $uvDir
        Set-ScopedEnv 'UV_UNMANAGED_INSTALL' $uvDir
        Set-ScopedEnv 'INSTALLER_NO_MODIFY_PATH' '1'
        & (Get-WindowsPowerShell) -NoProfile -ExecutionPolicy Bypass -File $uvInst
        Remove-Item -Force -LiteralPath $uvInst -ErrorAction SilentlyContinue
        $uv = Join-Path $uvDir 'uv.exe'
        if (-not (Test-Path -LiteralPath $uv) -or -not (Test-UvUsable $uv)) { Die 'uv install failed' }
    }
    Ok ('uv        ' + (Invoke-Quiet { & $uv --version }))

    # --- source -------------------------------------------------------------------
    $wantLocal = ($args -contains '-Local') -or ($args -contains '--local')
    $Src = ''
    if ($env:PULSAR_NO_LOCAL_SRC -ne '1') {
        if ($env:PULSAR_SRC) { $Src = $env:PULSAR_SRC }
        elseif ($wantLocal) {
            if (-not $ScriptDir) { Die '-Local needs the script as a file: & C:\path\to\pulsarcode\install.ps1 -Local' }
            $Src = $ScriptDir
        }
    }
    if ($Src) {
        if (-not (Test-Path -LiteralPath $Src -PathType Container) -or -not (Test-OwnSource $Src)) {
            Die "refusing to install from ${Src}: it must hold pulsar\, pulsarcode and requirements.txt and be owned by you"
        }
        $Src = (Resolve-Path -LiteralPath $Src).Path
        Ok 'source    local checkout'
    } else {
        if ($env:PULSAR_NO_LOCAL_SRC -ne '1' -and $ScriptDir -and (Test-Path -LiteralPath (Join-Path $ScriptDir 'pulsar\__init__.py'))) {
            Step "this script sits in a Pulsar Code checkout; installing $Ref from GitHub (add -Local to install the checkout)"
        }
        $Work.Tmp = Join-Path ([IO.Path]::GetTempPath()) ('pulsarcode-' + [Guid]::NewGuid().ToString('N'))
        [void][IO.Directory]::CreateDirectory($Work.Tmp)
        $zip = Join-Path $Work.Tmp 'src.zip'
        $verified = $false
        if ($Ref -cmatch '^v\d') {
            $base = "https://github.com/$Repo/releases/download/$Ref"
            $sums = Join-Path $Work.Tmp 'SHA256SUMS'
            $code = Get-Download "$base/SHA256SUMS" $sums
            if ($code -eq 200) {
                $name = 'pulsarcode-app-' + $Ref.Substring(1) + '.zip'
                $want = Find-Sum $sums $name
                if (-not $want) { Die "SHA256SUMS of $Ref does not list ${name}: refusing to install it unverified" }
                Step "downloading Pulsar Code $Ref (release archive)"
                $code = Get-Download "$base/$name" $zip
                if ($code -ne 200) { Die "could not download $name from the $Ref release (HTTP $code)" }
                $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLowerInvariant()
                if ($got -ne $want) {
                    Remove-Item -Force -LiteralPath $zip
                    Die "checksum mismatch for ${name}: SHA256SUMS says $want, the download is $got. Nothing was installed."
                }
                Ok "verify    $name matches the release SHA256SUMS"
                $verified = $true
            } elseif ($code -eq 404 -and $Ref -cmatch '^v(\d{1,9})(\.|$)' -and [int]$Matches[1] -lt 3) {
                # Only a release older than v3 publishes no SHA256SUMS.
                Warn "verify    release $Ref publishes no SHA256SUMS: installing its tag archive (HTTPS only, not checksum-verified)"
            } elseif ($code -eq 404) {
                Die "release $Ref publishes no SHA256SUMS. Every release from v3 on does, and is only installed after its checksum verifies: check the tag, or wait for the release to finish publishing and re-run."
            } else {
                Die "could not download SHA256SUMS for $Ref (HTTP $code). A release tag is only installed after its checksum verifies: check your connection or proxy and re-run."
            }
        }
        if (-not $verified) {
            Step "downloading Pulsar Code ($Ref)"
            if ($Ref -cmatch '^v\d') { $urls = @("https://github.com/$Repo/archive/refs/tags/$Ref.zip") }
            elseif ($Ref -cmatch '^[0-9a-f]{40}$') { $urls = @("https://github.com/$Repo/archive/$Ref.zip") }
            else { $urls = @("https://github.com/$Repo/archive/refs/heads/$Ref.zip", "https://github.com/$Repo/archive/refs/tags/$Ref.zip") }
            $got = $false
            foreach ($u in $urls) {
                if ((Get-Download $u $zip) -eq 200) { $got = $true; break }
            }
            if (-not $got) { Die "could not download Pulsar Code $Ref from github.com/$Repo" }
        }
        Expand-Archive -LiteralPath $zip -DestinationPath $Work.Tmp -Force
        $Src = (Get-ChildItem -LiteralPath $Work.Tmp -Directory | Select-Object -First 1).FullName
        if (-not $Src -or -not (Test-Path -LiteralPath (Join-Path $Src 'pulsar\__init__.py'))) { Die 'download does not contain the Pulsar Code engine' }
    }

    # --- stop a running Core -----------------------------------------------------
    # Before the runtime is touched: a running Core is venv\Scripts\python.exe with the .pyd files
    # of its dependencies loaded, and Windows refuses to delete or overwrite a loaded image, so
    # rebuilding the venv or upgrading a pinned dependency would fail with "Access is denied".
    $AppFwd = $App -replace '\\', '/'
    Stop-RunningCore $AppFwd

    # --- python + deps ------------------------------------------------------------
    $reuse = $false
    if (Test-Path -LiteralPath $Py) {
        [void](Invoke-Quiet { & $Py -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' })
        $reuse = ($LASTEXITCODE -eq 0)
    }
    if (-not $reuse) {
        if (Test-Path -LiteralPath $Venv) {
            # Only a virtualenv (pyvenv.cfg) or an empty folder is ever rebuilt: a venv folder of anyone
            # else's stops the install instead.
            if (-not (Test-Path -LiteralPath (Join-Path $Venv 'pyvenv.cfg')) -and @(Get-ChildItem -LiteralPath $Venv -Force).Count -gt 0) {
                Die "$Venv is not a Python environment this installer made (no pyvenv.cfg): move it out of the way and re-run"
            }
            Remove-Tree $Venv
        }
        Step 'creating private Python runtime'
        & $uv venv --quiet --allow-existing --python '>=3.9' $Venv
        if ($LASTEXITCODE -ne 0) { & $uv venv --quiet --allow-existing --python 3.12 --python-preference only-managed $Venv }
        if (-not (Test-Path -LiteralPath $Py)) { Die 'uv could not create the Python runtime' }
    }
    & $uv pip install --quiet --python $Py -r (Join-Path $Src 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { Die 'dependency install failed' }
    Ok ('runtime   ' + (Invoke-Quiet { & $Py -V }))

    # --- engine -------------------------------------------------------------------
    # The engine folder is only ever replaced by a newer engine, never when it holds anything else.
    if ((Test-Path -LiteralPath $App) -and -not (Test-Path -LiteralPath (Join-Path $App 'pulsar\__init__.py'))) {
        Die "$App is not a Pulsar Code engine: move it out of the way and re-run"
    }
    $stage = "$App.new"
    $old = "$App.old"
    Remove-Tree $stage
    [void][IO.Directory]::CreateDirectory($stage)
    Copy-Item -Recurse -LiteralPath (Join-Path $Src 'pulsar') -Destination (Join-Path $stage 'pulsar')
    foreach ($f in @('requirements.txt', 'LICENSE')) {
        $p = Join-Path $Src $f
        if (Test-Path -LiteralPath $p) { Copy-Item -LiteralPath $p -Destination $stage }
    }
    Get-ChildItem -LiteralPath $stage -Recurse -Directory -Force | Where-Object { $_.Name -eq '__pycache__' } |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Stop-RunningCore $AppFwd                     # no-op unless something started a Core meanwhile
    if (Test-Path -LiteralPath $old) { try { Remove-Tree $old } catch { } }
    if (Test-Path -LiteralPath $App) {
        # Rename is all or nothing: the old engine stays intact until the new one is in place.
        $renamed = $false
        for ($i = 0; $i -lt 10 -and -not $renamed; $i++) {
            try { Rename-Item -LiteralPath $App -NewName 'app.old'; $renamed = $true } catch { Start-Sleep -Milliseconds 500 }
        }
        if (-not $renamed) { Die "could not replace ${App}: it is in use. Close running Pulsar Code sessions (pulsarcode stop) and re-run." }
    }
    Move-Item -LiteralPath $stage -Destination $App
    if (Test-Path -LiteralPath $old) { try { Remove-Tree $old } catch { } }
    Ok ('engine    ' + (Invoke-Quiet { & $Py -E -X utf8 -B -c $Boot $AppFwd pulsar version }))

    # --- claude code --------------------------------------------------------------
    if ($env:PULSAR_SKIP_CLAUDE -eq '1') {
        Warn 'claude    skipped (PULSAR_SKIP_CLAUDE=1)'
    } elseif (Test-Claude) {
        Ok 'claude    ready'
    } else {
        Step 'installing Claude Code (official installer, in its own PowerShell)'
        if (-not $Work.Tmp) {
            $Work.Tmp = Join-Path ([IO.Path]::GetTempPath()) ('pulsarcode-' + [Guid]::NewGuid().ToString('N'))
            [void][IO.Directory]::CreateDirectory($Work.Tmp)
        }
        $ci = Join-Path $Work.Tmp 'claude-install.ps1'
        if ((Get-Download 'https://claude.ai/install.ps1' $ci) -eq 200) {
            & (Get-WindowsPowerShell) -NoProfile -ExecutionPolicy Bypass -File $ci
        }
        if (-not (Test-Claude) -and (Get-Command npm -ErrorAction SilentlyContinue)) {
            & npm install -g '@anthropic-ai/claude-code'
        }
        if (-not (Test-Claude)) { Die 'Claude Code could not be installed automatically. Install it (https://claude.com/claude-code) and re-run.' }
        Ok 'claude    ready'
    }

    # --- command shims ------------------------------------------------------------
    $cmd = Join-Path $BinDir 'pulsarcode.cmd'
    $cmdShim = New-CmdShim $BinDir $PulsarHome $Boot
    if ($cmdShim.Utf8) { Write-TextFile $cmd $cmdShim.Text 'utf8' } else { Write-TextFile $cmd $cmdShim.Text 'ascii' }
    Write-TextFile (Join-Path $BinDir 'pulsarcode') (New-ShShim $BinDir $PulsarHome $Boot) 'utf8'
    # pulsarcode.ps1 only when the user's persistent policy lets a local script run; otherwise
    # PowerShell would pick the .ps1 over the .cmd and refuse it. UTF-8 with a BOM, which both
    # PowerShell editions read correctly whatever the path holds.
    $ps1 = Join-Path $BinDir 'pulsarcode.ps1'
    $policy = Get-PersistentExecutionPolicy
    if (@('RemoteSigned', 'Unrestricted', 'Bypass') -contains $policy) {
        Write-TextFile $ps1 (New-Ps1Shim $BinDir $PulsarHome $Boot) 'utf8bom'
    } elseif ((Test-Path -LiteralPath $ps1 -PathType Leaf) -and
              ((Get-Content -LiteralPath $ps1 -TotalCount 1 -ErrorAction SilentlyContinue) -like '# Pulsar Code launcher for PowerShell*')) {
        Remove-Item -Force -LiteralPath $ps1      # only a launcher this installer wrote, never anyone else's script
    }
    if ($env:PULSAR_NO_PATH -ne '1') {
        if (Add-UserPath $BinDir) { Ok "PATH      added $BinDir (new terminals)" }
        if (-not (($env:Path -split ';') | Where-Object { $_.TrimEnd('\') -ieq $BinDir.TrimEnd('\') })) {
            $env:Path = $BinDir + ';' + $env:Path      # this window too: the one change kept on purpose
        }
    }
    Ok 'command   pulsarcode   (PowerShell, cmd.exe and Git Bash)'

    Write-Host ''
    Write-Host ('  ' + (Paint 54 226 255 ($GMark + ' Pulsar Code is installed.')))
    $interactive = $true
    try { $interactive = -not [Console]::IsInputRedirected } catch { }
    if ($env:PULSAR_NO_LAUNCH -ne '1' -and $interactive) {
        Write-Host ('  ' + (Paint 150 146 172 'Launching. Paste any API key when asked.'))
        if (Test-Path -LiteralPath $ps1) { $Launch = $ps1 } else { $Launch = $cmd }
    } else {
        Write-Host '  Run pulsarcode in any project folder.'
    }
} catch {
    $Failed = $true
    if ("$_" -ne 'pulsarcode-install-failed') {
        Write-Host ''
        Write-Host ('  ' + (Paint 255 70 90 ($GBad + ' ' + $_.Exception.Message)))
        if ($_.InvocationInfo -and $_.InvocationInfo.ScriptLineNumber) {
            Write-Host ('    (install.ps1 line ' + $_.InvocationInfo.ScriptLineNumber + ')')
        }
    }
} finally {
    foreach ($k in @($SavedEnv.Keys)) { Set-ProcessEnv $k $SavedEnv[$k] }
    if ($null -ne $SavedProtocol) { try { [Net.ServicePointManager]::SecurityProtocol = $SavedProtocol } catch { } }
    if ($Work.Tmp -and (Test-Path -LiteralPath $Work.Tmp)) { Remove-Item -Recurse -Force -LiteralPath $Work.Tmp -ErrorAction SilentlyContinue }
}
if ($Failed) {
    Write-Host ''
    Write-Host '  Pulsar Code was not installed. Fix the problem above and run the installer again.'
    if ($AsFile) { exit 1 }            # ends this script (or the powershell -File process), never your window
    $global:LASTEXITCODE = 1
} else {
    if ($Launch) { & $Launch }         # with the environment already restored
    if ($AsFile) { exit 0 }            # not whatever the last native command happened to return
}
} @args
