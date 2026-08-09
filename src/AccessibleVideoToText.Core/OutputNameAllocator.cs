namespace AccessibleVideoToText.Core;

public sealed record OutputReservation(string Stem, string? Mp3Path, string? TxtPath);

public static class OutputNameAllocator
{
    public static OutputReservation FindAvailable(
        string directory,
        string sourceStem,
        bool requireMp3,
        bool requireTxt,
        Func<string, bool>? pathExists = null)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(directory);
        ArgumentException.ThrowIfNullOrWhiteSpace(sourceStem);
        if (!requireMp3 && !requireTxt)
        {
            throw new ArgumentException("至少需要一种输出。", nameof(requireMp3));
        }

        pathExists ??= File.Exists;

        for (var suffix = 0; suffix < int.MaxValue; suffix++)
        {
            var stem = suffix == 0 ? sourceStem : $"{sourceStem} ({suffix})";
            var mp3Path = requireMp3 ? Path.Combine(directory, $"{stem}.mp3") : null;
            var txtPath = requireTxt ? Path.Combine(directory, $"{stem}.txt") : null;
            if ((mp3Path is null || !pathExists(mp3Path)) &&
                (txtPath is null || !pathExists(txtPath)))
            {
                return new OutputReservation(stem, mp3Path, txtPath);
            }
        }

        throw new IOException("无法找到可用的输出文件名。请整理目标目录后重试。");
    }

    public static string CreatePartPath(string finalPath)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(finalPath);
        var directory = Path.GetDirectoryName(finalPath)
            ?? throw new ArgumentException("输出路径没有目录。", nameof(finalPath));
        var stem = Path.GetFileNameWithoutExtension(finalPath);
        var extension = Path.GetExtension(finalPath);
        return Path.Combine(directory, $"{stem}.{Guid.NewGuid():N}.part{extension}");
    }
}

