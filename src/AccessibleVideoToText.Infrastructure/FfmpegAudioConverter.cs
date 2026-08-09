using AccessibleVideoToText.Core;
using FFMpegCore;

namespace AccessibleVideoToText.Infrastructure;

public sealed class FfmpegAudioConverter : IAudioConverter
{
    private static readonly HashSet<int> AllowedBitrates = [128, 192, 256, 320];

    private readonly FFOptions options;

    public FfmpegAudioConverter(LocalDataPaths localDataPaths, string? binaryFolder = null)
    {
        ArgumentNullException.ThrowIfNull(localDataPaths);
        localDataPaths.EnsureDirectories();
        options = new FFOptions
        {
            BinaryFolder = binaryFolder ?? string.Empty,
            TemporaryFilesFolder = localDataPaths.TempDirectory,
            WorkingDirectory = localDataPaths.TempDirectory
        };
    }

    public async Task ConvertFinalMp3Async(
        AudioConversionRequest request,
        IProgress<int>? progress,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(request);
        if (!AllowedBitrates.Contains(request.BitrateKbps))
        {
            throw new ArgumentOutOfRangeException(nameof(request), "最终 MP3 码率不在允许范围内。 ");
        }

        await ConvertAsync(
            request.SourcePath,
            request.TemporaryOutputPath,
            request.Duration,
            request.BitrateKbps,
            "-map 0:a:0 -vn -map_metadata 0 -map_chapters -1 -write_id3v1 0",
            progress,
            cancellationToken).ConfigureAwait(false);
    }

    public async Task CreateCloudAudioAsync(
        CloudAudioRequest request,
        IProgress<int>? progress,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(request);
        await ConvertAsync(
            request.SourcePath,
            request.TemporaryOutputPath,
            request.Duration,
            bitrateKbps: 64,
            "-map 0:a:0 -vn -ac 1 -ar 16000 -map_metadata -1 -map_chapters -1 -write_id3v1 0",
            progress,
            cancellationToken).ConfigureAwait(false);
    }

    private async Task ConvertAsync(
        string sourcePath,
        string outputPath,
        TimeSpan duration,
        int bitrateKbps,
        string customArguments,
        IProgress<int>? progress,
        CancellationToken cancellationToken)
    {
        if (File.Exists(outputPath))
        {
            throw new IOException("临时输出已存在，拒绝覆盖。 ");
        }

        var throttler = new ProgressThrottler(5);
        var processor = FFMpegArguments
            .FromFileInput(sourcePath, verifyExists: true)
            .OutputToFile(outputPath, overwrite: false, output => output
                .WithCustomArgument(customArguments)
                .WithAudioCodec("libmp3lame")
                .WithAudioBitrate(bitrateKbps)
                .WithTagVersion(3)
                .ForceFormat("mp3"))
            .NotifyOnProgress(value =>
            {
                if (throttler.Accept(value) is { } accepted)
                {
                    progress?.Report(accepted);
                }
            }, duration)
            .CancellableThrough(cancellationToken, timeout: 1000);

        await processor.ProcessAsynchronously(throwOnError: true, options).ConfigureAwait(false);
    }
}

