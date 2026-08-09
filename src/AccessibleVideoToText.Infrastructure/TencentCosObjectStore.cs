using AccessibleVideoToText.Core;
using COSXML;
using COSXML.Auth;
using COSXML.CosException;
using COSXML.Model;
using COSXML.Model.Bucket;
using COSXML.Model.Object;
using COSXML.Model.Tag;

namespace AccessibleVideoToText.Infrastructure;

public sealed class TencentCosObjectStore : ICloudObjectStore
{
    private const long SignedUrlLifetimeSeconds = 24 * 60 * 60;
    private readonly CloudCredentials credentials;
    private readonly CosXmlServer server;
    private readonly string? preferredBucket;
    private string? readyBucket;

    public TencentCosObjectStore(CloudCredentials credentials, string? preferredBucket = null)
    {
        this.credentials = credentials ?? throw new ArgumentNullException(nameof(credentials));
        TencentCosPolicy.ValidateAppId(credentials.AppId);
        this.preferredBucket = ValidatePreferredBucket(preferredBucket, credentials.AppId);

        var config = new CosXmlConfig.Builder()
            .SetAppid(credentials.AppId)
            .SetRegion(TencentCosPolicy.Region)
            .IsHttps(true)
            .SetConnectionTimeoutMs(30_000)
            .SetReadWriteTimeoutMs(60_000)
            .SetDebugLog(false)
            .Build();
        var provider = new DefaultQCloudCredentialProvider(
            credentials.SecretId,
            credentials.SecretKey,
            SignedUrlLifetimeSeconds);
        server = new CosXmlServer(config, provider);
    }

    public async Task<string> EnsureReadyAsync(CancellationToken cancellationToken)
    {
        if (readyBucket is not null)
        {
            return readyBucket;
        }

        var candidates = BuildBucketCandidates();
        Exception? lastCollision = null;
        foreach (var candidate in candidates)
        {
            cancellationToken.ThrowIfCancellationRequested();
            try
            {
                var request = new PutBucketRequest(candidate);
                request.SetCosACL("private");
                await ExecuteAsync<PutBucketResult>(request, cancellationToken).ConfigureAwait(false);
                await ApplyLifecycleAsync(candidate, cancellationToken).ConfigureAwait(false);
                readyBucket = candidate;
                return candidate;
            }
            catch (CosServerException exception) when (IsAlreadyOwned(exception))
            {
                await ApplyLifecycleAsync(candidate, cancellationToken).ConfigureAwait(false);
                readyBucket = candidate;
                return candidate;
            }
            catch (CosServerException exception) when (IsNameCollision(exception))
            {
                lastCollision = exception;
            }
        }

        throw new InvalidOperationException("无法创建应用专用 COS 存储桶，请检查 AppID、权限和存储桶配额。", lastCollision);
    }

    public async Task<CloudObjectReference> UploadAsync(
        string localPath,
        IProgress<int>? progress,
        CancellationToken cancellationToken)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(localPath);
        var file = new FileInfo(localPath);
        if (!file.Exists)
        {
            throw new FileNotFoundException("待上传的临时音频不存在。", file.Name);
        }

        if (file.Length <= 0 || file.Length > CloudLimits.MaximumAudioBytes)
        {
            throw new InvalidOperationException("临时音频为空或超过腾讯云 1 GB 限制。 ");
        }

        var bucket = await EnsureReadyAsync(cancellationToken).ConfigureAwait(false);
        var objectKey = TencentCosPolicy.CreateObjectKey();
        var request = new PutObjectRequest(bucket, objectKey, localPath);
        var throttler = new ProgressThrottler(5);
        request.SetCosProgressCallback((completed, total) =>
        {
            if (total > 0)
            {
                var accepted = throttler.Accept(Math.Clamp(completed * 100d / total, 0, 100));
                if (accepted is not null)
                {
                    progress?.Report(accepted.Value);
                }
            }
        });

        await ExecuteAsync<PutObjectResult>(request, cancellationToken).ConfigureAwait(false);
        var finalProgress = throttler.Accept(100);
        if (finalProgress is not null)
        {
            progress?.Report(finalProgress.Value);
        }

