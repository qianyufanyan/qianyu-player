# 常驻桥接：把 Windows 媒体会话（SMTC）里酷狗的播放状态按行输出 JSON
# 由 lyric_display.py 以无窗口方式启动，每 500ms 输出一行
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$mgrType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime]
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]

function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}

$mpType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties, Windows.Media.Control, ContentType = WindowsRuntime]

while ($true) {
    $line = '{"status":"none"}'
    try {
        $mgr = Await ($mgrType::RequestAsync()) ($mgrType)
        foreach ($s in $mgr.GetSessions()) {
            $app = $s.SourceAppUserModelId
            if ($app -like '*Kugou*' -or $app -like '*KuGou*' -or $app -like '*kugou*') {
                $info = $s.GetPlaybackInfo()
                $tl = $s.GetTimelineProperties()
                $title = ''
                $artist = ''
                try {
                    $props = Await ($s.TryGetMediaPropertiesAsync()) ($mpType)
                    $title = [string]$props.Title
                    $artist = [string]$props.Artist
                } catch { }
                $obj = [ordered]@{
                    status = [string]$info.PlaybackStatus
                    title  = $title
                    artist = $artist
                    pos    = [math]::Round($tl.Position.TotalSeconds, 3)
                    dur    = [math]::Round($tl.EndTime.TotalSeconds, 3)
                }
                $json = ($obj | ConvertTo-Json -Compress)
                # 统一转成 \uXXXX 转义，避免 stdout 代码页把中文写坏
                $line = [regex]::Replace($json, '[^\x00-\x7F]', { param($m) '\u{0:x4}' -f [int][char]$m.Value })
                break
            }
        }
    } catch {
        $line = '{"status":"error"}'
    }
    [Console]::Out.WriteLine($line)
    [Console]::Out.Flush()
    Start-Sleep -Milliseconds 500
}
