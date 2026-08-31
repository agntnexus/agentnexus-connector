<#
.SYNOPSIS
AgentNexus Connector — bootstrap loader.

.DESCRIPTION
Fetches the signed connector release manifest, verifies it against the public key embedded below,
installs the exact artifact that manifest pins, and hands over to the connector's own setup.

This file is deliberately small enough to read before running it. That matters: the convenience
form pipes it straight into PowerShell, so "inspectable" has to mean somebody can actually inspect
it in a minute. Everything it installs is verified; nothing it installs is decided here.

  Inspect first (recommended):
    irm https://agntnexus.com/connect.ps1 -OutFile connect.ps1 ; notepad connect.ps1 ; .\connect.ps1

  Convenience form:
    & ([scriptblock]::Create((irm 'https://agntnexus.com/connect.ps1')))

.NOTES
The trust chain, in order:

  1. HTTPS to the configured origin authenticates the host.
  2. This loader carries the release public key. It is not fetched, so a compromised origin cannot
     replace it without replacing this file, which the applicant can read.
  3. The manifest is verified with that key, over its exact downloaded bytes, before it is parsed.
  4. Artifact URLs must stay on the configured origin, so a valid signature still cannot redirect
     the install elsewhere.
  5. Every artifact byte is checked against the size and SHA-256 the manifest pins, before install.

ECDSA P-256 rather than the Ed25519 the agent protocol uses: Windows PowerShell 5.1 runs on .NET
Framework, which has no Ed25519. Verifying one would mean downloading crypto code and trusting it
before any verification had happened. P-256 is verified here by the platform itself.

The invitation is never passed to this script, and there is deliberately no parameter for one.
The connector prompts for it after installation, with no echo, so it cannot reach a command
line, a process listing, or PowerShell history. `-Runtime` is not a secret and may appear in a
command an operator hands over.
#>
[CmdletBinding()]
param(
    # The AgentNexus origin. Overridable so this exact file can be tested against a local fixture
    # release; the signature check does not weaken when it is, because the key below does not move.
    [ValidatePattern('^https?://[A-Za-z0-9.-]+(:\d{1,5})?$')]
    [string]$Origin = 'https://agntnexus.com',

    # Install root. One directory, owned by AgentNexus, never a shared or system location.
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'AgentNexus'),

    # Print what would happen and stop before installing anything.
    [switch]$WhatIfOnly,

    # Verify and install, but do not run the connector's interactive setup.
    [switch]$SkipSetup,

    # Which agent runtime to configure. Not a secret, so it belongs in the command an applicant is
    # given; ValidateSet refuses anything else before a single byte is downloaded. Omitted, the
    # connector asks, or uses the one runtime it finds.
    [ValidateSet('hermes', 'openclaw', 'both')]
    [string]$Runtime
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------------------------
# The release public key. Replaced by a real one at release time; this is the local development
# key so the loader can be exercised end to end without publishing anything.
# ---------------------------------------------------------------------------------------------
$ReleasePublicKeyX = 'd13dd7292bf0453357fcee6064e227b889d09a912b6387b274bcb03f07c3bbbb'
$ReleasePublicKeyY = '27033143957d30bd17a715ef95c422ebbc5c3094d4ddbef4ae78500179050ab8'

$ManifestPath = '/connector/connector-release.json'
$MaxManifestBytes = 65536
$MaxArtifactBytes = 64MB

function Write-Step([string]$Message) { Write-Host "  $Message" }

function Convert-FromHex([string]$Hex) {
    if ($Hex.Length % 2 -ne 0) { throw 'A hex value must have an even number of characters.' }
    $bytes = New-Object byte[] ($Hex.Length / 2)
    for ($i = 0; $i -lt $bytes.Length; $i++) {
        $bytes[$i] = [Convert]::ToByte($Hex.Substring($i * 2, 2), 16)
    }
    return , $bytes
}