        var signature = new PreSignatureStruct
        {
            appid = credentials.AppId,
            bucket = bucket,
            region = TencentCosPolicy.Region,
            key = objectKey,
            isHttps = true,
            signHost = true,
            httpMethod = "GET",
            signDurationSecond = SignedUrlLifetimeSeconds,
            keyDurationSecond = SignedUrlLifetimeSeconds
        };
        var signedUrl = server.GenerateSignURL(signature);
        if (!Uri.TryCreate(signedUrl, UriKind.Absolute, out var signedUri) || signedUri.Scheme != Uri.UriSchemeHttps)
        {
            throw new InvalidOperationException("COS 没有生成有效的 HTTPS 签名地址。 ");
        }

        return new CloudObjectReference(bucket, TencentCosPolicy.Region, objectKey, signedUri);
    }

    public async Task DeleteAsync(CloudObjectReference cloudObject, CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(cloudObject);
        var bucket = await EnsureReadyAsync(cancellationToken).ConfigureAwait(false);
        if (!string.Equals(cloudObject.Bucket, bucket, StringComparison.Ordinal) ||
            !string.Equals(cloudObject.Region, TencentCosPolicy.Region, StringComparison.Ordinal) ||
            !TencentCosPolicy.IsOwnedObjectKey(cloudObject.ObjectKey))
        {
            throw new InvalidOperationException("拒绝删除应用专用存储桶和前缀以外的 COS 对象。 ");
        }

        await ExecuteAsync<DeleteObjectResult>(
            new DeleteObjectRequest(bucket, cloudObject.ObjectKey),
            cancellationToken).ConfigureAwait(false);
    }

    private IReadOnlyList<string> BuildBucketCandidates()
    {
        var result = new List<string>(6);
        if (preferredBucket is not null)
        {
            result.Add(preferredBucket);
        }

        var baseName = TencentCosPolicy.BaseBucketName(credentials.AppId);
        if (!result.Contains(baseName, StringComparer.Ordinal))
        {
            result.Add(baseName);
        }

        while (result.Count < 6)
        {
            var suffix = Guid.NewGuid().ToString("N")[..8];
            result.Add(TencentCosPolicy.CollisionBucketName(credentials.AppId, suffix));
        }

        return result;
    }

    private async Task ApplyLifecycleAsync(string bucket, CancellationToken cancellationToken)
    {
        var request = new PutBucketLifecycleRequest(bucket);
        request.SetRules(TencentCosPolicy.CreateLifecycleRules().ToList());
        await ExecuteAsync<PutBucketLifecycleResult>(request, cancellationToken).ConfigureAwait(false);
    }

    private async Task<T> ExecuteAsync<T>(CosRequest request, CancellationToken cancellationToken)
        where T : CosResult
    {
        using var registration = cancellationToken.Register(() =>
        {
            try
            {
                server.Cancel(request);
            }
            catch
            {
                // Cancellation is best effort; WaitAsync still observes the token.
            }
        });
        return await server.ExecuteAsync<T>(request).WaitAsync(cancellationToken).ConfigureAwait(false);
    }

    private static string? ValidatePreferredBucket(string? bucket, string appId)
    {
        if (string.IsNullOrWhiteSpace(bucket))
        {
            return null;
        }

        if (!bucket.StartsWith("accessible-video-to-text-", StringComparison.Ordinal) ||
            !bucket.EndsWith($"-{appId}", StringComparison.Ordinal) &&
            !string.Equals(bucket, TencentCosPolicy.BaseBucketName(appId), StringComparison.Ordinal))
        {
            throw new ArgumentException("已保存的 COS 存储桶不属于本应用或 AppID 不匹配。", nameof(bucket));
        }

        return bucket;
    }

    private static bool IsAlreadyOwned(CosServerException exception) =>
        string.Equals(exception.errorCode, "BucketAlreadyOwnedByYou", StringComparison.OrdinalIgnoreCase);

    private static bool IsNameCollision(CosServerException exception) =>
        exception.statusCode == 409 ||
        string.Equals(exception.errorCode, "BucketAlreadyExists", StringComparison.OrdinalIgnoreCase);
}
