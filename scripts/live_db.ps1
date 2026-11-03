<#
.SYNOPSIS
  Local test copies of Oracle, SQL Server, PostgreSQL and MySQL in Docker, for tests\test_live_engines.py.

.DESCRIPTION
  scripts\live_db.ps1 up                 start the four containers and seed them (first time: a few minutes)
  scripts\live_db.ps1 start              start the containers (Docker Desktop too, if it is not running)
  scripts\live_db.ps1 seed               create the demo schema, the 2,000,000-row table and the read-only user
  scripts\live_db.ps1 status             what is running and reachable
  scripts\live_db.ps1 test               run the live tests (pytest -m live)
  scripts\live_db.ps1 stop               stop the containers; their data is kept for the next start
  scripts\live_db.ps1 remove             delete the containers and their data
  scripts\live_db.ps1 images             show the images, their size, and how to delete them

  Add engine names to work on some of them only:   scripts\live_db.ps1 up postgresql mysql

  The containers listen on 127.0.0.1 only, on ports that are not the engines' usual ones
  (Oracle 11521, SQL Server 11433, PostgreSQL 15432, MySQL 13306). Images, ports and the
  throw-away passwords are defined in scripts\live_engines.py.

  Starting the SQL Server container sets ACCEPT_EULA=Y, which accepts Microsoft's licence terms
  for SQL Server (free Developer edition).
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet('up', 'start', 'seed', 'status', 'test', 'stop', 'remove', 'images')]
    [string]$Command = 'status',
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Engines
)

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { Write-Error 'Run run.bat once first: it creates .venv.'; exit 1 }
Set-Location $root

function Invoke-Python {
    & $python @args
    if ($LASTEXITCODE -ne 0) { throw "python $($args -join ' ') failed" }
}

function Test-Docker {
    docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
    return $LASTEXITCODE -eq 0
}

function Start-DockerDesktop {
    if (Test-Docker) { return }
    $exe = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
    if (-not (Test-Path $exe)) { throw 'Docker is not running and Docker Desktop was not found.' }
    Write-Host 'Starting Docker Desktop ...'
    Start-Process $exe
    foreach ($i in 1..90) {
        Start-Sleep -Seconds 2
        if (Test-Docker) { return }
    }
    throw 'Docker Desktop did not start within 3 minutes.'
}

$config = (& $python -m scripts.live_engines config) | ConvertFrom-Json
$all = @($config.PSObject.Properties.Name)
if (-not $Engines) { $Engines = $all }
foreach ($name in $Engines) { if ($all -notcontains $name) { Write-Error "Unknown engine '$name'. Use: $($all -join ', ')"; exit 1 } }

function Start-Containers {
    Start-DockerDesktop
    foreach ($name in $Engines) {
        $e = $config.$name
        $existing = docker ps -a --filter "name=^$($e.container)$" --format '{{.Names}}'
        if ($existing) {
            Write-Host "$name : starting the existing container $($e.container)"
            docker start $e.container | Out-Null
        } else {
            Write-Host "$name : creating $($e.container) from $($e.image) on 127.0.0.1:$($e.port)"
            $run = @('run', '-d', '--name', $e.container, '-p', "127.0.0.1:$($e.port):$($e.container_port)")
            foreach ($p in $e.env.PSObject.Properties) { $run += @('-e', "$($p.Name)=$($p.Value)") }
            $run += $e.image
            docker @run | Out-Null
        }
        if ($LASTEXITCODE -ne 0) { throw "docker could not start $($e.container)" }
    }
    Invoke-Python -m scripts.live_engines wait @Engines
}

switch ($Command) {
    'start' { Start-Containers }
    'seed' { Invoke-Python -m scripts.live_engines seed @Engines }
    'up' { Start-Containers; Invoke-Python -m scripts.live_engines seed @Engines }
    'status' {
        if (Test-Docker) { docker ps -a --filter 'name=^dx-' --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' }
        else { Write-Host 'Docker is not running.' }
        Invoke-Python -m scripts.live_engines status
    }
    'test' { & $python -m pytest tests\test_live_engines.py -m live -v; exit $LASTEXITCODE }
    'stop' {
        # -t 20: a little longer than Docker's 10 s to shut down. The SQL Server container does not
        # exit on its own and is ended after the wait (exit code 137); its data is safe - it recovers on start.
        foreach ($name in $Engines) { docker stop -t 20 $config.$name.container | Out-Null; Write-Host "$name : stopped" }
    }
    'remove' {
        foreach ($name in $Engines) { docker rm -f -v $config.$name.container | Out-Null; Write-Host "$name : removed" }
    }
    'images' {
        foreach ($name in $all) { docker images $config.$name.image --format '{{.Repository}}:{{.Tag}}  {{.Size}}' }
        Write-Host ''
        Write-Host 'To delete them (after "live_db.ps1 remove"):'
        Write-Host "  docker rmi $(($all | ForEach-Object { $config.$_.image }) -join ' ')"
    }
}
