param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("check", "write")]
    [string] $Mode
)

$ErrorActionPreference = "Stop"

function Get-Sha256Hex([string] $Path) {
    $stream = [IO.File]::OpenRead([IO.Path]::GetFullPath($Path))
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace("-", "")
    }
    finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}

$projectHash = Get-Sha256Hex "pyproject.toml"
$lockHash = Get-Sha256Hex "uv.lock"
$expected = "$projectHash`:$lockHash"
$stamp = [IO.Path]::GetFullPath(".venv\.powerquery-sync-state")

if ($Mode -eq "check") {
    if (-not [IO.File]::Exists($stamp)) {
        exit 1
    }
    $actual = [IO.File]::ReadAllText($stamp).Trim()
    if ($actual -ceq $expected) {
        exit 0
    }
    exit 1
}

[IO.File]::WriteAllText(
    $stamp,
    $expected + [Environment]::NewLine,
    [Text.Encoding]::ASCII
)
exit 0
