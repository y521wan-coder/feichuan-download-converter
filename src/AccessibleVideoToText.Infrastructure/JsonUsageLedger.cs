using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using AccessibleVideoToText.Core;

namespace AccessibleVideoToText.Infrastructure;

public sealed class JsonUsageLedger : IUsageLedger
{
    private static readonly TimeZoneInfo ChinaTimeZone = TimeZoneInfo.FindSystemTimeZoneById("China Standard Time");
    private readonly LocalDataPaths paths;
    private readonly TimeProvider timeProvider;
    private readonly SemaphoreSlim gate = new(1, 1);

    public JsonUsageLedger(LocalDataPaths paths, TimeProvider? timeProvider = null)
    {
        this.paths = paths ?? throw new ArgumentNullException(nameof(paths));
        this.timeProvider = timeProvider ?? TimeProvider.System;
        paths.EnsureDirectories();
    }

    public async Task<TimeSpan> GetCurrentMonthUsageAsync(string secretId, CancellationToken cancellationToken)
    {
        var key = CreateCurrentKey(secretId);
        await gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            var document = await LoadAsync(cancellationToken).ConfigureAwait(false);
            return TimeSpan.FromSeconds(document.UsageSeconds.GetValueOrDefault(key));
        }
        finally
        {
            gate.Release();
        }
    }

    public async Task AddSuccessfulDurationAsync(string secretId, TimeSpan duration, CancellationToken cancellationToken)
    {
        if (duration <= TimeSpan.Zero)
        {
            throw new ArgumentOutOfRangeException(nameof(duration), "识别时长必须大于零。 ");
        }

        var key = CreateCurrentKey(secretId);
        await gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            var document = await LoadAsync(cancellationToken).ConfigureAwait(false);
            document.UsageSeconds[key] = document.UsageSeconds.GetValueOrDefault(key) + duration.TotalSeconds;
            await AtomicPrivateJson.WriteAsync(paths.UsageLedgerFile, document, cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            gate.Release();
        }
    }

    private async Task<UsageLedgerDocument> LoadAsync(CancellationToken cancellationToken) =>
        await AtomicPrivateJson.ReadAsync<UsageLedgerDocument>(paths.UsageLedgerFile, cancellationToken).ConfigureAwait(false)
        ?? new UsageLedgerDocument();

    private string CreateCurrentKey(string secretId)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(secretId);
        var hash = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(secretId)));
        var beijingNow = TimeZoneInfo.ConvertTime(timeProvider.GetUtcNow(), ChinaTimeZone);
        var month = beijingNow.ToString("yyyy-MM", CultureInfo.InvariantCulture);
        return $"{hash}|{month}";
    }

    private sealed class UsageLedgerDocument
    {
        public Dictionary<string, double> UsageSeconds { get; init; } = new(StringComparer.Ordinal);
    }
}
