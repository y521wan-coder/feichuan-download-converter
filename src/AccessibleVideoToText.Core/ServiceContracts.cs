namespace AccessibleVideoToText.Core;

public interface IMediaProbe
{
    Task<MediaProbeResult> ProbeAsync(string sourcePath, CancellationToken cancellationToken);
}

public interface IAudioConverter
{
    Task ConvertFinalMp3Async(
        AudioConversionRequest request,
        IProgress<int>? progress,
        CancellationToken cancellationToken);

    Task CreateCloudAudioAsync(
        CloudAudioRequest request,
        IProgress<int>? progress,
        CancellationToken cancellationToken);
}

public interface ICloudObjectStore
{
    Task<string> EnsureReadyAsync(CancellationToken cancellationToken);

    Task<CloudObjectReference> UploadAsync(
        string localPath,
        IProgress<int>? progress,
        CancellationToken cancellationToken);

    Task DeleteAsync(CloudObjectReference cloudObject, CancellationToken cancellationToken);
}

public interface ICloudTranscriber
{
    Task<string> SubmitAsync(
        Uri signedAudioUrl,
        CancellationToken cancellationToken);

    Task<CloudTranscriptionStatus> GetStatusAsync(
        string taskId,
        CancellationToken cancellationToken);
}

public interface IJobStore
{
    Task<IReadOnlyList<CloudRecoveryJob>> LoadPendingAsync(CancellationToken cancellationToken);

    Task SavePendingAsync(IReadOnlyCollection<CloudRecoveryJob> jobs, CancellationToken cancellationToken);
}

public interface ISettingsStore
{
    Task<AppSettings> LoadAsync(CancellationToken cancellationToken);

    Task SaveAsync(AppSettings settings, CancellationToken cancellationToken);

    Task SaveCredentialsAsync(CloudCredentials credentials, CancellationToken cancellationToken);

    Task<CloudCredentials?> LoadCredentialsAsync(CancellationToken cancellationToken);
}

public interface IUsageLedger
{
    Task<TimeSpan> GetCurrentMonthUsageAsync(string secretId, CancellationToken cancellationToken);

    Task AddSuccessfulDurationAsync(string secretId, TimeSpan duration, CancellationToken cancellationToken);
}

public interface IAccessibleStatusReporter
{
    void ReportStage(string message);

    void ReportProgress(string message, int percentage);

    void ReportActionRequired(string message);
}

public sealed record CloudObjectReference(string Bucket, string Region, string ObjectKey, Uri? SignedUrl);

public sealed record CloudTranscriptionStatus(
    CloudTranscriptionState State,
    string? Transcript,
    string? ErrorCode,
    string? ErrorMessage);

public sealed record CloudPhaseProgress(JobStage Stage, string Message, int? Percentage = null);

public sealed record CloudProcessingResult(
    string? Mp3Path,
    string? TxtPath,
    TimeSpan Duration,
    bool TxtGenerated,
    bool ExceededDurationLimit,
    string? CleanupWarning);

public sealed class CloudTaskContinuesException : OperationCanceledException
{
    public CloudTaskContinuesException(string taskId, Exception? innerException = null)
        : base("本地跟踪已停止，但已提交的腾讯云任务仍可能继续并占用额度。", innerException)
    {
        TaskId = taskId;
    }

    public string TaskId { get; }
}

public static class CloudLimits
{
    public static readonly TimeSpan MaximumAudioDuration = TimeSpan.FromHours(5);

    public const long MaximumAudioBytes = 1024L * 1024 * 1024;

    public static readonly TimeSpan TaskLifetime = TimeSpan.FromHours(24);

    public static readonly TimeSpan MonthlyFreeAllowance = TimeSpan.FromHours(10);
}

public enum CloudTranscriptionState
{
    Waiting,
    Running,
    Succeeded,
    Failed,
    Expired
}

public sealed record CloudRecoveryJob(
    string TaskId,
    string Bucket,
    string Region,
    string ObjectKey,
    DateTimeOffset SubmittedAt,
    string OutputPath,
    JobStage Stage,
    TimeSpan EstimatedDuration = default);

public sealed record AppSettings(
    int Mp3BitrateKbps = 192,
    string? CosBucket = null,
    string CosRegion = "ap-shanghai",
    string OutputPreference = OutputDirectoryPreference.SourceDirectory,
    bool HasShownVersion1Help = false,
    string? CustomOutputDirectory = null);

public sealed class CloudCredentials
{
    public CloudCredentials(string appId, string secretId, string secretKey)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(appId);
        ArgumentException.ThrowIfNullOrWhiteSpace(secretId);
        ArgumentException.ThrowIfNullOrWhiteSpace(secretKey);
        AppId = appId;
        SecretId = secretId;
        SecretKey = secretKey;
    }

    public string AppId { get; }

    public string SecretId { get; }

    public string SecretKey { get; }

    public override string ToString() => "[受保护的腾讯云凭据]";
}
