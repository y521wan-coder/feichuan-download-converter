using System.Collections.ObjectModel;

namespace AccessibleVideoToText.Core;

public enum MediaKind
{
    Video,
    Audio,
    Mp3,
    ProbeCandidate
}

public sealed class QueueItem
{
    public QueueItem(string sourcePath, MediaKind kind)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(sourcePath);
        SourcePath = sourcePath;
        FileName = Path.GetFileName(sourcePath);
        Kind = kind;
    }

    public Guid Id { get; } = Guid.NewGuid();

    public string SourcePath { get; }

    public string FileName { get; }

    public MediaKind Kind { get; set; }

    public JobStage Stage { get; set; } = JobStage.Waiting;

    public string StepDetail { get; set; } = "等待处理";

    public string ResultMessage { get; set; } = string.Empty;

    public bool IsSelected { get; set; } = true;

    public string KindText => Kind switch
    {
        MediaKind.Video => "视频",
        MediaKind.Audio => "音频",
        MediaKind.Mp3 => "MP3",
        MediaKind.ProbeCandidate => "待探测媒体",
        _ => "未知"
    };
}

public sealed record MediaProbeResult(
    string FormatName,
    TimeSpan Duration,
    bool HasVideo,
    int AudioStreamCount,
    string? AudioCodec,
    int? SampleRate,
    int? Channels,
    IReadOnlyDictionary<string, string> Metadata,
    double BitRate = 0)
{
    public static MediaProbeResult Empty { get; } = new(
        string.Empty,
        TimeSpan.Zero,
        false,
        0,
        null,
        null,
        null,
        new ReadOnlyDictionary<string, string>(new Dictionary<string, string>()));

    public bool HasAudio => AudioStreamCount > 0;

    public bool ExceedsCloudDurationLimit => Duration > TimeSpan.FromHours(5);
}

public sealed record AudioConversionRequest(
    string SourcePath,
    string TemporaryOutputPath,
    TimeSpan Duration,
    int BitrateKbps);

public sealed record CloudAudioRequest(
    string SourcePath,
    string TemporaryOutputPath,
    TimeSpan Duration);
