$ErrorActionPreference = 'Stop'
if (-not $env:CAMCTL_DATA_DIR) {
    $env:CAMCTL_DATA_DIR = Join-Path $PSScriptRoot 'data'
}
Push-Location (Join-Path $PSScriptRoot 'apps/client')
try {
    npm start
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
