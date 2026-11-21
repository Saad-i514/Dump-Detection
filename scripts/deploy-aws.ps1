# Run from an authenticated AWS CLI session. This creates billable AWS resources.
$ErrorActionPreference = 'Stop'
function Invoke-Checked {
    & $args[0] $args[1..($args.Length - 1)]
    if ($LASTEXITCODE -ne 0) { throw "Command failed: $($args[0])" }
}
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
    Invoke-Checked aws sts get-caller-identity
    Invoke-Checked .\.venv\Scripts\python.exe scripts/package_deploy.py
    Invoke-Checked terraform -chdir=infra init -input=false
    Invoke-Checked terraform -chdir=infra validate
    Invoke-Checked terraform -chdir=infra plan '-out=deploy.tfplan' -input=false
    Invoke-Checked terraform -chdir=infra apply -input=false deploy.tfplan
    Invoke-Checked terraform -chdir=infra output
    Write-Output 'Allow approximately 10 minutes for the server to install its dependencies, then check /health.'
}
finally { Pop-Location }
