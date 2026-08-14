namespace AccessibleVideoToText.Core;

public interface IPathInspector
{
    bool FileExists(string path);

    bool DirectoryExists(string path);

    string GetFullPath(string path);
}

public sealed class PhysicalPathInspector : IPathInspector
{
    public bool FileExists(string path) => File.Exists(path);

    public bool DirectoryExists(string path) => Directory.Exists(path);

    public string GetFullPath(string path) => Path.GetFullPath(path);
}

public sealed record ClipboardBatchResult(
    IReadOnlyList<QueueItem> Accepted,
    int DuplicateCount,
    int SkippedCount,
    bool RejectedForLimit,
    string Summary);

public sealed class ClipboardBatchAnalyzer
{
    public const int MaximumAcceptedFiles = 100;

    private static readonly HashSet<string> VideoExtensions = new(StringComparer.OrdinalIgnoreCase)
    {
        ".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm", ".m4v",
        ".mpeg", ".mpg", ".ts", ".m2ts", ".3gp"
    };

    private static readonly HashSet<string> AudioExtensions = new(StringComparer.OrdinalIgnoreCase)
    {
        ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus", ".wma"
    };

    private static readonly HashSet<string> KnownNonMediaExtensions = new(StringComparer.OrdinalIgnoreCase)
    {
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff",
        ".txt", ".rtf", ".doc", ".docx", ".pdf", ".zip", ".7z", ".rar",
        ".exe", ".dll", ".lnk"
    };

    private readonly IPathInspector pathInspector;

    public ClipboardBatchAnalyzer(IPathInspector? pathInspector = null)
    {
        this.pathInspector = pathInspector ?? new PhysicalPathInspector();
    }

    public ClipboardBatchResult Analyze(IEnumerable<string> clipboardPaths)
    {
        ArgumentNullException.ThrowIfNull(clipboardPaths);

        var accepted = new List<QueueItem>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var duplicateCount = 0;
        var skippedCount = 0;

        foreach (var rawPath in clipboardPaths)
        {
            if (string.IsNullOrWhiteSpace(rawPath))
            {
                skippedCount++;
                continue;
            }

            string fullPath;
            try
            {
                fullPath = pathInspector.GetFullPath(rawPath.Trim());
            }
            catch (Exception exception) when (exception is ArgumentException or NotSupportedException or PathTooLongException)
            {
                skippedCount++;
                continue;
            }

            if (!seen.Add(fullPath))
            {
                duplicateCount++;
                continue;
            }

            if (pathInspector.DirectoryExists(fullPath) || !pathInspector.FileExists(fullPath))
            {
                skippedCount++;
                continue;
            }

            var extension = Path.GetExtension(fullPath);
            if (KnownNonMediaExtensions.Contains(extension))
            {
                skippedCount++;
                continue;
            }

            var kind = extension.Equals(".mp3", StringComparison.OrdinalIgnoreCase)
                ? MediaKind.Mp3
                : AudioExtensions.Contains(extension)
                    ? MediaKind.Audio
                    : VideoExtensions.Contains(extension)
                        ? MediaKind.Video
                        : MediaKind.ProbeCandidate;

            accepted.Add(new QueueItem(fullPath, kind));
        }

        if (accepted.Count > MaximumAcceptedFiles)
        {
            var summary = $"有效文件 {accepted.Count} 个，超过一次最多 {MaximumAcceptedFiles} 个的限制，整批未加入。";
            return new ClipboardBatchResult(Array.Empty<QueueItem>(), duplicateCount, skippedCount, true, summary);
        }

        var videos = accepted.Count(item => item.Kind is MediaKind.Video or MediaKind.ProbeCandidate);
        var audioFiles = accepted.Count(item => item.Kind == MediaKind.Audio);
        var mp3Files = accepted.Count(item => item.Kind == MediaKind.Mp3);
        var acceptedSummary = $"已读取视频或待探测媒体 {videos} 个，音频 {audioFiles} 个，MP3 {mp3Files} 个，跳过 {skippedCount} 个，重复 {duplicateCount} 个。";
        return new ClipboardBatchResult(accepted, duplicateCount, skippedCount, false, acceptedSummary);
    }
}
