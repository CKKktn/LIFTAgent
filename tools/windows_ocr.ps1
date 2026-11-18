param(
    [Parameter(Mandatory = $true)][string]$InputManifest,
    [Parameter(Mandatory = $true)][string]$OutputPath
)

# Windows PowerShell 5.1 is intentional: PowerShell 7 cannot project these
# Windows Runtime APIs. This script never changes the execution policy.
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSEdition -ne 'Desktop') {
    throw 'Run this script with WindowsPowerShell v1.0 powershell.exe (5.1).'
}
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]

$script:AsTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and
    $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
if ($null -eq $script:AsTask) { throw 'Windows Runtime async bridge is unavailable.' }

function Wait-WinRt($Operation, [Type]$ResultType) {
    $task = $script:AsTask.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    return $task.GetAwaiter().GetResult()
}

$language = New-Object Windows.Globalization.Language('zh-Hans-CN')
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if ($null -eq $engine) { throw 'The local zh-Hans-CN Windows OCR language pack is unavailable.' }
$requests = Get-Content -LiteralPath $InputManifest -Raw -Encoding UTF8 | ConvertFrom-Json
$results = New-Object System.Collections.Generic.List[object]

foreach ($request in $requests) {
    $stream = $null
    $bitmap = $null
    try {
        $fullPath = [System.IO.Path]::GetFullPath([string]$request.path)
        $file = Wait-WinRt ([Windows.Storage.StorageFile]::GetFileFromPathAsync($fullPath)) ([Windows.Storage.StorageFile])
        $stream = Wait-WinRt ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Wait-WinRt ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Wait-WinRt ($decoder.GetSoftwareBitmapAsync(
            [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
            [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied)) ([Windows.Graphics.Imaging.SoftwareBitmap])
        if ($bitmap.PixelWidth -gt [Windows.Media.Ocr.OcrEngine]::MaxImageDimension -or
            $bitmap.PixelHeight -gt [Windows.Media.Ocr.OcrEngine]::MaxImageDimension) {
            throw 'Image exceeds the local OCR engine dimension limit.'
        }
        $recognition = Wait-WinRt ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $lines = New-Object System.Collections.Generic.List[object]
        foreach ($line in $recognition.Lines) {
            $words = New-Object System.Collections.Generic.List[object]
            foreach ($word in $line.Words) {
                $rect = $word.BoundingRect
                $words.Add([ordered]@{
                    text = [string]$word.Text
                    bounding_box = @([double]$rect.X, [double]$rect.Y, [double]$rect.Width, [double]$rect.Height)
                })
            }
            $lines.Add([ordered]@{text = [string]$line.Text; words = @($words.ToArray())})
        }
        $results.Add([ordered]@{
            id = [string]$request.id
            status = 'ok'
            text = [string]$recognition.Text
            width = [int]$bitmap.PixelWidth
            height = [int]$bitmap.PixelHeight
            lines = @($lines.ToArray())
        })
    }
    catch {
        $results.Add([ordered]@{
            id = [string]$request.id
            status = 'error'
            error = ('Local OCR failed (' + $_.Exception.GetType().Name + ').')
        })
    }
    finally {
        if ($null -ne $bitmap) { $bitmap.Dispose() }
        if ($null -ne $stream) { $stream.Dispose() }
    }
}
$payload = [ordered]@{engine = 'Windows.Media.Ocr zh-Hans-CN'; results = @($results.ToArray())}
$json = $payload | ConvertTo-Json -Depth 16
[System.IO.File]::WriteAllText([System.IO.Path]::GetFullPath($OutputPath), $json, (New-Object System.Text.UTF8Encoding($false)))
# Do not print recognized text or file names to the terminal.
Write-Output ('OCR requests: ' + $results.Count)
