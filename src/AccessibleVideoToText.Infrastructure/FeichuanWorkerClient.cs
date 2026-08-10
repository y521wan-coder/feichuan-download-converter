using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text;
using System.Text.Json;
using AccessibleVideoToText.Core;

namespace AccessibleVideoToText.Infrastructure;

public sealed record WorkerLaunchOptions(
    string ExecutablePath,
    IReadOnlyList<string>? Arguments = null,
    string? WorkingDirectory = null,
    IReadOnlyDictionary<string, string>? Environment = null);

public sealed class FeichuanWorkerClient : IAsyncDisposable
{
    private readonly WorkerLaunchOptions options;
    private readonly ConcurrentDictionary<string, TaskCompletionSource<WorkerMessage>> pending = new();
    private readonly SemaphoreSlim writeGate = new(1, 1);
    private readonly SemaphoreSlim cancellationGate = new(1, 1);
    private readonly CancellationTokenSource lifetimeCancellation = new();
    private Process? process;
    private Task? stdoutTask;
    private Task? stderrTask;
    private Task? exitTask;
    private bool disposing;

    public FeichuanWorkerClient(WorkerLaunchOptions options)
    {
        ArgumentNullException.ThrowIfNull(options);
        ArgumentException.ThrowIfNullOrWhiteSpace(options.ExecutablePath);
        this.options = options;
    }

    public event EventHandler<WorkerMessage>? EventReceived;

    public event EventHandler<string>? DiagnosticReceived;

    public bool IsRunning => process is { HasExited: false };

    public async Task<WorkerMessage> StartAsync(CancellationToken cancellationToken = default)
    {
        if (process is not null)
        {
            throw new InvalidOperationException("工作进程客户端已经启动。");
        }

        var startInfo = new ProcessStartInfo
        {
            FileName = options.ExecutablePath,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardInputEncoding = new UTF8Encoding(false),
            StandardOutputEncoding = new UTF8Encoding(false),
            StandardErrorEncoding = new UTF8Encoding(false),
            WorkingDirectory = string.IsNullOrWhiteSpace(options.WorkingDirectory)
                ? Environment.CurrentDirectory
                : options.WorkingDirectory
        };
        foreach (var argument in options.Arguments ?? [])
        {
            startInfo.ArgumentList.Add(argument);
        }

        foreach (var pair in options.Environment ?? new Dictionary<string, string>())
        {
            startInfo.Environment[pair.Key] = pair.Value;
        }

        process = new Process { StartInfo = startInfo, EnableRaisingEvents = true };
        try
        {
            if (!process.Start())
            {
                throw new WorkerProcessException("无法启动下载工作进程。");
            }
        }
        catch (Exception exception) when (exception is not WorkerProcessException)
        {
            process.Dispose();
            process = null;
            throw new WorkerProcessException("无法启动下载工作进程。", exception);
        }

        stdoutTask = ReadStdoutAsync(process, lifetimeCancellation.Token);
        stderrTask = ReadStderrAsync(process, lifetimeCancellation.Token);
        exitTask = WatchExitAsync(process);
        var hello = await SendAsync("hello", new { }, cancellationToken).ConfigureAwait(false);
        if (hello.Type != "hello.result")
        {
            throw new WorkerProtocolException("invalid_handshake", "下载工作进程握手失败。");
        }

        if (!hello.Payload.TryGetProperty("worker_version", out var workerVersion) ||
            string.IsNullOrWhiteSpace(workerVersion.GetString()))
        {
            throw new WorkerProtocolException("invalid_handshake", "下载工作进程没有返回版本。");
        }

        return hello;
    }

    public async Task<WorkerMessage> SendAsync<TPayload>(
        string messageType,
        TPayload payload,
        CancellationToken cancellationToken = default)
    {
        var activeProcess = process;
        if (activeProcess is null || activeProcess.HasExited)
        {
            throw new WorkerProcessException("下载工作进程没有运行。");
        }

        var requestId = Guid.NewGuid().ToString("N");
        var line = WorkerProtocol.CreateRequest(requestId, messageType, payload);
        var completion = new TaskCompletionSource<WorkerMessage>(
            TaskCreationOptions.RunContinuationsAsynchronously);
        if (!pending.TryAdd(requestId, completion))
        {
            throw new InvalidOperationException("无法登记工作进程请求。");
        }

        using var registration = cancellationToken.Register(() =>
        {
            if (pending.TryRemove(requestId, out var cancelled))
            {
                cancelled.TrySetCanceled(cancellationToken);
            }
        });
        try
        {
            await writeGate.WaitAsync(cancellationToken).ConfigureAwait(false);
            try
            {
                await activeProcess.StandardInput.WriteLineAsync(
                    line.AsMemory(),
                    cancellationToken).ConfigureAwait(false);
                await activeProcess.StandardInput.FlushAsync(cancellationToken).ConfigureAwait(false);
            }
            finally
            {
                writeGate.Release();
            }

            var response = await completion.Task.ConfigureAwait(false);
            if (response.Type == "error")
            {
                var code = response.Payload.TryGetProperty("code", out var codeElement)
                    ? codeElement.GetString() ?? "worker_error"
                    : "worker_error";
                var message = response.Payload.TryGetProperty("message", out var messageElement)
                    ? messageElement.GetString() ?? "下载工作进程执行失败。"
                    : "下载工作进程执行失败。";
                throw new WorkerRemoteException(code, message);
            }

            return response;
        }
        catch
        {
            pending.TryRemove(requestId, out _);
            throw;
        }
    }