function Get-RemoteBytes([string]$Url, [int]$MaximumBytes) {
    # -UseBasicParsing keeps this working on a machine with no Internet Explorer engine, which is
    # every current Windows. The length check is before anything is written to disk.
    $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -MaximumRedirection 0
    $bytes = $response.Content
    if ($bytes -is [string]) { $bytes = [Text.Encoding]::UTF8.GetBytes($bytes) }
    if ($bytes.Length -gt $MaximumBytes) {
        throw "$Url returned $($bytes.Length) bytes, over the $MaximumBytes limit. Refusing."
    }
    return , $bytes
}

function Test-ReleaseSignature([byte[]]$Payload, [byte[]]$Signature) {
    if ($Signature.Length -ne 64) {
        throw "The release signature is $($Signature.Length) bytes; expected 64 raw r||s bytes."
    }
    Add-Type -AssemblyName System.Core
    $parameters = New-Object System.Security.Cryptography.ECParameters
    $parameters.Curve = [System.Security.Cryptography.ECCurve]::CreateFromFriendlyName('nistP256')
    $point = New-Object System.Security.Cryptography.ECPoint
    $point.X = Convert-FromHex $ReleasePublicKeyX
    $point.Y = Convert-FromHex $ReleasePublicKeyY
    $parameters.Q = $point
    $ecdsa = [System.Security.Cryptography.ECDsa]::Create()
    $ecdsa.ImportParameters($parameters)
    return $ecdsa.VerifyData($Payload, $Signature, [System.Security.Cryptography.HashAlgorithmName]::SHA256)
}

function Get-Sha256Hex([byte[]]$Bytes) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return (($sha.ComputeHash($Bytes) | ForEach-Object { $_.ToString('x2') }) -join '') }
    finally { $sha.Dispose() }
}

function Test-CompatiblePython([string]$Executable, [string[]]$PrefixArguments = @()) {
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $false }
    # Avoid quotes inside `-c`: Windows PowerShell 5.1's native argument marshalling strips them.
    $probe = 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3,13) and sys.maxsize > 2**32 else 1)'
    try {
        & $Executable @PrefixArguments -c $probe 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    }
    catch { return $false }
}

function Find-CompatiblePython {
    # Prefer the Python launcher with an explicit minor version. `py -3` means "the newest Python
    # 3 currently installed" and created a 3.12 environment on a real applicant machine even
    # though the connector requires >=3.13,<3.14.
    $launcher = Get-Command 'py.exe' -ErrorAction SilentlyContinue
    if ($null -ne $launcher -and (Test-CompatiblePython $launcher.Source @('-3.13'))) {
        return [PSCustomObject]@{ Executable = $launcher.Source; Arguments = @('-3.13') }
    }

    # Winget installs per-user Python here. Check the canonical location directly because PATH in
    # the PowerShell process that launched winget is not refreshed automatically.
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'),
        (Join-Path $env:ProgramFiles 'Python313\python.exe'),
        'C:\Python313\python.exe'
    )
    $pythonCommand = Get-Command 'python.exe' -ErrorAction SilentlyContinue
    if ($null -ne $pythonCommand -and $pythonCommand.Source -notmatch '\\Microsoft\\WindowsApps\\python(3)?\.exe$') {
        $candidates += $pythonCommand.Source
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-CompatiblePython $candidate) {
            return [PSCustomObject]@{ Executable = $candidate; Arguments = @() }
        }
    }
    return $null
}

function Install-CompatiblePython {
    $winget = Get-Command 'winget.exe' -ErrorAction SilentlyContinue
    if ($null -eq $winget) {
        throw 'Python 3.13 x64 is required and Winget is unavailable. Install Python 3.13 from https://www.python.org/downloads/ and re-run this command.'
    }

    Write-Step 'Python 3.13 x64 was not found; installing it for the current user with Winget'
    & $winget.Source install `
        --exact `
        --id Python.Python.3.13 `
        --source winget `
        --scope user `
        --silent `
        --disable-interactivity `
        --accept-package-agreements `
        --accept-source-agreements | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) {
        throw "Winget could not install Python 3.13 (exit $LASTEXITCODE). Nothing in Hermes was changed."
    }

    # Refresh PATH only in this process. The parent shell is deliberately not modified.
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machinePath, $userPath) | Where-Object { $_ }) -join ';'

    $installed = Find-CompatiblePython
    if ($null -eq $installed) {
        throw 'Winget reported success, but Python 3.13 x64 could not be found. Open a new PowerShell window and re-run this command.'
    }
    return $installed
}

