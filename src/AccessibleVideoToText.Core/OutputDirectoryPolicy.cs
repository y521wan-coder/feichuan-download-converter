namespace AccessibleVideoToText.Core;

public static class OutputDirectoryPreference
{
    public const string SourceDirectory = "source-directory";

    public const string CustomDirectory = "custom-directory";
}

public static class OutputDirectoryPolicy
{
    public static bool UsesCustomDirectory(AppSettings settings)
    {
        ArgumentNullException.ThrowIfNull(settings);
        return string.Equals(
            settings.OutputPreference,
            OutputDirectoryPreference.CustomDirectory,
            StringComparison.Ordinal);
    }

    public static string ResolveForSource(AppSettings settings, string sourcePath)
    {
        ArgumentNullException.ThrowIfNull(settings);
        ArgumentException.ThrowIfNullOrWhiteSpace(sourcePath);

        var directory = UsesCustomDirectory(settings)
            ? ValidateExistingDirectory(
                settings.CustomOutputDirectory,
                "已选择统一结果目录，但目录为空、已移动或已删除。请在设置中重新选择结果目录。")
            : ValidateExistingDirectory(
                Path.GetDirectoryName(Path.GetFullPath(sourcePath)),
                "无法确定源文件所在目录。请重新选择源文件。 ");

        EnsureWritable(directory);
        return directory;
    }

    public static string ValidateCustomDirectory(string? directory)
    {
        var normalized = ValidateExistingDirectory(
            directory,
            "请选择一个当前存在的统一结果目录。 ");
        EnsureWritable(normalized);
        return normalized;
    }

    public static string NormalizePreference(string? preference) =>
        string.Equals(preference, OutputDirectoryPreference.CustomDirectory, StringComparison.Ordinal)
            ? OutputDirectoryPreference.CustomDirectory
            : OutputDirectoryPreference.SourceDirectory;

    public static bool IsWritable(string? directory)
    {
        try
        {
            var normalized = ValidateExistingDirectory(directory, "结果目录不可用。 ");
            EnsureWritable(normalized);
            return true;
        }
        catch (Exception exception) when (
            exception is ArgumentException or IOException or UnauthorizedAccessException or NotSupportedException)
        {
            return false;
        }
    }

    private static string ValidateExistingDirectory(string? directory, string errorMessage)
    {
        if (string.IsNullOrWhiteSpace(directory))
        {
            throw new InvalidDataException(errorMessage);
        }

        string normalized;
        try
        {
            normalized = Path.GetFullPath(directory.Trim());
        }
        catch (Exception exception) when (
            exception is ArgumentException or NotSupportedException or PathTooLongException)
        {
            throw new InvalidDataException(errorMessage, exception);
        }

        if (!Directory.Exists(normalized))
        {
            throw new InvalidDataException(errorMessage);
        }

        return normalized;
    }

    private static void EnsureWritable(string directory)
    {
        var probePath = Path.Combine(directory, $".feichuan-output.{Guid.NewGuid():N}.write-test");
        var probeCreated = false;
        try
        {
            using (new FileStream(
                probePath,
                FileMode.CreateNew,
                FileAccess.Write,
                FileShare.None,
                bufferSize: 1,
                FileOptions.WriteThrough))
            {
                probeCreated = true;
            }

            File.Delete(probePath);
            probeCreated = false;
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException or DirectoryNotFoundException or NotSupportedException)
        {
            throw new IOException("结果目录不可写或当前不可用。请检查磁盘连接和权限，或在设置中重新选择。 ", exception);
        }
        finally
        {
            if (probeCreated && File.Exists(probePath))
            {
                try
                {
                    File.Delete(probePath);
                }
                catch
                {
                    // The probe contains no user data. The original actionable failure is preserved.
                }
            }
        }
    }
}
