param([string]$BuildDirectory = "", [switch]$Provision)
$ErrorActionPreference = 'Stop'
$project = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (!$BuildDirectory) { $BuildDirectory = Join-Path $project 'build' }
$sdkRoot = Join-Path $env:USERPROFILE '.pico-sdk'
$cmake = Join-Path $sdkRoot 'cmake/v4.3.4/bin/cmake.exe'
$env:PICO_SDK_PATH = Join-Path $sdkRoot 'sdk/2.3.1'
$env:PICO_TOOLCHAIN_PATH = Join-Path $sdkRoot 'toolchain/15_2_Rel1'
# A copied build cache contains absolute paths. Reconfigure it at the new root.
$configureOptions = @()
$cache = Join-Path $BuildDirectory 'CMakeCache.txt'
if (Test-Path $cache) {
    $sourceLine = Get-Content $cache | Where-Object { $_ -match '^CMAKE_HOME_DIRECTORY:INTERNAL=' } | Select-Object -First 1
    if ($sourceLine -and [IO.Path]::GetFullPath(($sourceLine -split '=', 2)[1]) -ne $project) {
        $configureOptions += '--fresh'
    }
}
& $cmake @configureOptions -S $project -B $BuildDirectory -G Ninja "-DCMAKE_MAKE_PROGRAM=$sdkRoot/ninja/v1.13.2/ninja.exe" "-Dpicotool_DIR=$sdkRoot/picotool/2.3.1/picotool" -DCMAKE_BUILD_TYPE=Release
if ($LASTEXITCODE) { throw 'RP2354 configure failed' }
& $cmake --build $BuildDirectory --parallel
if ($LASTEXITCODE) { throw 'RP2354 build failed' }
if ($Provision) {
    # Reuse the Python interpreter selected by CMake, independent of any host venv.
    $pythonLine = Get-Content (Join-Path $BuildDirectory 'CMakeCache.txt') | Where-Object { $_ -match '^_?Python3_EXECUTABLE:[^=]+=' } | Select-Object -First 1
    if (!$pythonLine) { throw 'Configured Python interpreter not found' }
    $python = ($pythonLine -split '=', 2)[1]
    $arguments = @((Join-Path $PSScriptRoot 'rp2354_provision.py'), '--build-dir', $BuildDirectory)
    & $python @arguments
    if ($LASTEXITCODE) { throw 'RP2354 provisioning failed; no success assumed' }
}
