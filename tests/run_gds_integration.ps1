param(
    [Parameter(Mandatory = $true)]
    [string]$JarPath
)

$ErrorActionPreference = 'Stop'
$resolvedJar = (Resolve-Path -LiteralPath $JarPath).Path
$repository = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$composeFile = Join-Path $PSScriptRoot 'docker-compose.gds.yml'
$python = Join-Path $repository 'venv\Scripts\python.exe'
$project = 'tdissue19gds' + [guid]::NewGuid().ToString('N').Substring(0, 10)

if ((Split-Path -Leaf $resolvedJar) -ne 'td-issue19-gds-2.6.6.jar') {
    throw 'Expected the isolated GDS 2.6.6 test JAR'
}
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Existing Python virtual environment is unavailable'
}

$env:TD_GDS_TEST_JAR = $resolvedJar
$env:TD_GDS_TEST_PASSWORD = [guid]::NewGuid().ToString('N')

try {
    Write-Output "test_project=$project"
    docker compose -p $project -f $composeFile config --quiet
    if ($LASTEXITCODE -ne 0) { throw 'Isolated Compose configuration is invalid' }

    docker compose -p $project -f $composeFile up -d --pull never
    if ($LASTEXITCODE -ne 0) { throw 'Isolated Neo4j startup failed' }

    $portText = docker compose -p $project -f $composeFile port neo4j 7687
    $portMatch = [regex]::Match($portText, '^127\.0\.0\.1:(\d+)$')
    if ($LASTEXITCODE -ne 0 -or -not $portMatch.Success) {
        throw 'Isolated Bolt port is not bound to loopback'
    }
    $env:TD_GDS_TEST_URI = "bolt://127.0.0.1:$($portMatch.Groups[1].Value)"

    $ready = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            & $python -B -c "import os; from neo4j import GraphDatabase; driver=GraphDatabase.driver(os.environ['TD_GDS_TEST_URI'], auth=('neo4j', os.environ['TD_GDS_TEST_PASSWORD'])); driver.verify_connectivity(); driver.close()" 2>$null
            if ($LASTEXITCODE -eq 0) {
                $ready = $true
                break
            }
        } catch {}
        Start-Sleep -Seconds 2
    }
    if (-not $ready) { throw 'Isolated Neo4j did not become ready' }

    & $python -B -m unittest discover -s tests -p test_track1_gds_integration.py -v
    if ($LASTEXITCODE -ne 0) { throw 'GDS integration tests failed' }
}
finally {
    docker compose -p $project -f $composeFile down --timeout 15
    Remove-Item Env:TD_GDS_TEST_JAR -ErrorAction SilentlyContinue
    Remove-Item Env:TD_GDS_TEST_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:TD_GDS_TEST_URI -ErrorAction SilentlyContinue
}
