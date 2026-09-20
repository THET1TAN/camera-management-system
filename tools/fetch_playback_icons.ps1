# Developer-only resource acquisition. Never used when starting the application.
$ErrorActionPreference = 'Stop'
$taskAssetRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../assets/bootstrap-icons'))
$taskSvgRoot = Join-Path $taskAssetRoot 'svg'
New-Item -ItemType Directory -Force -Path $taskSvgRoot | Out-Null
$taskVersion = 'v1.13.1'
$taskBase = "https://raw.githubusercontent.com/twbs/icons/$taskVersion"
$taskIcons = @('film','play-fill','pause-fill','stop-fill','arrow-counterclockwise','arrow-clockwise',
    'chevron-left','chevron-right','calendar3','volume-up','volume-mute','zoom-in','zoom-out',
    'download','scissors','sliders','arrow-repeat','info-circle','funnel','skip-forward-fill',
    'layout-sidebar','x-lg','check2','exclamation-triangle','camera-video','clock')
foreach ($taskIcon in $taskIcons) {
    Invoke-WebRequest -UseBasicParsing -Uri "$taskBase/icons/$taskIcon.svg" -OutFile (Join-Path $taskSvgRoot "$taskIcon.svg")
}
Invoke-WebRequest -UseBasicParsing -Uri "$taskBase/LICENSE" -OutFile (Join-Path $taskAssetRoot 'LICENSE')
[IO.File]::WriteAllText((Join-Path $taskAssetRoot 'VERSION'), "$taskVersion`n")
