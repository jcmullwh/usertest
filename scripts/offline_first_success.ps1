Write-Warning "offline_first_success.ps1 is deprecated."
Write-Host "NOTE: This path only rerenders a golden fixture run; it does NOT execute any agents or validate real performance." -ForegroundColor Yellow
Write-Host "Use scripts/offline_fixture_rerender.ps1 (or run usertest/usertest-backlog normally) for real runs." -ForegroundColor Yellow

& "$PSScriptRoot/offline_fixture_rerender.ps1" @Args
exit $LASTEXITCODE
