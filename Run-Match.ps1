[CmdletBinding()]
param(
    [ValidateRange(0,100000)][int]$Limit=0,
    [ValidateRange(1,256)][int]$BatchSize=16,
    [ValidateRange(1,65535)][int]$Port=8102,
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9_.-]*$')][string]$ContainerName='capa-ai-match',
    [string]$Image='capa-ai-match:local', [string]$DataDirectory='data', [string]$EnvFile='.env.docker',
    [string]$Source, [string]$Sheet,
    [switch]$PrepareOnly, [switch]$RebuildIndex, [switch]$Stop
)
$ErrorActionPreference='Stop'
if ($Stop -and ($PrepareOnly -or $RebuildIndex -or $Source)) { throw '-Stop cannot be combined with preparation options.' }
if ($Sheet -and -not $Source) { throw 'Use -Source with -Sheet.' }
$repoRoot=$PSScriptRoot
if ($ContainerName -ne 'capa-ai-match' -and -not $PSBoundParameters.ContainsKey('DataDirectory')) { $DataDirectory=".local/$ContainerName/data" }
function ProjectPath([string]$Value) { if (-not [IO.Path]::IsPathRooted($Value)) { $Value=Join-Path $repoRoot $Value }; return [IO.Path]::GetFullPath($Value) }
function HashText([string]$Value) {
    $sha=[Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value)))).Replace('-','').ToLowerInvariant() } finally { $sha.Dispose() }
}
$projectId=(HashText $repoRoot.ToLowerInvariant()).Substring(0,12)
$localDirectory=Join-Path $repoRoot ".local\$ContainerName"
New-Item -ItemType Directory -Path $localDirectory -Force | Out-Null
$launcherLog=Join-Path $localDirectory 'setup.log'
$originalPath=$env:PATH
$lock=$null
try {
    try { $lock=[IO.File]::Open((Join-Path $localDirectory 'launch.lock'),'OpenOrCreate','ReadWrite','None') } catch { throw 'Another MATCH setup for this container is active. Wait for it to finish.' }
    $dockerExe=Get-Command docker -ErrorAction SilentlyContinue
    if (-not $dockerExe) { $dockerExe=@((Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker.exe'),(Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin\docker.exe')) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1 }
    if (-not $dockerExe) { throw 'Install Docker Desktop and start its Linux engine. Host Python is not needed.' }
    function Invoke-Docker([string[]]$Arguments,[switch]$Capture,[switch]$AllowFailure) {
        $saved=$ErrorActionPreference; $lines=[Collections.Generic.List[string]]::new()
        try {
            $ErrorActionPreference='Continue'
            & $dockerExe @Arguments 2>&1 | ForEach-Object { $line=$_.ToString(); if (-not $line -or $line -eq 'System.Management.Automation.RemoteException') { return }; [IO.File]::AppendAllText($launcherLog,$line+[Environment]::NewLine); if ($Capture) { $lines.Add($line) } else { Write-Host $line } }
            $script:dockerExitCode=$LASTEXITCODE
        } finally { $ErrorActionPreference=$saved }
        if ($dockerExitCode -ne 0 -and -not $AllowFailure) { throw "Docker failed (exit $dockerExitCode). See the error above and $launcherLog" }
        if ($Capture) { return ($lines -join [Environment]::NewLine) }
    }
    Write-Host '[1/5] Checking Docker Linux engine...'
    $engine=Invoke-Docker @('info','--format','{{.OSType}}') -Capture
    if ($engine.Trim() -ne 'linux') { throw 'Start Docker Desktop with Linux containers and rerun.' }
    $dataPath=ProjectPath $DataDirectory
    $existing=$null
    $containerId=Invoke-Docker @('container','ls','--all','--filter',('name=^/'+$ContainerName+'$'),'--format','{{.ID}}') -Capture
    if ($containerId) {
        $existing=@((Invoke-Docker @('container','inspect','--format','{"Image":{{json .Image}},"AutoRemove":{{json .HostConfig.AutoRemove}},"Config":{"Image":{{json .Config.Image}},"Labels":{{json .Config.Labels}}},"State":{{json .State}},"Mounts":{{json .Mounts}}}',$ContainerName) -Capture) | ConvertFrom-Json)[0]
        if ($existing.Config.Labels.'capa.match.project' -ne $projectId) {
            $sameData=@($existing.Mounts | Where-Object { $_.Type -eq 'bind' -and $_.Destination -eq '/data' -and ([string]$_.Source).Replace('\','/').TrimEnd('/') -eq $dataPath.Replace('\','/').TrimEnd('/') }).Count -eq 1
            $legacyOwned=-not $existing.Config.Labels.'capa.match.project' -and $existing.Config.Image -eq $Image -and $sameData
            if (-not $legacyOwned) { throw "Container '$ContainerName' belongs to another launcher/checkout. No service was changed. Use another -ContainerName and -Port with separate data." }
            Write-Host 'Recognised the previous MATCH launcher using this exact data mount. It will be upgraded after preparation passes.'
        }
    }
    if ($Stop) {
        if ($existing -and $existing.State.Running) { Invoke-Docker @('stop','-t','30',$ContainerName) }
        Write-Host 'MATCH stopped or already stopped. Source, models and indexes were retained.'; return
    }
    $envPath=ProjectPath $EnvFile
    if ($dataPath.Contains(',') -or $repoRoot.Contains(',') -or ($Source -and $Source.Contains(','))) { throw 'Docker bind-mount paths cannot contain commas.' }
    New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
    if (-not (Test-Path -LiteralPath $envPath)) { Copy-Item -LiteralPath (Join-Path $repoRoot 'docker.env.example') -Destination $envPath }
    $configuration=@{}
    foreach ($line in [IO.File]::ReadAllLines($envPath)) {
        if ($line -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$') {
            $key=$Matches[1]; $value=$Matches[2].Trim()
            if ($value.Length -ge 2 -and (($value.StartsWith("'") -and $value.EndsWith("'")) -or ($value.StartsWith('"') -and $value.EndsWith('"')))) { $value=$value.Substring(1,$value.Length-2) }
            $configuration[$key]=$value
        }
    }
    $fixed=@{CAPA_ENV='local'; CAPA_PROJECT_ROOT='/app'; CAPA_DATA_DIR='/data'; CAPA_CACHE_DIR='/data/cache'; MATCH_HOST='0.0.0.0'; MATCH_PORT='8102'; MATCH_MODEL_CACHE='/data/models/huggingface'; HF_HUB_OFFLINE='1'; TRANSFORMERS_OFFLINE='1'}
    foreach ($key in $fixed.Keys) { $configuration[$key]=$fixed[$key] }
    $runtimeEnv=Join-Path $localDirectory 'runtime.env'
    function Write-RuntimeEnv {
        $lines=@($configuration.Keys | Sort-Object | ForEach-Object { $value=[string]$configuration[$_]; if ($value.Contains("`n") -or $value.Contains("`r") -or $value.Contains([string][char]0)) { throw "Invalid multiline Docker value for $_" }; "$_=$value" })
        [IO.File]::WriteAllLines($runtimeEnv,[string[]]$lines,[Text.UTF8Encoding]::new($false))
    }
    Write-RuntimeEnv
    Write-Host '[2/5] Checking repository source/model downloads...'
    $manifest=Get-Content -LiteralPath (Join-Path $repoRoot 'runtime-assets.json') -Raw | ConvertFrom-Json
    $missing=$false
    $missingBytes=[long]0
    foreach ($item in $manifest.files) {
        $path=[IO.Path]::GetFullPath((Join-Path $repoRoot ([string]$item.path)))
        if (-not $path.StartsWith($repoRoot+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) { throw 'Runtime manifest path escapes the checkout.' }
        if (-not [IO.File]::Exists($path) -or ([IO.FileInfo]::new($path)).Length -ne $item.size_bytes) { $missing=$true; $missingBytes+=[long]$item.size_bytes }
    }
    $drive=[IO.DriveInfo]::new([IO.Path]::GetPathRoot($dataPath))
    if ($drive.AvailableFreeSpace -lt (256MB+$missingBytes)) { throw 'Not enough free disk space for MATCH setup. Free space on the data drive (several GB recommended) and rerun; no service was replaced.' }
    if ($missing) {
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Install Git for Windows and sign in to the private repository, then rerun.' }
        $saved=$ErrorActionPreference
        try { $ErrorActionPreference='Continue'; git lfs version 2>&1 | Out-Null; $hasLfs=$LASTEXITCODE -eq 0 } finally { $ErrorActionPreference=$saved }
        if (-not $hasLfs) {
            $spec=(Get-Content (Join-Path $repoRoot 'runtime-bootstrap.json') -Raw | ConvertFrom-Json).git_lfs
            $archive=Join-Path $localDirectory 'git-lfs.zip'
            [Net.ServicePointManager]::SecurityProtocol=[Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            if (-not (Test-Path $archive) -or (Get-FileHash $archive -Algorithm SHA256).Hash -ne $spec.sha256) {
                Invoke-WebRequest -Uri $spec.url -OutFile ($archive+'.partial') -UseBasicParsing -TimeoutSec 180
                if ((Get-FileHash ($archive+'.partial') -Algorithm SHA256).Hash -ne $spec.sha256) { throw 'Git LFS download checksum mismatch.' }
                Move-Item -LiteralPath ($archive+'.partial') -Destination $archive -Force
            }
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $zip=[IO.Compression.ZipFile]::OpenRead($archive)
            try { $entries=@($zip.Entries | Where-Object { $_.Name -eq 'git-lfs.exe' }); if ($entries.Count -ne 1) { throw 'Verified archive is missing git-lfs.exe.' }; [IO.Compression.ZipFileExtensions]::ExtractToFile($entries[0],(Join-Path $localDirectory 'git-lfs.exe'),$true) } finally { $zip.Dispose() }
            $env:PATH=$localDirectory+[IO.Path]::PathSeparator+$env:PATH
            $portableLfs=(Join-Path $localDirectory 'git-lfs.exe').Replace('\','/')
            foreach ($filter in @(@('clean','clean -- %f'),@('smudge','smudge -- %f'),@('process','filter-process'))) {
                git -c "safe.directory=$($repoRoot.Replace('\','/'))" -C $repoRoot config --local ("filter.lfs."+$filter[0]) ('"'+$portableLfs+'" '+$filter[1])
                if ($LASTEXITCODE -ne 0) { throw 'Could not configure Git LFS for this checkout.' }
            }
            git -c "safe.directory=$($repoRoot.Replace('\','/'))" -C $repoRoot config --local filter.lfs.required true
        }
        $saved=$ErrorActionPreference
        try { $ErrorActionPreference='Continue'; git -c "safe.directory=$($repoRoot.Replace('\','/'))" -c core.longpaths=true -C $repoRoot lfs pull '--include=data/runtime/**,data/sources/trackwise_records.csv' '--exclude=' 2>&1 | ForEach-Object { Write-Host $_.ToString() }; $lfsExit=$LASTEXITCODE } finally { $ErrorActionPreference=$saved }
        if ($lfsExit -ne 0) { throw 'Git LFS retrieval failed. Check repository access and rerun.' }
    }
    Write-Host '[3/5] Building the MATCH image (unchanged layers are cached)...'
    # A fresh provenance attestation can change an otherwise cached image ID.
    # Keep the local runtime image stable so unchanged launches reuse its container.
    Invoke-Docker @('build','--provenance=false','--platform','linux/amd64','--tag',$Image,$repoRoot)
    $mount="type=bind,source=$dataPath,target=/data"
    $prepare=@('run','--rm','--init','--no-healthcheck','--platform','linux/amd64','--env-file',$runtimeEnv,'--mount',$mount,'--mount',("type=bind,source="+(Join-Path $repoRoot 'data')+",target=/bundle,readonly"))
    if ($Source) { $sourcePath=ProjectPath $Source; if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) { throw "Source file not found: $sourcePath" }; $containerSource='/input/source'+[IO.Path]::GetExtension($sourcePath); $prepare+=@('--mount',"type=bind,source=$sourcePath,target=$containerSource,readonly") }
    $prepare+=@($Image,'python','-u','-m','capa_match.docker_setup','prepare','--limit',[string]$Limit,'--batch-size',[string]$BatchSize)
    if ($Source) { $prepare+=@('--source',$containerSource) }; if ($Sheet) { $prepare+=@('--sheet',$Sheet) }; if ($RebuildIndex) { $prepare+='--rebuild-index' }
    Write-Host '[4/5] Importing the source and preparing the real search index inside Docker...'
    Invoke-Docker $prepare
    if ($PrepareOnly) { Write-Host 'Preparation passed. Source, model and real search index are ready; the API was not started.'; return }
    $state=Get-Content -LiteralPath (Join-Path $dataPath '.setup/match.json') -Raw | ConvertFrom-Json
    foreach ($key in @('CAPA_SOURCE_SNAPSHOT','CAPA_SOURCE_CSV','MATCH_INDEX_DIR','MATCH_MODEL_NAME','MATCH_MODEL_CACHE','MATCH_ALLOWED_SITES_JSON')) { $configuration[$key]=[string]$state.$key }
    Write-RuntimeEnv
    $configId=HashText ((Get-Content $runtimeEnv -Raw)+$dataPath+[string]$Port+[string]$state.generation)
    $imageId=(Invoke-Docker @('image','inspect','--format','{{.Id}}',$Image) -Capture).Trim()
    if ($existing -and ($existing.Image -ne $imageId -or $existing.Config.Labels.'capa.match.configuration' -ne $configId)) {
        if ($existing.State.Running) { Invoke-Docker @('stop','-t','30',$ContainerName) }
        if (-not $existing.AutoRemove) { Invoke-Docker @('container','rm',$ContainerName) }
        $existing=$null
    }
    Write-Host '[5/5] Starting MATCH and verifying real single-site/all-sites search...'
    if (-not $existing) {
        $networks=Invoke-Docker @('network','ls','--filter','name=^capa-ai$','--format','{{.Name}}') -Capture
        if (-not $networks) { Invoke-Docker @('network','create','capa-ai') }
        Invoke-Docker @('run','--detach','--init','--restart','unless-stopped','--platform','linux/amd64','--name',$ContainerName,'--network','capa-ai','--label',"capa.match.project=$projectId",'--label',"capa.match.configuration=$configId",'--publish',('127.0.0.1:'+$Port+':8102'),'--env-file',$runtimeEnv,'--mount',$mount,$Image)
    } elseif (-not $existing.State.Running) { Invoke-Docker @('start',$ContainerName) }
    $ready=$false
    for ($attempt=0; $attempt -lt 90; $attempt++) { Invoke-Docker @('exec',$ContainerName,'python','/opt/capa/healthcheck.py') -Capture -AllowFailure | Out-Null; if ($dockerExitCode -eq 0) { $ready=$true; break }; Start-Sleep -Seconds 1 }
    if (-not $ready) { throw "MATCH did not start. Run: docker logs $ContainerName" }
    Invoke-Docker @('exec',$ContainerName,'python','-u','-m','capa_match.docker_setup','verify')
    Write-Host "MATCH ready: http://127.0.0.1:$Port/docs | $($state.records) indexed records | $($state.sites) allowed sites."
    Write-Host "Source, models and index persist in: $dataPath. No OpenAI key or paid API calls are needed."
} finally { $env:PATH=$originalPath; if ($lock) { $lock.Dispose() } }