function Remove-IncompatibleOwnedVenv([string]$VenvPath, [string]$VersionPath) {
    $fullVenv = [IO.Path]::GetFullPath($VenvPath)
    $fullVersion = [IO.Path]::GetFullPath($VersionPath).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    )
    $expectedPrefix = $fullVersion + [IO.Path]::DirectorySeparatorChar
    if (
        -not $fullVenv.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($fullVenv) -ne 'venv'
    ) {
        throw "Refusing to remove a virtual environment outside $fullVersion."
    }
    $item = Get-Item -LiteralPath $fullVenv -Force
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Refusing to replace $fullVenv because it is not a normal directory."
    }
    Remove-Item -LiteralPath $fullVenv -Recurse -Force
}

# ---------------------------------------------------------------------------------------------
# 1. Preflight. Fail with one exact prerequisite rather than a partial install.
# ---------------------------------------------------------------------------------------------
Write-Host 'AgentNexus Connector'
Write-Step 'Checking prerequisites'

if ($PSVersionTable.PSVersion.Major -lt 5) {
    throw "Windows PowerShell 5.1 or later is required; this is $($PSVersionTable.PSVersion)."
}
if ([System.Environment]::Is64BitOperatingSystem -ne $true) {
    throw 'A 64-bit Windows installation is required.'
}

if ($null -eq (Get-Command 'hermes' -ErrorAction SilentlyContinue)) {
    # Not fatal here: the connector reports it precisely, with the one command to fix it, after it
    # has done the work that does not depend on Hermes. Failing now would waste a verified install.
    Write-Step 'Hermes was not found on PATH; the connector will tell you exactly what to install.'
}

# ---------------------------------------------------------------------------------------------
# 2. Fetch and verify the release manifest.
# ---------------------------------------------------------------------------------------------
$origin = $Origin.TrimEnd('/')
Write-Step "Fetching the release manifest from $origin"

$manifestBytes = Get-RemoteBytes "$origin$ManifestPath" $MaxManifestBytes
$signatureText = [Text.Encoding]::ASCII.GetString((Get-RemoteBytes "$origin$ManifestPath.sig" 256)).Trim()
$signatureBytes = Convert-FromHex $signatureText

if (-not (Test-ReleaseSignature $manifestBytes $signatureBytes)) {
    throw 'The release manifest signature does not verify against this loader''s embedded key. Refusing to install.'
}
Write-Step 'Manifest signature verified'

$manifest = [Text.Encoding]::UTF8.GetString($manifestBytes) | ConvertFrom-Json
if ($manifest.schema_version -ne 1) {
    throw "Unsupported manifest schema $($manifest.schema_version). Update this loader."
}
if ($manifest.connector_version -notmatch '^\d+\.\d+\.\d+(?:[.-][A-Za-z0-9.-]{1,32})?$') {
    throw "The connector version $($manifest.connector_version) is not a safe version directory. Refusing."
}

# The signature says who wrote the manifest, not where it may send the install. Pick the artifact
# and re-check its origin here, so a valid signature over a redirecting manifest still fails.
$artifact = $manifest.artifacts | Where-Object { $_.platform -eq 'windows-x64' } | Select-Object -First 1
if ($null -eq $artifact) {
    $artifact = $manifest.artifacts | Where-Object { $_.platform -eq 'any' } | Select-Object -First 1
}
if ($null -eq $artifact) { throw 'The manifest publishes no artifact for this platform.' }
if (-not $artifact.url.StartsWith("$origin/")) {
    throw "The manifest points at $($artifact.url), which is not on $origin. Refusing."
}
if ($artifact.url.Contains('..')) { throw 'The artifact url contains a traversal segment. Refusing.' }
if ($artifact.size -le 0 -or $artifact.size -gt $MaxArtifactBytes) {
    throw "The artifact size $($artifact.size) is outside the accepted bounds. Refusing."
}
if ($artifact.filename -notmatch '^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$') {
    throw "The artifact filename $($artifact.filename) is not a plain file name. Refusing."
}

