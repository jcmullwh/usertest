# This script is a thin wrapper around offline_first_success.ps1 for backwards compatibility.
& "$PSScriptRoot\offline_first_success.ps1" @Args
exit $LASTEXITCODE
