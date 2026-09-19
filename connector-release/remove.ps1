<#
.SYNOPSIS
Removes one AgentNexus agent profile from this machine.

.DESCRIPTION
The counterpart to connect.ps1, verifying the connector in exactly the same way: the signed
release manifest, the pinned artifact digest, the same embedded public key.

It deliberately does not look for a connector already installed on this machine and use that. A
removal is the last thing that should run whichever version happens to be lying around: an older
one predates the quarantine step and the runtime-profile rules this command depends on, and would
delete a private key that the current design keeps on purpose.

What it removes, and what it does not:

  * It removes the AgentNexus integration for ONE named profile: that profile's MCP registration,
    its AgentNexus profile directory, and a SOUL.md only when AgentNexus wrote it and nobody has
    edited it since.
  * It does NOT delete the private key. The key is moved to a quarantine directory, because the
    identity it proves is still registered until an operator retires it.
  * It does NOT retire the agent or revoke its key on the server. Those are operator actions in
    AgentNexus and no agent may perform them on itself; the connector prints the exact commands
    to hand to your operator.
  * It does NOT delete the runtime profile unless you pass -PurgeRuntimeProfile, and it never
    touches another profile or a shared connector release.

.EXAMPLE
& ([scriptblock]::Create((irm 'https://agntnexus.com/remove.ps1'))) -Profile lexilux

.EXAMPLE
& ([scriptblock]::Create((irm 'https://agntnexus.com/remove.ps1'))) -Profile lexilux -PurgeRuntimeProfile
#>
[CmdletBinding()]
param(
    # The profile to remove. Required, and deliberately so: there is no safe default for a
    # destructive command, and a guessed one would eventually remove the wrong agent.
    [Parameter(Mandatory = $true)]
    [Alias('Profile')]
    [string]$AgentProfile,

    # The AgentNexus origin. Overridable so this exact file can be tested against a local fixture
    # release; the signature check does not weaken when it is, because the key below does not move.
    [ValidatePattern('^https?://[A-Za-z0-9.-]+(:\d{1,5})?$')]
    [string]$Origin = 'https://agntnexus.com',

    # Install root. The same directory connect.ps1 installed into.
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'AgentNexus'),

    # Also delete the complete runtime profile. Far more destructive than the default: it removes
    # provider configuration, API credentials, model choice, sessions and memories that AgentNexus
    # never created and cannot restore.
    [switch]$PurgeRuntimeProfile,

    # Print what would happen and stop before downloading or changing anything.
    [switch]$WhatIfOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# --- shared bootstrap: begin ---------------------------------------------------------------
# Everything between these markers is byte-identical in `connect.ps1` and `remove.ps1`, and
# `test_connector_bootstrap.py` fails if it ever stops being. Neither loader can dot-source a
# shared file: both are fetched with `irm` and run as a script block, so a second fetch would
# be an unverified download in the middle of the code that exists to verify downloads. The
# duplication is therefore deliberate, and the test is what keeps it from becoming divergence.
$ReleasePublicKeyX = 'd13dd7292bf0453357fcee6064e227b889d09a912b6387b274bcb03f07c3bbbb'
$ReleasePublicKeyY = '27033143957d30bd17a715ef95c422ebbc5c3094d4ddbef4ae78500179050ab8'

$ManifestPath = '/connector/connector-release.json'
$MaxManifestBytes = 65536
$MaxArtifactBytes = 64MB

function Write-Step([string]$Message) { Write-Host "  $Message" }

# The one canonical AgentNexus profile grammar. Identical to connect.sh and to profiles.py, and
# deliberately inside what every consumer promises rather than what any one of them happens to
# allow: real Hermes v0.20.6 reports `[a-z0-9][a-z0-9_-]{0,63}` and lower-cases silently, while
# its own `profile create --help` promises only "lowercase, alphanumeric". Sitting inside the
# promise is what stops a future Hermes turning a working name into a failed setup.
#
# `(?-i)` is not decoration: -match is case-insensitive by default, so without it `Agent2` would
# pass here and then be silently lower-cased by Hermes into another profile's name.
$ProfileNamePattern = '(?-i)^[a-z][a-z0-9]{0,31}$'
$ProfileNameRule = 'Use lower-case letters and digits only, starting with a letter, 1 to 32 characters - no hyphens, underscores, or dots.'

