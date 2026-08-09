using System.ComponentModel;
using System.Runtime.InteropServices;

namespace AccessibleVideoToText.Infrastructure;

public sealed class SystemSleepInhibitor : IDisposable
{
    private const uint EsContinuous = 0x80000000;
    private const uint EsSystemRequired = 0x00000001;
    private bool disposed;

    private SystemSleepInhibitor()
    {
        var result = SetThreadExecutionState(EsContinuous | EsSystemRequired);
        if (result == 0)
        {
            throw new Win32Exception(Marshal.GetLastWin32Error(), "无法阻止系统自动睡眠。 ");
        }
    }

    public static SystemSleepInhibitor Acquire() => new();

    public void Dispose()
    {
        if (disposed)
        {
            return;
        }

        SetThreadExecutionState(EsContinuous);
        disposed = true;
        GC.SuppressFinalize(this);
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint SetThreadExecutionState(uint executionState);
}

