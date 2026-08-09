using AccessibleVideoToText.Core;

namespace AccessibleVideoToText.Infrastructure;

public sealed class JsonJobStore : IJobStore
{
    private readonly LocalDataPaths paths;
    private readonly SemaphoreSlim gate = new(1, 1);

    public JsonJobStore(LocalDataPaths paths)
    {
        this.paths = paths ?? throw new ArgumentNullException(nameof(paths));
        paths.EnsureDirectories();
    }

    public async Task<IReadOnlyList<CloudRecoveryJob>> LoadPendingAsync(CancellationToken cancellationToken)
    {
        await gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            var jobs = await AtomicPrivateJson.ReadAsync<List<CloudRecoveryJob>>(paths.JobsFile, cancellationToken)
                .ConfigureAwait(false);
            return jobs is null
                ? Array.Empty<CloudRecoveryJob>()
                : jobs.AsReadOnly();
        }
        finally
        {
            gate.Release();
        }
    }

    public async Task SavePendingAsync(IReadOnlyCollection<CloudRecoveryJob> jobs, CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(jobs);
        if (jobs.Any(job => job.Stage.IsTerminal()))
        {
            throw new ArgumentException("待恢复任务不能包含已经结束的任务。", nameof(jobs));
        }

        await gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            await AtomicPrivateJson.WriteAsync(paths.JobsFile, jobs.ToArray(), cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            gate.Release();
        }
    }
}