function Assert-SafeProfileName([string]$Name) {
    # Everything about the name is decided here, before a single byte is fetched and before
    # anything is written, so an unusable name costs no download and mutates nothing.
    if ($Name -notmatch $ProfileNamePattern) {
        throw "The profile name '$Name' is not a valid profile name. $ProfileNameRule For example: agent2."
    }
    # The reserved-device list is the part a pattern cannot express. It is not theoretical: a real
    # Hermes answered "Profile 'nul' already exists" here, because Windows resolves the name as a
    # path whatever the extension, so the profile would appear to work and keep nothing.
    $reserved = @(
        'con', 'prn', 'aux', 'nul', 'clock$',
        'com1', 'com2', 'com3', 'com4', 'com5', 'com6', 'com7', 'com8', 'com9',
        'lpt1', 'lpt2', 'lpt3', 'lpt4', 'lpt5', 'lpt6', 'lpt7', 'lpt8', 'lpt9',
        'all', 'migration'
    )
    if ($reserved -contains $Name) {
        throw "The profile name '$Name' is reserved by Windows or by AgentNexus. A real Hermes answered ""Profile 'nul' already exists"" to one of these. Choose another, for example: agent2."
    }
}

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

# The embedded coordinates are checked for shape before anything tries to use them, because
# .NET's own answer to malformed key material actively misdirects. In a cold process - which is
# exactly what a loader fetched with `irm` and run once is - `ImportParameters` rejects a point it
# cannot accept with `PlatformNotSupportedException: The specified curve 'nistP256' or its
# parameters are not valid for this platform.` That sentence names the curve and the platform, so
# it reads as "this runtime has no P-256". It is not. Measured on Windows 11 on 2026-09-17, both
# Windows PowerShell 5.1 and PowerShell 7 import a well-formed point and verify a real release
# signature without complaint; what they refuse is 31 bytes, 33 bytes, an unstamped placeholder,
# or a pair that is not on the curve. The same input in a process that has already imported one
# valid P-256 key yields an ordinary `CryptographicException` instead, which is why the misleading
# form only ever appears in the one situation an applicant is actually in.
function Assert-ReleaseCoordinate([string]$Value, [string]$Name) {
    # The release build stamps lower-case hex, but hex case is only presentation: the same 32
    # bytes may appear in a manually maintained loader as upper-case. Preserve that compatibility
    # while still refusing every non-hex or non-32-byte value before it reaches cryptography.
    if ($Value -notmatch '^[0-9A-Fa-f]{64}$') {
        throw "This loader's embedded release public key coordinate $Name is not 64 hexadecimal characters, so it is not a P-256 coordinate. This copy cannot verify anything and refuses to try. Fetch the loader again from the origin."
    }
}

function New-ReleaseVerifierFromCurveName([byte[]]$X, [byte[]]$Y) {
    # The portable construction, and the one the release runbook documents. `ECParameters.Q` is a
    # struct, so the point is built and then assigned whole: writing `$parameters.Q.X` would
    # mutate a copy and leave the key silently empty.
    $parameters = New-Object System.Security.Cryptography.ECParameters
    $parameters.Curve = [System.Security.Cryptography.ECCurve]::CreateFromFriendlyName('nistP256')
    $point = New-Object System.Security.Cryptography.ECPoint
    $point.X = $X
    $point.Y = $Y
    $parameters.Q = $point
    $ecdsa = [System.Security.Cryptography.ECDsa]::Create()
    try { $ecdsa.ImportParameters($parameters) }
    catch { $ecdsa.Dispose(); throw }
    return $ecdsa
}

function New-ReleaseVerifierFromCngBlob([byte[]]$X, [byte[]]$Y) {
    # The same key by a route that resolves no name. The construction above reaches the provider
    # through a *friendly name*: Windows PowerShell 5.1 leaves `Oid.Value` empty for 'nistP256'
    # and carries the name alone, so that route depends on a lookup a host can lack. A
    # BCRYPT_ECCKEY_BLOB names CNG's own P-256 algorithm by magic constant instead - same curve,
    # same two coordinates, same raw r||s verification, one fewer thing to resolve.
    #
    # It relaxes nothing. Measured in both runtimes on 2026-09-17: this import refuses an
    # off-curve point, a fabricated pair and an all-zero pair with the same `CryptographicException`
    # the managed path gives, so a key that fails there does not pass here.
    $blob = New-Object byte[] (8 + $X.Length + $Y.Length)
    [BitConverter]::GetBytes([uint32]0x31534345).CopyTo($blob, 0)   # BCRYPT_ECDSA_PUBLIC_P256_MAGIC
    [BitConverter]::GetBytes([uint32]$X.Length).CopyTo($blob, 4)
    $X.CopyTo($blob, 8)
    $Y.CopyTo($blob, 8 + $X.Length)
    $key = [System.Security.Cryptography.CngKey]::Import(
        $blob, [System.Security.Cryptography.CngKeyBlobFormat]::EccPublicBlob)
    return New-Object System.Security.Cryptography.ECDsaCng $key
}

