namespace AccessibleVideoToText.Infrastructure;

public static class CloudRetryExecutor
{
    private static readonly TimeSpan[] RetryDelays =
    [
        TimeSpan.FromSeconds(2),
        TimeSpan.FromSeconds(5),
        TimeSpan.FromSeconds(15)
    ];

    public static async Task<T> ExecuteAsync<T>(
        Func<CancellationToken, Task<T>> operation,
        Func<Exception, bool> isTransient,
        CancellationToken cancellationToken,
        Func<TimeSpan, CancellationToken, Task>? delay = null)
    {
        ArgumentNullException.ThrowIfNull(operation);
        ArgumentNullException.ThrowIfNull(isTransient);
        delay ??= static (duration, token) => Task.Delay(duration, token);

        for (var attempt = 0; ; attempt++)
        {
            try
            {
                return await operation(cancellationToken).ConfigureAwait(false);
            }
            catch (Exception exception) when (
                exception is not OperationCanceledException &&
                attempt < RetryDelays.Length &&
                isTransient(exception))
            {
                await delay(RetryDelays[attempt], cancellationToken).ConfigureAwait(false);
            }
        }
    }
}
