namespace AccessibleVideoToText.Core;

public enum DownloadedMediaAction
{
    ConvertVideo,
    ConvertAudio,
    UseExistingMp3,
    Skip
}

public sealed record DownloadedMediaPlan(string Path, DownloadedMediaAction Action, string Reason);

public static class DownloadPipelinePlanner
{
    private static readonly HashSet<string> VideoExtensions = new(StringComparer.OrdinalIgnoreCase)
    {
        ".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm", ".m4v",
        ".mpeg", ".mpg", ".ts", ".m2ts", ".3gp"
    };

    private static readonly HashSet<string> AudioExtensions = new(StringComparer.OrdinalIgnoreCase)
    {
        ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus", ".wma"
    };

    public static IReadOnlyList<DownloadedMediaPlan> Create(IEnumerable<string> paths)
    {
        ArgumentNullException.ThrowIfNull(paths);
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var result = new List<DownloadedMediaPlan>();
        foreach (var rawPath in paths)
        {
            if (string.IsNullOrWhiteSpace(rawPath))
            {
                continue;
            }

            var path = rawPath.Trim();
            if (!seen.Add(path))
            {
                continue;
            }

            var extension = Path.GetExtension(path);
            if (extension.Equals(".mp3", StringComparison.OrdinalIgnoreCase))
            {
                result.Add(new DownloadedMediaPlan(path, DownloadedMediaAction.UseExistingMp3, "已有 MP3 不重新编码"));
            }
            else if (VideoExtensions.Contains(extension))
            {
                result.Add(new DownloadedMediaPlan(path, DownloadedMediaAction.ConvertVideo, "视频可转换 MP3"));
            }
            else if (AudioExtensions.Contains(extension))
            {
                result.Add(new DownloadedMediaPlan(path, DownloadedMediaAction.ConvertAudio, "音频可转换 MP3"));
            }
            else
            {
                result.Add(new DownloadedMediaPlan(path, DownloadedMediaAction.Skip, "图片或不支持的音频/文件格式，保留下载结果"));
            }
        }

        return result;
    }
}
