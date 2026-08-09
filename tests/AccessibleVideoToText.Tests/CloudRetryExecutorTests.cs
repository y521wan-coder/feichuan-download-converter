using AccessibleVideoToText.Infrastructure;

namespace AccessibleVideoToText.Tests;

[TestClass]
public sealed class CloudRetryExecutorTests
{
    [TestMethod]
    public async Task TransientFailures_RetryAfterTwoFiveAndFifteenSeconds()
    {
        var attempts = 0;
        var delays = new List<TimeSpan>();

        var value = await CloudRetryExecutor.ExecuteAsync(
            _ => ++attempts <= 3
                ? Task.FromException<int>(new IOException("temporary"))
                : Task.FromResult(42),
            exception => exception is IOException,
            CancellationToken.None,
            (delay, _) =>
            {
                delays.Add(delay);
                return Task.CompletedTask;
            });

        Assert.AreEqual(42, value);
        CollectionAssert.AreEqual(
            new[] { TimeSpan.FromSeconds(2), TimeSpan.FromSeconds(5), TimeSpan.FromSeconds(15) },
            delays);
    }

    [TestMethod]
    public async Task NonTransientFailure_IsNotRetried()
    {
        var attempts = 0;
        await Assert.ThrowsExactlyAsync<UnauthorizedAccessException>(() =>
            CloudRetryExecutor.ExecuteAsync<int>(
                _ =>
                {
                    attempts++;
                    return Task.FromException<int>(new UnauthorizedAccessException());
                },
                _ => false,
                CancellationToken.None,
                (_, _) => Task.CompletedTask));

        Assert.AreEqual(1, attempts);
    }
}