    public async Task CancelOrTerminateAsync(TimeSpan responseTimeout)
    {
        if (!IsRunning)
        {
            return;
        }

        await cancellationGate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (!IsRunning)
            {
                return;
            }

            using var timeout = new CancellationTokenSource(responseTimeout);
            var response = await SendAsync("cancel", new { }, timeout.Token).ConfigureAwait(false);
            if (response.Type != "cancel.result")
            {
                KillProcessTree();
                return;
            }

            while (IsRunning)
            {
                var status = await SendAsync("task.status", new { }, timeout.Token).ConfigureAwait(false);
                var busy = status.Payload.TryGetProperty("busy", out var busyElement) &&
                    busyElement.GetBoolean();
                if (!busy)
                {
                    return;
                }

                await Task.Delay(TimeSpan.FromMilliseconds(100), timeout.Token).ConfigureAwait(false);
            }
        }
        catch (Exception exception) when (
            exception is OperationCanceledException or WorkerProcessException or WorkerRemoteException or IOException)
        {
            // The cooperative path failed. The process tree is terminated below.
            KillProcessTree();
        }
        finally
        {
            cancellationGate.Release();
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (disposing)
        {
            return;
        }

        disposing = true;
        var activeProcess = process;
        if (activeProcess is not null && !activeProcess.HasExited)
        {
            using var shutdownTimeout = new CancellationTokenSource(TimeSpan.FromSeconds(2));
            try
            {
                await SendAsync("shutdown", new { }, shutdownTimeout.Token).ConfigureAwait(false);
                await activeProcess.WaitForExitAsync(shutdownTimeout.Token).ConfigureAwait(false);
            }
            catch (Exception exception) when (
                exception is OperationCanceledException or WorkerRemoteException or WorkerProcessException or IOException)
            {
                KillProcessTree();
            }
        }

        lifetimeCancellation.Cancel();
        await IgnoreCancellationAsync(stdoutTask).ConfigureAwait(false);
        await IgnoreCancellationAsync(stderrTask).ConfigureAwait(false);
        await IgnoreCancellationAsync(exitTask).ConfigureAwait(false);
        FailPending(new ObjectDisposedException(nameof(FeichuanWorkerClient)));
        activeProcess?.Dispose();
        process = null;
        lifetimeCancellation.Dispose();
        writeGate.Dispose();
        cancellationGate.Dispose();
    }

    private async Task ReadStdoutAsync(Process activeProcess, CancellationToken cancellationToken)
    {
        try
        {
            while (true)
            {
                var line = await activeProcess.StandardOutput.ReadLineAsync(cancellationToken)
                    .ConfigureAwait(false);
                if (line is null)
                {
                    return;
                }

                WorkerMessage message;
                try
                {
                    message = WorkerProtocol.ParseMessage(line);
                }
                catch (Exception exception)
                {
                    FailPending(exception);
                    KillProcessTree();
                    return;
                }

                if (pending.TryRemove(message.Id, out var completion))
                {
                    completion.TrySetResult(message);
                }
                else
                {
                    EventReceived?.Invoke(this, message);
                }
            }
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
        }
        catch (Exception exception)
        {
            FailPending(new WorkerProcessException("读取下载工作进程输出失败。", exception));
        }
    }

    private async Task ReadStderrAsync(Process activeProcess, CancellationToken cancellationToken)
    {
        try
        {
            while (true)
            {
                var line = await activeProcess.StandardError.ReadLineAsync(cancellationToken)
                    .ConfigureAwait(false);
                if (line is null)
                {
                    return;
                }

                var safeLine = WorkerProtocol.SanitizeDiagnostic(line);
                if (!string.IsNullOrWhiteSpace(safeLine))
                {
                    DiagnosticReceived?.Invoke(this, safeLine);
                }
            }
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
        }
        catch (Exception exception)
        {
            DiagnosticReceived?.Invoke(
                this,
                WorkerProtocol.SanitizeDiagnostic(exception.Message));
        }
    }

    private async Task WatchExitAsync(Process activeProcess)
    {
        try
        {
            await activeProcess.WaitForExitAsync().ConfigureAwait(false);
            if (!disposing)
            {
                FailPending(new WorkerProcessException(
                    $"下载工作进程异常退出（代码 {activeProcess.ExitCode}），请重新扫描。"));
            }
        }
        catch (Exception exception) when (!disposing)
        {
            FailPending(new WorkerProcessException("等待下载工作进程退出时失败。", exception));
        }
    }

    private void KillProcessTree()
    {
        var activeProcess = process;
        if (activeProcess is null)
        {
            return;
        }

        try
        {
            if (!activeProcess.HasExited)
            {
                activeProcess.Kill(entireProcessTree: true);
            }
        }
        catch (InvalidOperationException)
        {
        }
        catch (System.ComponentModel.Win32Exception exception)
        {
            DiagnosticReceived?.Invoke(this, WorkerProtocol.SanitizeDiagnostic(exception.Message));
        }
    }

    private void FailPending(Exception exception)
    {
        foreach (var item in pending.ToArray())
        {
            if (pending.TryRemove(item.Key, out var completion))
            {
                completion.TrySetException(exception);
            }
        }
    }

    private static async Task IgnoreCancellationAsync(Task? task)
    {
        if (task is null)
        {
            return;
        }

        try
        {
            await task.ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
        }
        catch
        {
            // Disposal must continue so the process object and redirected handles are released.
        }
    }
}

public sealed class WorkerProcessException : Exception
{
    public WorkerProcessException(string message, Exception? innerException = null)
        : base(message, innerException)
    {
    }
}

public sealed class WorkerRemoteException : Exception
{
    public WorkerRemoteException(string code, string message)
        : base(message)
    {
        Code = code;
    }

    public string Code { get; }
}
