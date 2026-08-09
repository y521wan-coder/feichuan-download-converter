using System.Text.Json;

namespace AccessibleVideoToText.Infrastructure;

internal static class AtomicPrivateJson
{
    internal static readonly JsonSerializerOptions Options = new()
    {
        WriteIndented = true
    };

    public static async Task<T?> ReadAsync<T>(string path, CancellationToken cancellationToken)
    {
        if (!File.Exists(path))
        {
            return default;
        }

        await using var stream = new FileStream(
            path,
            FileMode.Open,
            FileAccess.Read,
            FileShare.Read,
            4096,
            FileOptions.Asynchronous);
        return await JsonSerializer.DeserializeAsync<T>(stream, Options, cancellationToken).ConfigureAwait(false);
    }

    public static async Task WriteAsync<T>(string path, T value, CancellationToken cancellationToken)
    {
        var directory = Path.GetDirectoryName(path)
            ?? throw new InvalidOperationException("私有数据路径没有目录。 ");
        Directory.CreateDirectory(directory);
        var bytes = JsonSerializer.SerializeToUtf8Bytes(value, Options);
        var partPath = Path.Combine(directory, $"{Path.GetFileName(path)}.{Guid.NewGuid():N}.part");
        try
        {
            await using (var stream = new FileStream(
                partPath,
                FileMode.CreateNew,
                FileAccess.Write,
                FileShare.None,
                4096,
                FileOptions.Asynchronous | FileOptions.WriteThrough))
            {
                await stream.WriteAsync(bytes, cancellationToken).ConfigureAwait(false);
                await stream.FlushAsync(cancellationToken).ConfigureAwait(false);
                stream.Flush(flushToDisk: true);
            }

            if (File.Exists(path))
            {
                File.Replace(partPath, path, destinationBackupFileName: null, ignoreMetadataErrors: true);
            }
            else
            {
                File.Move(partPath, path, overwrite: false);
            }
        }
        catch
        {
            if (File.Exists(partPath))
            {
                File.Delete(partPath);
            }

            throw;
        }
    }
}
