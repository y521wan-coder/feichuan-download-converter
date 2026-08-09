using AccessibleVideoToText.Core;
using FFMpegCore;

namespace AccessibleVideoToText.Infrastructure;

public sealed class FfmpegMediaProbe : IMediaProbe
{
    private readonly FFOptions options;

    public FfmpegMediaProbe(LocalDataPaths localDataPaths, string? binaryFolder = null)
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

    public async Task<MediaProbeResult> ProbeAsync(string sourcePath, CancellationToken cancellationToken)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(sourcePath);
        if (!File.Exists(sourcePath))
        {
            throw new FileNotFoundException("源文件已经不存在。", Path.GetFileName(sourcePath));
        }

        var analysis = await FFProbe.AnalyseAsync(
            sourcePath,
            options,
            cancellationToken).ConfigureAwait(false);
        var firstAudio = analysis.AudioStreams.OrderBy(stream => stream.Index).FirstOrDefault();
        var metadata = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        if (analysis.Format.Tags is not null)
        {
            foreach (var pair in analysis.Format.Tags)
            {
                if (pair.Key is not null && pair.Value is not null)
                {
                    metadata[pair.Key] = pair.Value;
                }
            }
        }

        return new MediaProbeResult(
            analysis.Format.FormatName,
            analysis.Duration,
            analysis.VideoStreams.Count > 0,
            analysis.AudioStreams.Count,
            firstAudio?.CodecName,
            firstAudio?.SampleRateHz,
            firstAudio?.Channels,
            metadata,
            analysis.Format.BitRate);
    }
}
