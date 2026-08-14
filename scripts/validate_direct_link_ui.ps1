[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Executable,
    [string]$ShareText = "",
    [ValidateSet("StartButton", "ModeEnter")]
    [string]$Trigger = "StartButton"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw "Application executable was not found."
}

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class DirectLinkUiNative
{
    [DllImport("user32.dll", CharSet=CharSet.Auto)]
    public static extern IntPtr SendMessage(
        IntPtr hWnd,
        int message,
        IntPtr wParam,
        IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern UInt32 GetClipboardSequenceNumber();
}
'@

function Find-NamedElement($Root, [string]$Name) {
    $condition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty,
        $Name)
    return $Root.FindFirst(
        [System.Windows.Automation.TreeScope]::Descendants,
        $condition)
}

$process = Start-Process -FilePath $Executable -PassThru
try {
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    $main = $null
    while ([DateTime]::UtcNow -lt $deadline -and $null -eq $main) {
        Start-Sleep -Milliseconds 250
        $condition = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
            $process.Id)
        $windows = [System.Windows.Automation.AutomationElement]::RootElement.FindAll(
            [System.Windows.Automation.TreeScope]::Children,
            $condition)
        foreach ($window in $windows) {
            if ($window.Current.Name -eq "飞船下载转换工具主窗口") {
                $main = $window
                break
            }
        }
    }
    if ($null -eq $main) {
        throw "Main window did not appear."
    }

    $input = Find-NamedElement $main "下载链接或平台分享文本"
    $combo = Find-NamedElement $main "处理模式"
    $noteContent = Find-NamedElement $main "图文下载内容"
    $tabs = Find-NamedElement $main "任务与状态页签"
    $start = Find-NamedElement $main "开始(B)"
    $cancel = Find-NamedElement $main "取消任务(C)"
    if ($null -in @($input, $combo, $noteContent, $tabs, $start, $cancel)) {
        throw "Required direct-link controls are missing."
    }
    $isLiveAcceptance = -not [string]::IsNullOrWhiteSpace($ShareText)

    $focusDeadline = [DateTime]::UtcNow.AddSeconds(5)
    $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
    while ([DateTime]::UtcNow -lt $focusDeadline -and
           $focused.Current.Name -ne "下载链接或平台分享文本") {
        Start-Sleep -Milliseconds 100
        $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
    }
    $startupFocusObserved = $focused.Current.Name -eq "下载链接或平台分享文本"
    $input.SetFocus()
    Start-Sleep -Milliseconds 100
    $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
    if ($focused.Current.Name -ne "下载链接或平台分享文本" -and
        -not $isLiveAcceptance) {
        throw "UI Automation could not focus the link input."
    }
    [System.Windows.Forms.SendKeys]::SendWait("{TAB}")
    Start-Sleep -Milliseconds 250
    $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
    if ($focused.Current.Name -ne "处理模式") {
        if (-not $isLiveAcceptance) {
            throw "One Tab from the link input did not reach the processing mode."
        }
        $combo.SetFocus()
    }

    $handle = [IntPtr]$combo.Current.NativeWindowHandle
    $count = [DirectLinkUiNative]::SendMessage(
        $handle,
        0x0146,
        [IntPtr]::Zero,
        [IntPtr]::Zero).ToInt64()
    if ($count -ne 4) {
        throw "Processing mode count is $count rather than 4."
    }
    $combo.SetFocus()
    Start-Sleep -Milliseconds 100
    [System.Windows.Forms.SendKeys]::SendWait("{TAB}")
    Start-Sleep -Milliseconds 250
    $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
    if ($focused.Current.Name -ne "图文下载内容") {
        $combo.SetFocus()
        Start-Sleep -Milliseconds 100
        [System.Windows.Forms.SendKeys]::SendWait("{TAB}")
        Start-Sleep -Milliseconds 250
        $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
        if ($focused.Current.Name -ne "图文下载内容") {
            throw "Tab from processing mode did not reach note content."
        }
    }
    [System.Windows.Forms.SendKeys]::SendWait("{TAB}")
    Start-Sleep -Milliseconds 250
    $focused = [System.Windows.Automation.AutomationElement]::FocusedElement
    if ($focused.Current.Name -ne "任务与状态页签") {
        throw "Tab from note content did not reach task tabs."
    }
    $noteHandle = [IntPtr]$noteContent.Current.NativeWindowHandle
    $noteCount = [DirectLinkUiNative]::SendMessage(
        $noteHandle,
        0x0146,
        [IntPtr]::Zero,
        [IntPtr]::Zero).ToInt64()
    if ($noteCount -ne 2) {
        throw "Note content count is $noteCount rather than 2."
    }

    $testInput = if ($isLiveAcceptance) {
        $ShareText
    }
    else {
        "https://www.youtube.com/playlist?list=PL_OFFLINE_UIA"
    }
    $input.GetCurrentPattern(
        [System.Windows.Automation.ValuePattern]::Pattern).SetValue($testInput)
    $clipboardBefore = [DirectLinkUiNative]::GetClipboardSequenceNumber()
    [DirectLinkUiNative]::SendMessage(
        $handle,
        0x014E,
        [IntPtr]3,
        [IntPtr]::Zero) | Out-Null
    $selected = [DirectLinkUiNative]::SendMessage(
        $handle,
        0x0147,
        [IntPtr]::Zero,
        [IntPtr]::Zero).ToInt64()
    if ($selected -ne 3) {
        throw "Could not select the direct-link mode."
    }
    [DirectLinkUiNative]::SendMessage(
        $noteHandle,
        0x014E,
        [IntPtr]1,
        [IntPtr]::Zero) | Out-Null
    if ($Trigger -eq "ModeEnter") {
        $combo.SetFocus()
        Start-Sleep -Milliseconds 100
        [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
    }
    else {
        $start.GetCurrentPattern(
            [System.Windows.Automation.InvokePattern]::Pattern).Invoke()
    }

    $taskDeadline = if ($isLiveAcceptance) {
        [DateTime]::UtcNow.AddMinutes(3)
    }
    else {
        [DateTime]::UtcNow.AddSeconds(20)
    }
    $completedSafely = $false
    while ([DateTime]::UtcNow -lt $taskDeadline) {
        Start-Sleep -Milliseconds 200
        $selected = [DirectLinkUiNative]::SendMessage(
            $handle,
            0x0147,
            [IntPtr]::Zero,
            [IntPtr]::Zero).ToInt64()
        $expectedResult = if ($isLiveAcceptance) {
            [DirectLinkUiNative]::GetClipboardSequenceNumber() -ne $clipboardBefore
        }
        else {
            $selected -eq 0
        }
        if ($selected -eq 0 -and -not $cancel.Current.IsEnabled -and $expectedResult) {
            $noteSelected = [DirectLinkUiNative]::SendMessage(
                $noteHandle,
                0x0147,
                [IntPtr]::Zero,
                [IntPtr]::Zero).ToInt64()
            if ($noteSelected -ne 0) {
                throw "Note content did not reset after the task ended."
            }
            $completedSafely = $true
            break
        }
    }
    if (-not $completedSafely) {
        throw (
            "Direct-link route did not finish safely; selected=$selected")
    }
    if ($cancel.Current.IsEnabled) {
        throw "Cancel remained enabled after the direct-link task ended."
    }
    if ($isLiveAcceptance) {
        $clipboardAfter = [DirectLinkUiNative]::GetClipboardSequenceNumber()
        if ($clipboardAfter -eq $clipboardBefore) {
            throw "The application reported success without updating the clipboard."
        }
        $direct = [System.Windows.Forms.Clipboard]::GetText()
        try {
            $uri = $null
            if (-not [Uri]::TryCreate($direct, [UriKind]::Absolute, [ref]$uri) -or
                $uri.Scheme -notin @("http", "https") -or
                [string]::IsNullOrWhiteSpace($uri.Host)) {
                throw "The clipboard does not contain one valid HTTP direct link."
            }
            Add-Type -AssemblyName System.Net.Http
            $handler = [Net.Http.HttpClientHandler]::new()
            $handler.AllowAutoRedirect = $true
            $client = [Net.Http.HttpClient]::new($handler)
            $client.Timeout = [TimeSpan]::FromSeconds(30)
            $request = [Net.Http.HttpRequestMessage]::new(
                [Net.Http.HttpMethod]::Get,
                $uri)
            $request.Headers.Range = [Net.Http.Headers.RangeHeaderValue]::new(0, 0)
            $request.Headers.UserAgent.ParseAdd("Mozilla/5.0")
            $response = $null
            try {
                $response = $client.SendAsync(
                    $request,
                    [Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
                if (-not $response.IsSuccessStatusCode) {
                    throw "The copied direct link did not return a successful HTTP status."
                }
                $statusCode = [int]$response.StatusCode
                $contentType = if ($null -eq $response.Content.Headers.ContentType) {
                    "unknown"
                }
                else {
                    $response.Content.Headers.ContentType.MediaType
                }
            }
            catch {
                throw "The copied direct link failed the in-memory network probe."
            }
            finally {
                if ($null -ne $response) { $response.Dispose() }
                $request.Dispose()
                $client.Dispose()
                $handler.Dispose()
            }
            Write-Output (
                "Direct-link live acceptance passed; " +
                "http_status=$statusCode; content_type=$contentType; " +
                "trigger=$Trigger; startup_focus_observed=$startupFocusObserved.")
        }
        finally {
            $direct = $null
            $uri = $null
        }
    }
    else {
        Write-Output (
            "Direct-link installed UIA passed; trigger=$Trigger; " +
            "startup_focus_observed=$startupFocusObserved.")
    }
}
finally {
    if (-not $process.HasExited) {
        $null = $process.CloseMainWindow()
        if (-not $process.WaitForExit(10000)) {
            $process.Kill($true)
            $process.WaitForExit()
        }
    }
}
