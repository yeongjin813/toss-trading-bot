# Pull EC2 backups to this PC (run weekly while bot is unattended).
# Usage (PowerShell):
#   .\scripts\pull_backups_from_ec2.ps1 -PemPath "C:\Users\WD\Downloads\tb.pem" -Host "3.35.26.155"

param(
    [string]$PemPath = "$env:USERPROFILE\Downloads\tb.pem",
    [string]$Host = "3.35.26.155",
    [string]$User = "ubuntu",
    [string]$RemoteDir = "/home/ubuntu/toss-trading-bot/backups",
    [string]$LocalDir = "$env:USERPROFILE\Desktop\toss-bot-backups"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $PemPath)) {
    Write-Error "PEM not found: $PemPath"
}

New-Item -ItemType Directory -Force -Path $LocalDir | Out-Null

Write-Host "Pulling $User@${Host}:$RemoteDir -> $LocalDir"
scp -i $PemPath -r "${User}@${Host}:${RemoteDir}/*" $LocalDir

Write-Host "Done. Latest backup folders under $LocalDir"