Write-Step "Release $($manifest.connector_version), artifact $($artifact.filename)"

if ($WhatIfOnly) {
    Write-Host ''
    Write-Host "Would install $($artifact.filename) ($($artifact.size) bytes) into $InstallRoot."
    Write-Host 'Nothing was downloaded or changed.'
    return
}

# ---------------------------------------------------------------------------------------------
# 3. Download and verify the artifact before it is installed.
# ---------------------------------------------------------------------------------------------
Write-Step 'Downloading the connector'
$artifactBytes = Get-RemoteBytes $artifact.url $MaxArtifactBytes
if ($artifactBytes.Length -ne $artifact.size) {
    throw "The download is $($artifactBytes.Length) bytes; the manifest pins $($artifact.size). Refusing."
}
$actualDigest = Get-Sha256Hex $artifactBytes
if ($actualDigest -ne $artifact.sha256) {
    throw "The download digest $actualDigest does not match the pinned $($artifact.sha256). Refusing."
}
Write-Step 'Artifact digest verified'

# Python is installed only after the signed manifest and the artifact digest have both verified.
# This keeps a broken or tampered release from causing a prerequisite installation as a side
# effect. The invitation is not requested until after every step below succeeds.
$python = Find-CompatiblePython
if ($null -eq $python) { $python = Install-CompatiblePython }

# ---------------------------------------------------------------------------------------------
# 4. Install into an isolated, AgentNexus-owned location.
# ---------------------------------------------------------------------------------------------
$versionRoot = Join-Path (Join-Path $InstallRoot 'connector') $manifest.connector_version
$venv = Join-Path $versionRoot 'venv'
$null = New-Item -ItemType Directory -Force -Path $versionRoot

$artifactPath = Join-Path $versionRoot $artifact.filename
[IO.File]::WriteAllBytes($artifactPath, $artifactBytes)

$venvPython = Join-Path $venv 'Scripts\python.exe'
if ((Test-Path -LiteralPath $venv) -and -not (Test-CompatiblePython $venvPython)) {
    Write-Step 'Replacing an incomplete or incompatible Python environment'
    Remove-IncompatibleOwnedVenv $venv $versionRoot
}

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    Write-Step 'Creating an isolated Python environment'
    $pythonArguments = @($python.Arguments)
    & $python.Executable @pythonArguments -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated Python environment.' }
}

Write-Step 'Installing the connector'
# --no-input and the explicit local path: pip must install the verified file on disk, never
# resolve a name against an index, which would undo every check above.
& $venvPython -m pip install --quiet --no-input --upgrade "$artifactPath"
if ($LASTEXITCODE -ne 0) { throw 'The connector could not be installed into its own environment.' }

if ($SkipSetup) {
    Write-Host ''
    Write-Host "Installed $($manifest.connector_version) into $versionRoot. Setup was skipped."
    return
}

# ---------------------------------------------------------------------------------------------
# 5. Hand over. The connector prompts for the invitation itself, masked: it is never an argument
#    here, so it cannot appear in a process listing or in PowerShell history.
# ---------------------------------------------------------------------------------------------
Write-Host ''
$setupArguments = @('setup', '--origin', $origin, '--install-root', $InstallRoot)
if ($PSBoundParameters.ContainsKey('Runtime')) {
    $setupArguments += @('--runtime', $Runtime)
}
& (Join-Path $venv 'Scripts\agentnexus-connector.exe') @setupArguments
exit $LASTEXITCODE
