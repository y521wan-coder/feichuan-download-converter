namespace AccessibleVideoToText.Core;

public interface IAtomicOutputCommitter
{
    void CommitOwnedPart(string partPath, string finalPath);

    void DeleteOwnedPartIfPresent(string partPath);

    Task WriteUtf8BomTextAsync(string finalPath, string text, CancellationToken cancellationToken);
}

public sealed record VideoProcessingResult(string Mp3Path, MediaProbeResult ProbeResult);

public sealed class LocalVideoProcessor
{
    private static readonly HashSet<int> AllowedBitrates = [128, 192, 256, 320];

    private readonly IMediaProbe mediaProbe;
    private readonly IAudioConverter audioConverter;
    private readonly IAtomicOutputCommitter outputCommitter;

    public LocalVideoProcessor(
        IMediaProbe mediaProbe,
        IAudioConverter audioConverter,
        IAtomicOutputCommitter outputCommitter)
    {
        this.mediaProbe = mediaProbe ?? throw new ArgumentNullException(nameof(mediaProbe));
        this.audioConverter = audioConverter ?? throw new ArgumentNullException(nameof(audioConverter));
        this.outputCommitter = outputCommitter ?? throw new ArgumentNullException(nameof(outputCommitter));
    }

    public async Task<VideoProcessingResult> ProcessAsync(
        QueueItem item,
        int bitrateKbps,
        IProgress<int>? progress,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(item);
        var sourceDirectory = Path.GetDirectoryName(item.SourcePath)
            ?? throw new IOException("无法确定源文件所在目录。 ");
        var sourceStem = Path.GetFileNameWithoutExtension(item.SourcePath);
        var reservation = OutputNameAllocator.FindAvailable(
            sourceDirectory,
            sourceStem,
            requireMp3: true,
            requireTxt: false);
        return await ProcessToReservationAsync(item, bitrateKbps, reservation, progress, cancellationToken)
            .ConfigureAwait(false);
    }

    public async Task<VideoProcessingResult> ProcessToReservationAsync(
        QueueItem item,
        int bitrateKbps,
        OutputReservation reservation,
        IProgress<int>? progress,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(item);
        ArgumentNullException.ThrowIfNull(reservation);
        if (!AllowedBitrates.Contains(bitrateKbps))
        {
            throw new ArgumentOutOfRangeException(nameof(bitrateKbps), "码率只能是 128、192、256 或 320 kbps。 ");
        }

        if (!File.Exists(item.SourcePath))
        {
            throw new FileNotFoundException("源文件已经不存在。请重新复制该文件后再试。", item.FileName);
        }

        item.Stage = JobStage.Probing;
        item.StepDetail = "正在检查媒体格式、时长和第一条音轨";
        var probe = await mediaProbe.ProbeAsync(item.SourcePath, cancellationToken).ConfigureAwait(false);
        if (!probe.HasVideo)
        {
            throw new InvalidDataException("文件不是可解码的视频。第一版只有现有 MP3 可以直接转文字。 ");
        }

        if (!probe.HasAudio)
        {
            throw new InvalidDataException("视频没有可用音轨，无法生成 MP3。 ");
        }

        item.Kind = MediaKind.Video;
        item.Stage = JobStage.ConvertingMp3;
        item.StepDetail = probe.AudioStreamCount > 1
            ? $"正在转换第一条音轨；检测到 {probe.AudioStreamCount} 条音轨"
            : "正在转换第一条音轨";

        var finalPath = reservation.Mp3Path
            ?? throw new InvalidOperationException("没有生成 MP3 输出路径。 ");
        var partPath = OutputNameAllocator.CreatePartPath(finalPath);

        try
        {
            var request = new AudioConversionRequest(
                item.SourcePath,
                partPath,
                probe.Duration,
                bitrateKbps);
            await audioConverter.ConvertFinalMp3Async(request, progress, cancellationToken).ConfigureAwait(false);
            cancellationToken.ThrowIfCancellationRequested();
            outputCommitter.CommitOwnedPart(partPath, finalPath);
            item.Stage = JobStage.Succeeded;
            item.StepDetail = "MP3 已保存到源文件旁边";
            item.ResultMessage = $"成功生成 {Path.GetFileName(finalPath)}";
            return new VideoProcessingResult(finalPath, probe);
        }
        catch
        {
            outputCommitter.DeleteOwnedPartIfPresent(partPath);
            throw;
        }
    }
}
