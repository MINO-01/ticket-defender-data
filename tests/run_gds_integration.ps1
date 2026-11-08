param(
    [Parameter(Mandatory = $true)]
    [string]$JarPath,

    # 공식 배포처에서 받은 JAR의 SHA-256을 호출자가 명시합니다.
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{64}$')]
    [string]$ExpectedJarSha256,

    # 저장소 밖 전용 venv의 python.exe입니다. 생략하면 저장소의 venv를 사용합니다.
    [string]$PythonPath,

    # 격리 GDS가 떠 있는 동안 같은 고정 원장으로 score-v1과 score-v2를 비교합니다.
    [string[]]$ComparisonLedgerDir = @(),
    [string]$ComparisonOutputDir
)

$ErrorActionPreference = 'Stop'
$resolvedJar = (Resolve-Path -LiteralPath $JarPath).Path
$repository = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$composeFile = Join-Path $PSScriptRoot 'docker-compose.gds.yml'
$python = if ($PythonPath) { (Resolve-Path -LiteralPath $PythonPath).Path } else { Join-Path $repository 'venv\Scripts\python.exe' }
$project = 'tdissue19gds' + [guid]::NewGuid().ToString('N').Substring(0, 10)

if ((Split-Path -Leaf $resolvedJar) -ne 'neo4j-graph-data-science-2.6.6.jar') {
    throw 'Expected the official GDS 2.6.6 JAR file name'
}
if ((Get-FileHash -LiteralPath $resolvedJar -Algorithm SHA256).Hash -ne $ExpectedJarSha256.ToUpperInvariant()) {
    throw 'GDS JAR SHA-256 does not match the expected value'
}
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Existing Python virtual environment is unavailable'
}
if ($PythonPath) {
    $venvRoot = Split-Path -Parent (Split-Path -Parent $python)
    if ((Split-Path -Leaf $python) -ne 'python.exe' -or -not (Test-Path -LiteralPath (Join-Path $venvRoot 'pyvenv.cfg'))) {
        throw 'PythonPath must be the python.exe of a virtual environment'
    }
}
if ($ComparisonLedgerDir.Count -gt 0 -and -not $ComparisonOutputDir) {
    throw 'ComparisonOutputDir is required with ComparisonLedgerDir'
}
Set-Location -LiteralPath $repository

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

    foreach ($ledgerDir in $ComparisonLedgerDir) {
        & $python -B -m evaluation.capstone_demo compare --ledger-dir $ledgerDir --output-dir $ComparisonOutputDir --gds-uri $env:TD_GDS_TEST_URI
        if ($LASTEXITCODE -ne 0) { throw 'GDS score comparison failed' }
    }
}
finally {
    # 무작위 프로젝트 이름에 속한 컨테이너와 익명 볼륨만 제거합니다.
    docker compose -p $project -f $composeFile down -v --timeout 15
    Remove-Item Env:TD_GDS_TEST_JAR -ErrorAction SilentlyContinue
    Remove-Item Env:TD_GDS_TEST_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:TD_GDS_TEST_URI -ErrorAction SilentlyContinue
}
