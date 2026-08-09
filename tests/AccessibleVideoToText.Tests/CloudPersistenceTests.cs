using AccessibleVideoToText.Core;
using AccessibleVideoToText.Infrastructure;

namespace AccessibleVideoToText.Tests;

[TestClass]
public sealed class CloudPersistenceTests
{
    [TestMethod]
    public async Task JobStore_RoundTripsPendingJobAndRejectsTerminalState()
    {
        var directory = CreateTemporaryDirectory();
        try
        {
            var store = new JsonJobStore(new LocalDataPaths(directory));
            var job = new CloudRecoveryJob(
                "123456",
                "accessible-video-to-text-1250000000",
                "ap-shanghai",
                "accessible-video-to-text/00000000000000000000000000000000.mp3",
                DateTimeOffset.Parse("2026-08-09T01:00:00+08:00"),
                Path.Combine(directory, "result.txt"),
                JobStage.Recoverable);

            await store.SavePendingAsync([job], CancellationToken.None);
            var restored = await store.LoadPendingAsync(CancellationToken.None);

            Assert.HasCount(1, restored);
            Assert.AreEqual(job, restored[0]);
            await Assert.ThrowsExactlyAsync<ArgumentException>(() =>
                store.SavePendingAsync([job with { Stage = JobStage.Succeeded }], CancellationToken.None));
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
        }
    }

    [TestMethod]
    public async Task UsageLedger_HashesSecretIdAndResetsAtBeijingMonthBoundary()
    {
        var directory = CreateTemporaryDirectory();
        try
        {
            var clock = new MutableTimeProvider(new DateTimeOffset(2026, 7, 31, 15, 59, 0, TimeSpan.Zero));
            var paths = new LocalDataPaths(directory);
            var ledger = new JsonUsageLedger(paths, clock);
            const string credentialIdentifier = "test-credential-identifier-not-real";

            await ledger.AddSuccessfulDurationAsync(credentialIdentifier, TimeSpan.FromMinutes(90), CancellationToken.None);
            Assert.AreEqual(TimeSpan.FromMinutes(90), await ledger.GetCurrentMonthUsageAsync(credentialIdentifier, CancellationToken.None));
            Assert.IsFalse((await File.ReadAllTextAsync(paths.UsageLedgerFile)).Contains(credentialIdentifier, StringComparison.Ordinal));

            clock.UtcNow = new DateTimeOffset(2026, 7, 31, 16, 1, 0, TimeSpan.Zero);
            Assert.AreEqual(TimeSpan.Zero, await ledger.GetCurrentMonthUsageAsync(credentialIdentifier, CancellationToken.None));
        }
        finally
        {
            Directory.Delete(directory, recursive: true);
        }
    }

    private static string CreateTemporaryDirectory()
    {
        var path = Path.Combine(Path.GetTempPath(), $"AccessibleVideoToText.Tests.{Guid.NewGuid():N}");
        Directory.CreateDirectory(path);
        return path;
    }

    private sealed class MutableTimeProvider(DateTimeOffset utcNow) : TimeProvider
    {
        public DateTimeOffset UtcNow { get; set; } = utcNow;

        public override DateTimeOffset GetUtcNow() => UtcNow;
    }
}
