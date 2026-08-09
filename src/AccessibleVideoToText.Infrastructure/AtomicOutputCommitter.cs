using System.Text;
using AccessibleVideoToText.Core;

namespace AccessibleVideoToText.Infrastructure;

public sealed class AtomicOutputCommitter : IAtomicOutputCommitter
{
    private static readonly UTF8Encoding Utf8WithBom = new(encoderShouldEmitUTF8Identifier: true);

    public void CommitOwnedPart(string partPath, string finalPath)
    {
        ValidateSameDirectory(partPath, finalPath);
        if (!File.Exists(partPath))
        {
            throw new FileNotFoundException("临时输出不存在，不能提交结果。", Path.GetFileName(partPath));
        }

        if (File.Exists(finalPath))
        {
            throw new IOException($"目标文件 {Path.GetFileName(finalPath)} 已存在，未覆盖。 ");
        }

        File.Move(partPath, finalPath, overwrite: false);
    }

    public void DeleteOwnedPartIfPresent(string partPath)
    {
        if (!IsOwnedPartName(partPath) || !File.Exists(partPath))
        {
            return;
        }

        File.Delete(partPath);
    }

    public async Task WriteUtf8BomTextAsync(string finalPath, string text, CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(text))
        {
            throw new InvalidDataException("识别结果没有有效语音正文，未生成空 TXT。 ");
        }

        if (File.Exists(finalPath))
        {
            throw new IOException($"目标文件 {Path.GetFileName(finalPath)} 已存在，未覆盖。 ");
        }

        var normalized = NormalizeWindowsLineEndings(text.Trim());
        var partPath = OutputNameAllocator.CreatePartPath(finalPath);
        try
        {
            await using (var stream = new FileStream(
                partPath,
                FileMode.CreateNew,
                FileAccess.Write,
                FileShare.None,
                bufferSize: 4096,
                FileOptions.Asynchronous | FileOptions.WriteThrough))
            await using (var writer = new StreamWriter(stream, Utf8WithBom))
            {
                await writer.WriteAsync(normalized.AsMemory(), cancellationToken).ConfigureAwait(false);
                await writer.FlushAsync(cancellationToken).ConfigureAwait(false);
                stream.Flush(flushToDisk: true);
            }

            cancellationToken.ThrowIfCancellationRequested();
            CommitOwnedPart(partPath, finalPath);
        }
        catch
        {
            DeleteOwnedPartIfPresent(partPath);
            throw;
        }
    }

    private static string NormalizeWindowsLineEndings(string text)
    {
        var lf = text.Replace("\r\n", "\n", StringComparison.Ordinal)
            .Replace('\r', '\n');
        return lf.Replace("\n", "\r\n", StringComparison.Ordinal);
    }

    private static bool IsOwnedPartName(string path)
    {
        var name = Path.GetFileName(path);
        return name.Contains(".part.", StringComparison.OrdinalIgnoreCase);
    }

    private static void ValidateSameDirectory(string partPath, string finalPath)
    {
        var partDirectory = Path.GetFullPath(Path.GetDirectoryName(partPath) ?? string.Empty);
        var finalDirectory = Path.GetFullPath(Path.GetDirectoryName(finalPath) ?? string.Empty);
        if (!partDirectory.Equals(finalDirectory, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException("临时文件和最终文件必须位于同一目录，才能安全提交。 ");
        }
    }
}

