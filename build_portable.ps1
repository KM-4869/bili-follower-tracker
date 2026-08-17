param(
    [string]$Version = "0.2.0"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$windowsVenvPython = Join-Path $projectRoot ".venv-build\Scripts\python.exe"
$msysVenvPython = Join-Path $projectRoot ".venv-build\bin\python.exe"

Set-Location $projectRoot

if (-not (Test-Path -LiteralPath $windowsVenvPython) -and -not (Test-Path -LiteralPath $msysVenvPython)) {
    python -m venv (Join-Path $projectRoot ".venv-build")
}

$venvPython = if (Test-Path -LiteralPath $windowsVenvPython) {
    $windowsVenvPython
} else {
    $msysVenvPython
}

& $venvPython -m pip install -r requirements.txt -r requirements-build.txt
& $venvPython -m PyInstaller --noconfirm --clean BiliFollowerTracker.spec

$portableFolder = Join-Path $projectRoot "dist\BiliFollowerTracker"
$releaseFolder = Join-Path $projectRoot "release"
$archive = Join-Path $releaseFolder "BiliFollowerTracker-$Version-windows-x64.zip"

New-Item -ItemType Directory -Force -Path $releaseFolder | Out-Null
Copy-Item -LiteralPath (Join-Path $projectRoot "PORTABLE_README.txt") -Destination $portableFolder -Force

$licenseRoot = Join-Path $portableFolder "THIRD_PARTY_LICENSES"
New-Item -ItemType Directory -Force -Path $licenseRoot | Out-Null
$sitePackages = (& $venvPython -c "import site; print(site.getsitepackages()[0])").Trim()
Get-ChildItem -LiteralPath $sitePackages -Directory -Filter "*.dist-info" | ForEach-Object {
    $licenseFiles = Get-ChildItem -LiteralPath $_.FullName -Recurse -File | Where-Object {
        $_.Name -match '^(LICENSE|COPYING|NOTICE)'
    }
    if ($licenseFiles) {
        $destination = Join-Path $licenseRoot $_.Name
        New-Item -ItemType Directory -Force -Path $destination | Out-Null
        $licenseFiles | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $destination $_.Name) -Force
        }
    }
}

$pythonBase = (& $venvPython -c "import sys; print(sys.base_prefix)").Trim()
$runtimeLicenseRoot = Join-Path $pythonBase "share\licenses"
@("python3.12", "gcc-libs", "mingw-w64-libraries", "openssl", "bzip2", "expat", "libffi", "mpdecimal", "xz", "zlib") | ForEach-Object {
    $source = Join-Path $runtimeLicenseRoot $_
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $licenseRoot $_) -Recurse -Force
    }
}
$pythonLicense = Join-Path $pythonBase "LICENSE.txt"
if (Test-Path -LiteralPath $pythonLicense) {
    Copy-Item -LiteralPath $pythonLicense -Destination (Join-Path $licenseRoot "PYTHON-LICENSE.txt") -Force
}

$compressed = $false
for ($attempt = 1; $attempt -le 5; $attempt++) {
    if (Test-Path -LiteralPath $archive) {
        Remove-Item -LiteralPath $archive -Force
    }
    try {
        Compress-Archive -Path $portableFolder -DestinationPath $archive -CompressionLevel Optimal
        $compressed = $true
        break
    } catch {
        if ($attempt -eq 5) { throw }
        Start-Sleep -Seconds 2
    }
}
if (-not $compressed) {
    throw "Failed to create portable archive."
}

$hash = Get-FileHash -Algorithm SHA256 -LiteralPath $archive
$hashFile = "$archive.sha256.txt"
"$($hash.Hash)  $([System.IO.Path]::GetFileName($archive))" | Set-Content -LiteralPath $hashFile -Encoding ascii
Write-Host "Portable package: $archive"
Write-Host "SHA-256: $($hash.Hash)"