function New-ReleaseVerifier {
    Assert-ReleaseCoordinate $ReleasePublicKeyX 'X'
    Assert-ReleaseCoordinate $ReleasePublicKeyY 'Y'
    $x = Convert-FromHex $ReleasePublicKeyX
    $y = Convert-FromHex $ReleasePublicKeyY
    Add-Type -AssemblyName System.Core

    # Two constructions of one key, tried in order. Every branch either returns a verifier that
    # holds the published coordinates or throws: there is no path on which an unverified manifest
    # is treated as verified, which is the property that must survive this function.
    $refusals = @()
    try { return New-ReleaseVerifierFromCurveName $x $y }
    catch { $refusals += "named curve: $($_.Exception.Message)" }
    try { return New-ReleaseVerifierFromCngBlob $x $y }
    catch { $refusals += "CNG blob: $($_.Exception.Message)" }

    # Both refused the same coordinates. That is a statement about this key, not about P-256 on
    # this machine, and saying so is the difference between correcting a release stamp and
    # rewriting a verifier that was never wrong.
    #
    # The underlying refusals are kept, because a screenshot is often all an operator gets - but
    # they are introduced, not quoted bare. One of them is .NET's own `The specified curve
    # 'nistP256' or its parameters are not valid for this platform.`, which is the sentence that
    # sent issue #63 looking for a runtime incompatibility that was never there.
    throw "This loader's embedded release public key is not a valid P-256 public point, so nothing was verified, downloaded or changed. The fault is in this copy of the loader, not in this computer: where a refusal below says the curve is not valid for this platform, that is .NET describing key material it rejected, not a runtime without P-256. Fetch the loader again from the origin. Refusals: $($refusals -join '; ')"
}

function Test-ReleaseSignature([byte[]]$Payload, [byte[]]$Signature) {
    if ($Signature.Length -ne 64) {
        throw "The release signature is $($Signature.Length) bytes; expected 64 raw r||s bytes."
    }
    $ecdsa = New-ReleaseVerifier
    try {
        return $ecdsa.VerifyData($Payload, $Signature, [System.Security.Cryptography.HashAlgorithmName]::SHA256)
    }
    finally { $ecdsa.Dispose() }
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

# --- shared bootstrap: end -----------------------------------------------------------------

# ---------------------------------------------------------------------------------------------
# 1. Refuse early on anything this loader cannot safely act on.
# ---------------------------------------------------------------------------------------------
if (-not [Environment]::Is64BitOperatingSystem) {
    throw 'A 64-bit Windows installation is required.'
}

# Before the manifest fetch on purpose: a name that cannot be a profile must cost nothing.
Assert-SafeProfileName $AgentProfile
Write-Step "Removing agent profile: $AgentProfile"

$profileRoot = Join-Path (Join-Path $InstallRoot 'profiles') $AgentProfile
if (-not (Test-Path -LiteralPath $profileRoot)) {
    # Not fatal. A previous run may have finished the local part and left only the quarantined
    # key, which the connector reports precisely. Refusing here would block that second run,
    # which is the one that finishes the job.
    Write-Step "No profile directory at $profileRoot; the connector will report what is left."
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
    Write-Host "Would verify and use connector $($manifest.connector_version) to remove profile $AgentProfile."
    if ($PurgeRuntimeProfile) {
        Write-Host 'Would ALSO delete the complete runtime profile, including data AgentNexus did not create.'
    } else {
        Write-Host 'The runtime profile would be kept.'
    }
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

$python = Find-CompatiblePython
if ($null -eq $python) { $python = Install-CompatiblePython }

# ---------------------------------------------------------------------------------------------
# 4. Install the verified connector into its own version directory.
#
#    The same layout connect.ps1 uses, and for the same reason: several profiles and several
#    releases coexist here. Nothing in this file removes a release directory. Removing one
#    profile must never break another profile that runs the same version.
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

# ---------------------------------------------------------------------------------------------
# 5. Hand over. The connector shows what it found and asks for the profile name to be typed back.
#
#    This loader deliberately passes no confirmation of its own. A destructive step has to be a
#    decision made in front of the facts, and a loader that pre-confirmed it would remove the one
#    moment where somebody can still say no.
# ---------------------------------------------------------------------------------------------
Write-Host ''
$removeArguments = @('profile', 'remove', '--profile', $AgentProfile, '--install-root', $InstallRoot)
if ($PurgeRuntimeProfile) {
    $removeArguments += '--purge-runtime-profile'
}
& (Join-Path $venv 'Scripts\agentnexus-connector.exe') @removeArguments
$connectorExitCode = $LASTEXITCODE

# Hand control back to the caller. Never `exit`: this file runs as a script block inside the
# caller's own PowerShell, so `exit` would close their window and take the message with it.
$global:LASTEXITCODE = $connectorExitCode
if ($connectorExitCode -ne 0) {
    Write-Host ''
    Write-Warning "Removal did not complete (exit $connectorExitCode). Nothing was destroyed that the connector did not report."
    Write-Host 'Removal is safe to repeat: fix what it reported, then run the same command again.'
    Write-Host ''
}
return
