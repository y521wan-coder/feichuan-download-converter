[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstalledExe,
    [Parameter(Mandatory = $true)]
    [string]$ShareText,
    [switch]$ConfirmCloudCosts,
    [int]$TimeoutMinutes = 20
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (-not $ConfirmCloudCosts) {
    throw "Real cloud acceptance requires the explicit -ConfirmCloudCosts switch."
}
if (-not (Test-Path -LiteralPath $InstalledExe -PathType Leaf)) {
    throw "Installed executable was not found."
}

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class NativeCombo {
    [DllImport("user32.dll", CharSet=CharSet.Auto)]
    public static extern IntPtr SendMessage(
        IntPtr hWnd,
        int message,
        IntPtr wParam,
        IntPtr lParam);
}
'@

function Get-ProcessWindows([int]$ProcessId) {
    $condition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
        $ProcessId)
    return [System.Windows.Automation.AutomationElement]::RootElement.FindAll(
        [System.Windows.Automation.TreeScope]::Children,
        $condition)
}

function Find-NamedElement($Root, [string]$Name) {
    $condition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::NameProperty,
        $Name)
    return $Root.FindFirst(
        [System.Windows.Automation.TreeScope]::Descendants,
        $condition)
}

function Invoke-NamedButton($Window, [string[]]$Names) {
    foreach ($name in $Names) {
        $button = Find-NamedElement $Window $name
        if ($null -eq $button) {
            continue
        }
        try {
            $button.GetCurrentPattern(
                [System.Windows.Automation.InvokePattern]::Pattern).Invoke()
            return $true
        }
        catch {
            continue
        }
    }
    return $false
}

function Invoke-YesButton($Window) {
    $condition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button)
    foreach ($button in $Window.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            $condition)) {
        $name = $button.Current.Name
        if ($name -like "是*" -or $name -eq "Yes") {
            $button.GetCurrentPattern(
                [System.Windows.Automation.InvokePattern]::Pattern).Invoke()
            return $true
        }
    }
    return $false
}

$process = Start-Process -FilePath $InstalledExe -PassThru
$completed = $false
$failure = $false
$credentialBlocked = $false
$taskStarted = $false
$expenseConfirmed = $false
$limitConfirmed = $false
$batchConfirmed = $false
$main = $null

