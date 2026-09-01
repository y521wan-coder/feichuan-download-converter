using AccessibleVideoToText.Core;

namespace AccessibleVideoToText.Infrastructure;

public sealed class CloudJobProcessor
{
    private readonly IMediaProbe mediaProbe;
    private readonly IAudioConverter audioConverter;
    private readonly LocalVideoProcessor localVideoProcessor;
    private readonly IAtomicOutputCommitter outputCommitter;
    private readonly ICloudObjectStore objectStore;
    private readonly ICloudTranscriber transcriber;
    private readonly IJobStore jobStore;
    private readonly IUsageLedger usageLedger;
    private readonly LocalDataPaths localPaths;
    private readonly CloudCredentials credentials;
    private readonly TimeProvider timeProvider;
    private readonly Func<TimeSpan, CancellationToken, Task> delay;

    public CloudJobProcessor(
        IMediaProbe mediaProbe,
        IAudioConverter audioConverter,
        LocalVideoProcessor localVideoProcessor,
        IAtomicOutputCommitter outputCommitter,
        ICloudObjectStore objectStore,
        ICloudTranscriber transcriber,
        IJobStore jobStore,
        IUsageLedger usageLedger,
        LocalDataPaths localPaths,
        CloudCredentials credentials,
        TimeProvider? timeProvider = null,
        Func<TimeSpan, CancellationToken, Task>? delay = null)
    {
        this.mediaProbe = mediaProbe ?? throw new ArgumentNullException(nameof(mediaProbe));
        this.audioConverter = audioConverter ?? throw new ArgumentNullException(nameof(audioConverter));
        this.localVideoProcessor = localVideoProcessor ?? throw new ArgumentNullException(nameof(localVideoProcessor));
        this.outputCommitter = outputCommitter ?? throw new ArgumentNullException(nameof(outputCommitter));
        this.objectStore = objectStore ?? throw new ArgumentNullException(nameof(objectStore));
        this.transcriber = transcriber ?? throw new ArgumentNullException(nameof(transcriber));
        this.jobStore = jobStore ?? throw new ArgumentNullException(nameof(jobStore));
        this.usageLedger = usageLedger ?? throw new ArgumentNullException(nameof(usageLedger));
        this.localPaths = localPaths ?? throw new ArgumentNullException(nameof(localPaths));
        this.credentials = credentials ?? throw new ArgumentNullException(nameof(credentials));
        this.timeProvider = timeProvider ?? TimeProvider.System;
        this.delay = delay ?? ((duration, token) => Task.Delay(duration, token));
        localPaths.EnsureDirectories();
    }

    public async Task<CloudProcessingResult> ProcessVideoAsync(
        QueueItem item,
        int bitrateKbps,
        IProgress<CloudPhaseProgress>? progress,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(item);
        var directory = Path.GetDirectoryName(item.SourcePath)
            ?? throw new IOException("无法确定源视频所在目录。 ");
        return await ProcessVideoAsync(
            item,
            bitrateKbps,
            directory,
            progress,
            cancellationToken).ConfigureAwait(false);
    }

    public async Task<CloudProcessingResult> ProcessVideoAsync(
        QueueItem item,
        int bitrateKbps,
        string outputDirectory,
        IProgress<CloudPhaseProgress>? progress,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(item);
        ArgumentException.ThrowIfNullOrWhiteSpace(outputDirectory);
        var reservation = OutputNameAllocator.FindAvailable(
            outputDirectory,
            Path.GetFileNameWithoutExtension(item.SourcePath),
            requireMp3: true,
            requireTxt: true);

        progress?.Report(new CloudPhaseProgress(JobStage.Probing, "正在探测视频"));
        var localProgress = new Progress<int>(percentage =>
            progress?.Report(new CloudPhaseProgress(JobStage.ConvertingMp3, "正在转换最终 MP3", percentage)));
        var video = await localVideoProcessor.ProcessToReservationAsync(
            item,
            bitrateKbps,
            reservation,
            localProgress,
            cancellationToken).ConfigureAwait(false);

        if (video.ProbeResult.ExceedsCloudDurationLimit)
        {
            item.Stage = JobStage.PartiallySucceeded;
            item.StepDetail = "MP3 成功；TXT 因超过腾讯云 5 小时时长限制未生成";
            item.ResultMessage = $"已生成 {Path.GetFileName(video.Mp3Path)}；未提交云识别";
            return new CloudProcessingResult(
                video.Mp3Path,
                null,
                video.ProbeResult.Duration,
                TxtGenerated: false,
                ExceededDurationLimit: true,
                CleanupWarning: null);
        }

        return await ProcessCloudAudioAsync(
            item,
            item.SourcePath,
            reservation.TxtPath!,
            video.Mp3Path,
            video.ProbeResult.Duration,
            progress,
            cancellationToken).ConfigureAwait(false);
    }

