$ErrorActionPreference = 'Stop'
$projectDir = Split-Path $PSScriptRoot -Parent
$cacheDir = Join-Path $env:USERPROFILE '.gradle/caches/modules-2/files-2.1'
$javaExe = Join-Path $env:USERPROFILE 'java/17.0.13+11/bin/java.exe'
if ($env:JAVA_HOME -and (Test-Path -LiteralPath "$env:JAVA_HOME/bin/java.exe")) {
    $javaExe = Join-Path $env:JAVA_HOME 'bin/java.exe'
}
if (-not (Test-Path -LiteralPath $javaExe)) { throw 'Set JAVA_HOME to JDK 17.' }
function Find-Jar([string]$pattern) {
    $jar = Get-ChildItem -Path (Join-Path $cacheDir $pattern) -File | Select-Object -First 1
    if (-not $jar) { throw "Missing cached dependency: $pattern. Build the APK first." }
    return $jar.FullName
}
$compiler = Find-Jar 'org.jetbrains.kotlin/kotlin-compiler-embeddable/1.9.24/*/*.jar'
$stdlib = Find-Jar 'org.jetbrains.kotlin/kotlin-stdlib/1.9.24/*/*.jar'
$reflect = Find-Jar 'org.jetbrains.kotlin/kotlin-reflect/1.6.10/*/*.jar'
$trove = Find-Jar 'org.jetbrains.intellij.deps/trove4j/*/*/*.jar'
$annotations = Find-Jar 'org.jetbrains/annotations/*/*/*.jar'
$jsonJar = Find-Jar 'org.json/json/*/*/*.jar'
$compilerPath = @($compiler, $stdlib, $reflect, $trove, $annotations) -join ';'
$runtimePath = @($stdlib, $jsonJar, $annotations) -join ';'
$tempDir = Join-Path ([System.IO.Path]::GetTempPath()) ('campus-policy-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $tempDir | Out-Null
try {
    $policy = Join-Path $projectDir 'template_source/{{cookiecutter.out_dir}}/android/app/src/main/kotlin/{{ cookiecutter.kotlin_dir }}/MonitorPolicy.kt'
    $source = [System.IO.File]::ReadAllText($policy)
    $source = $source.Replace('package {{ cookiecutter.org_name_2 }}.{{ cookiecutter.package_name }}', 'package campus.monitor.tests')
    $rendered = Join-Path $tempDir 'MonitorPolicy.kt'
    [System.IO.File]::WriteAllText($rendered, $source)
    & $javaExe -cp $compilerPath org.jetbrains.kotlin.cli.jvm.K2JVMCompiler -no-stdlib -no-reflect -jvm-target 1.8 -classpath $runtimePath -d "$tempDir/checks.jar" $rendered "$PSScriptRoot/MonitorPolicyChecks.kt"
    if ($LASTEXITCODE -ne 0) { throw 'Kotlin compilation failed.' }
    & $javaExe -cp "$tempDir/checks.jar;$runtimePath" campus.monitor.tests.MonitorPolicyChecksKt
    if ($LASTEXITCODE -ne 0) { throw 'Android policy checks failed.' }
} finally {
    # Only remove the unique directory created by this test run.
    if ((Split-Path $tempDir -Parent) -eq [System.IO.Path]::GetTempPath().TrimEnd('\')) {
        Remove-Item -LiteralPath $tempDir -Recurse -Force
    }
}
