[CmdletBinding()]
param(
    [switch]$Build,
    [ValidateRange(1, 65535)][int]$Port = 8102,
    [string]$EnvFile = '.env.docker',
    [string]$DataDirectory = 'data',
    [string]$Image = 'capa-ai-match:local'
)
$ErrorActionPreference = 'Stop'
$repoRoot = $PSScriptRoot
function ProjectPath([string]$Value) {
    if ([IO.Path]::IsPathRooted($Value)) { return [IO.Path]::GetFullPath($Value) }
    return [IO.Path]::GetFullPath((Join-Path $repoRoot $Value))
}
$envPath = ProjectPath $EnvFile
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    Copy-Item -LiteralPath (Join-Path $repoRoot 'docker.env.example') -Destination $envPath
    throw "Created $envPath. Configure the source and service settings in it, then rerun this command. See DOCKER.md."
}
$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
if ($dockerCommand) {
    $dockerExe = $dockerCommand
} else {
    $dockerExe = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker.exe'),
        (Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin\docker.exe')
    ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $dockerExe) { throw 'Docker is not installed or its CLI is unavailable. Install/start Docker Desktop with Linux containers.' }
function Invoke-Docker([string[]]$DockerArguments) {
    # BuildKit sends ordinary progress to stderr. Windows PowerShell 5 must
    # judge native commands by their exit code, including when logs redirect.
    $savedPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $dockerExe @DockerArguments 2>&1 | ForEach-Object { $_.ToString() }
        $script:dockerExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $savedPreference
    }
}
$engine = Invoke-Docker @('info', '--format', '{{.OSType}}')
if ($dockerExitCode -ne 0 -or $engine.Trim() -ne 'linux') { throw 'Start the Docker Linux engine before running this command.' }
$dataPath = ProjectPath $DataDirectory
if ($dataPath.Contains(',')) { throw 'Docker bind-mount paths cannot contain a comma. Use -DataDirectory with another path.' }
New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
# Missing images are normal. Successful list queries avoid PowerShell 5 treating
# Docker's not-found stderr as a terminating error before the build can run.
$knownImage = Invoke-Docker @('image', 'ls', '--quiet', '--filter', ('reference=' + $Image))
if ($dockerExitCode -ne 0) { throw 'Could not query local Docker images.' }
$needsBuild = -not $knownImage
if ($Build -or $needsBuild) {
    Invoke-Docker @('build', '--platform', 'linux/amd64', '--tag', $Image, $repoRoot)
    if ($dockerExitCode -ne 0) { throw 'Docker image build failed. The service was not started.' }
}
$knownNetwork = Invoke-Docker @('network', 'ls', '--quiet', '--filter', 'name=^capa-ai$')
if ($dockerExitCode -ne 0) { throw 'Could not query Docker networks.' }
if (-not $knownNetwork) {
    Invoke-Docker @('network', 'create', 'capa-ai') | Out-Null
    if ($dockerExitCode -ne 0) { throw 'Could not create the capa-ai Docker network.' }
}
Write-Host 'Starting MATCH. Stop with Ctrl+C; all data stays in the mounted directory.'
$runArguments = @(
    'run', '--rm', '--init', '--platform', 'linux/amd64',
    '--name', 'capa-ai-match', '--network', 'capa-ai',
    '--publish', ('127.0.0.1:' + $Port + ':8102'),
    '--env-file', $envPath,
    '--env', 'CAPA_DATA_DIR=/data',
    '--env', 'MATCH_HOST=0.0.0.0',
    '--env', 'MATCH_PORT=8102',
    '--mount', ('type=bind,source=' + $dataPath + ',target=/data'),
    $Image
)
Invoke-Docker $runArguments
if ($dockerExitCode -ne 0) { throw 'Docker exited with an error. Check the output above.' }