try {
    $startupDeadline = [DateTime]::UtcNow.AddSeconds(45)
    while ([DateTime]::UtcNow -lt $startupDeadline -and $null -eq $main) {
        Start-Sleep -Milliseconds 250
        foreach ($window in Get-ProcessWindows $process.Id) {
            if ($window.Current.Name -eq "飞船下载转换工具主窗口") {
                $main = $window
            }
        }
    }
    if ($null -eq $main) {
        throw "Installed main window did not appear."
    }

    $input = Find-NamedElement $main "下载链接或平台分享文本"
    $combo = Find-NamedElement $main "处理模式"
    $start = Find-NamedElement $main "开始(B)"
    $cancel = Find-NamedElement $main "取消任务(C)"
    if ($null -in @($input, $combo, $start, $cancel)) {
        $missing = @()
        if ($null -eq $input) { $missing += "input" }
        if ($null -eq $combo) { $missing += "combo" }
        if ($null -eq $start) { $missing += "start" }
        if ($null -eq $cancel) { $missing += "cancel" }
        throw "Installed acceptance controls are incomplete: $($missing -join ', ')."
    }

    $input.GetCurrentPattern(
        [System.Windows.Automation.ValuePattern]::Pattern).SetValue($ShareText)
    [NativeCombo]::SendMessage(
        [IntPtr]$combo.Current.NativeWindowHandle,
        0x014E,
        [IntPtr]2,
        [IntPtr]::Zero) | Out-Null
    Start-Sleep -Milliseconds 250
    if (-not $start.Current.IsEnabled) {
        throw "Start button did not enable after setting the share text."
    }
    $start.GetCurrentPattern(
        [System.Windows.Automation.InvokePattern]::Pattern).Invoke()

    $deadline = [DateTime]::UtcNow.AddMinutes($TimeoutMinutes)
    $inactiveSince = $null
    while ([DateTime]::UtcNow -lt $deadline -and
           -not $completed -and
           -not $credentialBlocked -and
           -not $failure) {
        Start-Sleep -Milliseconds 500
        if ($process.HasExited) {
            $failure = $true
            break
        }

        foreach ($window in Get-ProcessWindows $process.Id) {
            switch ($window.Current.Name) {
                "确认上传腾讯云并可能产生费用" {
                    if (Invoke-YesButton $window) {
                        $expenseConfirmed = $true
                    }
                }
                "已达到本机 10 小时费用保护线" {
                    if (Invoke-YesButton $window) {
                        $limitConfirmed = $true
                    }
                }
                "首次配置腾讯云" {
                    Invoke-NamedButton $window @("取消", "Cancel") | Out-Null
                    $credentialBlocked = $true
                }
                "确认批量下载" {
                    if (Invoke-NamedButton $window @("开始下载", "继续下载")) {
                        $batchConfirmed = $true
                    }
                }
                "确认普通网站下载" {
                    if (Invoke-NamedButton $window @("继续下载")) {
                        $batchConfirmed = $true
                    }
                }
                "确认后续处理" {
                    if (Invoke-YesButton $window) {
                        $batchConfirmed = $true
                    }
                }
            }
        }

        $cancel = Find-NamedElement $main "取消任务(C)"
        if ($null -ne $cancel -and $cancel.Current.IsEnabled) {
            $taskStarted = $true
            $inactiveSince = $null
        }
        elseif ($taskStarted) {
            if ($null -eq $inactiveSince) {
                $inactiveSince = [DateTime]::UtcNow
            }
            $inactiveFor = [DateTime]::UtcNow - $inactiveSince
            if ($expenseConfirmed -and $inactiveFor.TotalSeconds -ge 8) {
                $completed = $true
            }
            elseif (-not $expenseConfirmed -and $inactiveFor.TotalSeconds -ge 60) {
                $failure = $true
            }
        }
    }

    Write-Output "ACCEPTANCE_TASK_STARTED $taskStarted"
    Write-Output "ACCEPTANCE_EXPENSE_CONFIRMED $expenseConfirmed"
    Write-Output "ACCEPTANCE_LIMIT_CONFIRMED $limitConfirmed"
    Write-Output "ACCEPTANCE_CREDENTIAL_BLOCKED $credentialBlocked"
    Write-Output "ACCEPTANCE_BATCH_CONFIRMED $batchConfirmed"
    Write-Output "ACCEPTANCE_COMPLETED $completed"
    Write-Output "ACCEPTANCE_FAILURE $failure"
}
finally {
    if (-not $process.HasExited -and -not $completed) {
        $cancel = if ($null -eq $main) { $null } else {
            Find-NamedElement $main "取消任务(C)"
        }
        if ($null -ne $cancel -and $cancel.Current.IsEnabled) {
            try {
                $cancel.GetCurrentPattern(
                    [System.Windows.Automation.InvokePattern]::Pattern).Invoke()
                Start-Sleep -Seconds 1
                foreach ($window in Get-ProcessWindows $process.Id) {
                    if ($window.Current.Name -eq "选择取消范围") {
                        Invoke-NamedButton $window @("停止整个批次") | Out-Null
                    }
                }
            }
            catch {
                # The final close path below still asks the application to exit.
            }
        }
    }

    if (-not $process.HasExited) {
        $closeDeadline = [DateTime]::UtcNow.AddSeconds(30)
        while ([DateTime]::UtcNow -lt $closeDeadline) {
            $cancel = if ($null -eq $main) { $null } else {
                Find-NamedElement $main "取消任务(C)"
            }
            if ($null -eq $cancel -or -not $cancel.Current.IsEnabled) {
                break
            }
            Start-Sleep -Milliseconds 250
        }
        if ($null -ne $main) {
            try {
                $main.GetCurrentPattern(
                    [System.Windows.Automation.WindowPattern]::Pattern).Close()
            }
            catch {
                # Process cleanup is checked below.
            }
        }
        $process.WaitForExit(20000) | Out-Null
    }

    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id -Force
    }
    Start-Sleep -Seconds 2
    Write-Output "ACCEPTANCE_MAIN_LEFT $(@(
        Get-Process -Name '飞船下载转换工具' -ErrorAction SilentlyContinue).Count)"
    Write-Output "ACCEPTANCE_WORKER_LEFT $(@(
        Get-Process -Name 'feichuan-worker' -ErrorAction SilentlyContinue).Count)"
}

if (-not $completed) {
    exit 2
}
