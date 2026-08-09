using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using AccessibleVideoToText.Core;

namespace AccessibleVideoToText.Infrastructure;

public sealed class DpapiSettingsStore : ISettingsStore
{
    private static readonly byte[] OptionalEntropy = SHA256.HashData(
        Encoding.UTF8.GetBytes("AccessibleVideoToText|credentials|v1"));
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = true
    };

    private readonly LocalDataPaths paths;

    public DpapiSettingsStore(LocalDataPaths paths)
    {
        this.paths = paths ?? throw new ArgumentNullException(nameof(paths));
        paths.EnsureDirectories();
    }

    public async Task<AppSettings> LoadAsync(CancellationToken cancellationToken)
    {
        if (!File.Exists(paths.SettingsFile))
        {
            return new AppSettings();
        }

        await using var stream = new FileStream(
            paths.SettingsFile,
            FileMode.Open,
            FileAccess.Read,
            FileShare.Read,
            4096,
            FileOptions.Asynchronous);
        return await JsonSerializer.DeserializeAsync<AppSettings>(stream, JsonOptions, cancellationToken).ConfigureAwait(false)
            ?? new AppSettings();
    }

    public async Task SaveAsync(AppSettings settings, CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(settings);
        var bytes = JsonSerializer.SerializeToUtf8Bytes(settings, JsonOptions);
        await WritePrivateBytesAtomicallyAsync(paths.SettingsFile, bytes, cancellationToken).ConfigureAwait(false);
    }

    public async Task SaveCredentialsAsync(CloudCredentials credentials, CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(credentials);
        var plaintext = JsonSerializer.SerializeToUtf8Bytes(credentials, JsonOptions);
        try
        {
            var encrypted = ProtectedData.Protect(plaintext, OptionalEntropy, DataProtectionScope.CurrentUser);
            await WritePrivateBytesAtomicallyAsync(paths.CredentialsFile, encrypted, cancellationToken).ConfigureAwait(false);
            CryptographicOperations.ZeroMemory(encrypted);
        }
        finally
        {
            CryptographicOperations.ZeroMemory(plaintext);
        }
    }

    public async Task<CloudCredentials?> LoadCredentialsAsync(CancellationToken cancellationToken)
    {
        if (!File.Exists(paths.CredentialsFile))
        {
            return null;
        }

        var encrypted = await File.ReadAllBytesAsync(paths.CredentialsFile, cancellationToken).ConfigureAwait(false);
        byte[]? plaintext = null;
        try
        {
            plaintext = ProtectedData.Unprotect(encrypted, OptionalEntropy, DataProtectionScope.CurrentUser);
            return JsonSerializer.Deserialize<CloudCredentials>(plaintext, JsonOptions);
        }
        finally
        {
            CryptographicOperations.ZeroMemory(encrypted);
            if (plaintext is not null)
            {
                CryptographicOperations.ZeroMemory(plaintext);
            }
        }
    }

    private static async Task WritePrivateBytesAtomicallyAsync(
        string finalPath,
        byte[] bytes,
        CancellationToken cancellationToken)
    {
        var directory = Path.GetDirectoryName(finalPath)
            ?? throw new InvalidOperationException("私有数据路径没有目录。 ");
        Directory.CreateDirectory(directory);
        var partPath = Path.Combine(directory, $"{Path.GetFileName(finalPath)}.{Guid.NewGuid():N}.part");
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

            if (File.Exists(finalPath))
            {
                File.Replace(partPath, finalPath, destinationBackupFileName: null, ignoreMetadataErrors: true);
            }
            else
            {
                File.Move(partPath, finalPath, overwrite: false);
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