    public async Task<CloudProcessingResult> ProcessMp3Async(
        QueueItem item,
        string outputDirectory,
        IProgress<CloudPhaseProgress>? progress,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(item);
        ArgumentException.ThrowIfNullOrWhiteSpace(outputDirectory);
        if (!string.Equals(Path.GetExtension(item.SourcePath), ".mp3", StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException("第一版只有现有 MP3 可以直接转文字。 ");
        }

        progress?.Report(new CloudPhaseProgress(JobStage.Probing, "正在探测现有 MP3"));
        var probe = await mediaProbe.ProbeAsync(item.SourcePath, cancellationToken).ConfigureAwait(false);
        if (!probe.HasAudio)
        {
            throw new InvalidDataException("MP3 没有可解码的音轨或文件已损坏。 ");
        }

        if (probe.ExceedsCloudDurationLimit)
        {
            throw new InvalidDataException("现有 MP3 超过腾讯云 5 小时时长限制，未提交云识别。 ");
        }

        var reservation = OutputNameAllocator.FindAvailable(
            outputDirectory,
            Path.GetFileNameWithoutExtension(item.SourcePath),
            requireMp3: false,
            requireTxt: true);
        return await ProcessCloudAudioAsync(
            item,
            item.SourcePath,
            reservation.TxtPath!,
            null,
            probe.Duration,
            progress,
            cancellationToken).ConfigureAwait(false);
    }

    public async Task<CloudProcessingResult> ResumeAsync(
        CloudRecoveryJob job,
        IProgress<CloudPhaseProgress>? progress,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(job);
        if (timeProvider.GetUtcNow() - job.SubmittedAt > CloudLimits.TaskLifetime)
        {
            await RemovePendingAsync(job.TaskId, cancellationToken).ConfigureAwait(false);
            throw new InvalidDataException("腾讯云任务已超过约 24 小时有效期，无法继续恢复。 ");
        }

        var cloudObject = new CloudObjectReference(job.Bucket, job.Region, job.ObjectKey, null);
        if (job.Stage == JobStage.CleaningUp)
        {
            await objectStore.DeleteAsync(cloudObject, cancellationToken).ConfigureAwait(false);
            await RemovePendingAsync(job.TaskId, cancellationToken).ConfigureAwait(false);
            return new CloudProcessingResult(null, job.OutputPath, job.EstimatedDuration, File.Exists(job.OutputPath), false, null);
        }

        if (job.Stage == JobStage.WritingTxt && File.Exists(job.OutputPath) && new FileInfo(job.OutputPath).Length > 3)
        {
            var warningAfterExistingOutput = await FinishCleanupAsync(job, cloudObject, cancellationToken).ConfigureAwait(false);
            return new CloudProcessingResult(null, job.OutputPath, job.EstimatedDuration, true, false, warningAfterExistingOutput);
        }

        var status = await PollUntilTerminalAsync(job.TaskId, job.SubmittedAt, progress, cancellationToken).ConfigureAwait(false);
        if (status.State != CloudTranscriptionState.Succeeded)
        {
            await CompleteFailedTaskAsync(job, cloudObject, cancellationToken).ConfigureAwait(false);
            throw new InvalidDataException(status.ErrorMessage ?? "腾讯云录音识别失败。 ");
        }

        progress?.Report(new CloudPhaseProgress(JobStage.WritingTxt, "正在写入恢复任务的 TXT"));
        await outputCommitter.WriteUtf8BomTextAsync(job.OutputPath, status.Transcript!, cancellationToken).ConfigureAwait(false);
        await usageLedger.AddSuccessfulDurationAsync(credentials.SecretId, job.EstimatedDuration, cancellationToken).ConfigureAwait(false);
        var warning = await FinishCleanupAsync(job, cloudObject, cancellationToken).ConfigureAwait(false);
        return new CloudProcessingResult(null, job.OutputPath, job.EstimatedDuration, true, false, warning);
    }

    private async Task<CloudProcessingResult> ProcessCloudAudioAsync(
        QueueItem item,
        string sourcePath,
        string txtPath,
        string? mp3Path,
        TimeSpan duration,
        IProgress<CloudPhaseProgress>? progress,
        CancellationToken cancellationToken)
    {
        if (duration <= TimeSpan.Zero)
        {
            throw new InvalidDataException("媒体时长无效，未提交云识别。 ");
        }

        var temporaryAudio = Path.Combine(localPaths.TempDirectory, $"{Guid.NewGuid():N}.upload.mp3");
        CloudObjectReference? cloudObject = null;
        CloudRecoveryJob? recoveryJob = null;
        try
        {
            item.Stage = JobStage.PreparingCloudAudio;
            progress?.Report(new CloudPhaseProgress(JobStage.PreparingCloudAudio, "正在生成 16kHz 单声道临时上传音频", 0));
            await audioConverter.CreateCloudAudioAsync(
                new CloudAudioRequest(sourcePath, temporaryAudio, duration),
                new Progress<int>(percentage => progress?.Report(
                    new CloudPhaseProgress(JobStage.PreparingCloudAudio, "正在准备云端音频", percentage))),
                cancellationToken).ConfigureAwait(false);

            var temporaryInfo = new FileInfo(temporaryAudio);
            if (!temporaryInfo.Exists || temporaryInfo.Length <= 0 || temporaryInfo.Length > CloudLimits.MaximumAudioBytes)
            {
                throw new InvalidDataException("临时上传音频为空或超过腾讯云 1 GB 限制。 ");
            }

            item.Stage = JobStage.Uploading;
            progress?.Report(new CloudPhaseProgress(JobStage.Uploading, "正在上传随机命名的临时音频", 0));
            cloudObject = await RetryAsync(
                token => objectStore.UploadAsync(
                    temporaryAudio,
                    new Progress<int>(percentage => progress?.Report(
                        new CloudPhaseProgress(JobStage.Uploading, "正在上传腾讯云 COS", percentage))),
                    token),
                cancellationToken).ConfigureAwait(false);

            item.Stage = JobStage.SubmittingTranscription;
            progress?.Report(new CloudPhaseProgress(JobStage.SubmittingTranscription, "正在提交常规 16k_zh 录音文件识别"));
            var taskId = await RetryAsync(
                token => transcriber.SubmitAsync(
                    cloudObject.SignedUrl ?? throw new InvalidOperationException("COS 签名地址不可用。"),
                    token),
                cancellationToken).ConfigureAwait(false);

            recoveryJob = new CloudRecoveryJob(
                taskId,
                cloudObject.Bucket,
                cloudObject.Region,
                cloudObject.ObjectKey,
                timeProvider.GetUtcNow(),
                txtPath,
                JobStage.CloudTranscribing,
                duration);
            await AddOrReplacePendingAsync(recoveryJob, CancellationToken.None).ConfigureAwait(false);

            CloudTranscriptionStatus status;
            try
            {
                status = await PollUntilTerminalAsync(taskId, recoveryJob.SubmittedAt, progress, cancellationToken)
                    .ConfigureAwait(false);
            }
            catch (OperationCanceledException exception)
            {
                throw new CloudTaskContinuesException(taskId, exception);
            }

            if (status.State != CloudTranscriptionState.Succeeded)
            {
                await CompleteFailedTaskAsync(recoveryJob, cloudObject, CancellationToken.None).ConfigureAwait(false);
                throw new InvalidDataException(status.ErrorMessage ?? "腾讯云录音识别失败。 ");
            }

            item.Stage = JobStage.WritingTxt;
            progress?.Report(new CloudPhaseProgress(JobStage.WritingTxt, "正在写入纯正文 TXT"));
            recoveryJob = recoveryJob with { Stage = JobStage.WritingTxt };
            await AddOrReplacePendingAsync(recoveryJob, CancellationToken.None).ConfigureAwait(false);
            try
            {
                await outputCommitter.WriteUtf8BomTextAsync(txtPath, status.Transcript!, cancellationToken).ConfigureAwait(false);
                await usageLedger.AddSuccessfulDurationAsync(credentials.SecretId, duration, cancellationToken).ConfigureAwait(false);
            }
            catch
            {
                await AddOrReplacePendingAsync(recoveryJob with { Stage = JobStage.WritingTxt }, CancellationToken.None)
                    .ConfigureAwait(false);
                throw;
            }

            var warning = await FinishCleanupAsync(recoveryJob, cloudObject, CancellationToken.None).ConfigureAwait(false);
            item.Stage = warning is null ? JobStage.Succeeded : JobStage.PartiallySucceeded;
            item.StepDetail = warning is null ? "MP3 和 TXT 处理完成" : "TXT 已完成，云端清理待重试";
            item.ResultMessage = mp3Path is null
                ? $"成功生成 {Path.GetFileName(txtPath)}；原 MP3 未修改"
                : $"成功生成 {Path.GetFileName(mp3Path)} 和 {Path.GetFileName(txtPath)}";
            return new CloudProcessingResult(mp3Path, txtPath, duration, true, false, warning);
        }
        catch (OperationCanceledException exception) when (recoveryJob is not null)
        {
            throw new CloudTaskContinuesException(recoveryJob.TaskId, exception);
        }
        catch
        {
            if (cloudObject is not null && recoveryJob is null)
            {
                try
                {
                    await objectStore.DeleteAsync(cloudObject, CancellationToken.None).ConfigureAwait(false);
                }
                catch
                {
                    // The one-day lifecycle is the final cleanup safety net before ASR submission.
                }
            }

            throw;
        }
        finally
        {
            try
            {
                if (File.Exists(temporaryAudio))
                {
                    File.Delete(temporaryAudio);
                }
            }
            catch (IOException)
            {
                // The app-owned temp directory can be cleaned on a later start.
            }
        }
    }

    private async Task<CloudTranscriptionStatus> PollUntilTerminalAsync(
        string taskId,
        DateTimeOffset submittedAt,
        IProgress<CloudPhaseProgress>? progress,
        CancellationToken cancellationToken)
    {
        while (true)
        {
            cancellationToken.ThrowIfCancellationRequested();
            var elapsed = timeProvider.GetUtcNow() - submittedAt;
            if (elapsed > CloudLimits.TaskLifetime)
            {
                return new CloudTranscriptionStatus(
                    CloudTranscriptionState.Expired,
                    null,
                    "expired",
                    "腾讯云任务已超过约 24 小时有效期。 ");
            }

            progress?.Report(new CloudPhaseProgress(
                JobStage.CloudTranscribing,
                $"腾讯云识别中，已等待 {FormatElapsed(elapsed)}"));
            var status = await RetryAsync(
                token => transcriber.GetStatusAsync(taskId, token),
                cancellationToken).ConfigureAwait(false);
            if (status.State is CloudTranscriptionState.Succeeded or CloudTranscriptionState.Failed or CloudTranscriptionState.Expired)
            {
                return status;
            }

            await delay(TimeSpan.FromSeconds(5), cancellationToken).ConfigureAwait(false);
        }
    }

    private async Task CompleteFailedTaskAsync(
        CloudRecoveryJob job,
        CloudObjectReference cloudObject,
        CancellationToken cancellationToken)
    {
        try
        {
            await objectStore.DeleteAsync(cloudObject, cancellationToken).ConfigureAwait(false);
            await RemovePendingAsync(job.TaskId, CancellationToken.None).ConfigureAwait(false);
        }
        catch
        {
            await AddOrReplacePendingAsync(job with { Stage = JobStage.CleaningUp }, CancellationToken.None)
                .ConfigureAwait(false);
        }
    }

    private async Task<string?> FinishCleanupAsync(
        CloudRecoveryJob job,
        CloudObjectReference cloudObject,
        CancellationToken cancellationToken)
    {
        try
        {
            await objectStore.DeleteAsync(cloudObject, cancellationToken).ConfigureAwait(false);
            await RemovePendingAsync(job.TaskId, CancellationToken.None).ConfigureAwait(false);
            return null;
        }
        catch
        {
            await AddOrReplacePendingAsync(job with { Stage = JobStage.CleaningUp }, CancellationToken.None)
                .ConfigureAwait(false);
            return "TXT 已成功，但 COS 临时对象删除失败；已记录为待重试清理。";
        }
    }

    private async Task AddOrReplacePendingAsync(CloudRecoveryJob job, CancellationToken cancellationToken)
    {
        var jobs = (await jobStore.LoadPendingAsync(cancellationToken).ConfigureAwait(false)).ToList();
        jobs.RemoveAll(candidate => candidate.TaskId == job.TaskId && candidate.SubmittedAt == job.SubmittedAt);
        jobs.Add(job);
        await jobStore.SavePendingAsync(jobs, cancellationToken).ConfigureAwait(false);
    }

    private async Task RemovePendingAsync(string taskId, CancellationToken cancellationToken)
    {
        var jobs = (await jobStore.LoadPendingAsync(cancellationToken).ConfigureAwait(false)).ToList();
        jobs.RemoveAll(candidate => candidate.TaskId == taskId);
        await jobStore.SavePendingAsync(jobs, cancellationToken).ConfigureAwait(false);
    }

    private static Task<T> RetryAsync<T>(
        Func<CancellationToken, Task<T>> operation,
        CancellationToken cancellationToken) =>
        CloudRetryExecutor.ExecuteAsync(operation, TencentCloudErrorClassifier.IsTransient, cancellationToken);

    private static string FormatElapsed(TimeSpan elapsed)
    {
        if (elapsed < TimeSpan.Zero)
        {
            elapsed = TimeSpan.Zero;
        }

        return elapsed.TotalHours >= 1
            ? $"{(int)elapsed.TotalHours} 小时 {elapsed.Minutes} 分钟"
            : $"{elapsed.Minutes} 分 {elapsed.Seconds} 秒";
    }
}
