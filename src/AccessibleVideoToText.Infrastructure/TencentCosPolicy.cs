using COSXML.Model.Tag;

namespace AccessibleVideoToText.Infrastructure;

public static class TencentCosPolicy
{
    public const string Region = "ap-shanghai";
    public const string ObjectPrefix = "accessible-video-to-text/";
    public const int LifecycleDays = 1;

    public static string BaseBucketName(string appId)
    {
        ValidateAppId(appId);
        return $"accessible-video-to-text-{appId}";
    }

    public static string CollisionBucketName(string appId, string suffix)
    {
        ValidateAppId(appId);
        if (string.IsNullOrWhiteSpace(suffix) || suffix.Any(character => !char.IsAsciiLetterOrDigit(character)))
        {
            throw new ArgumentException("存储桶随机后缀只能包含 ASCII 字母和数字。", nameof(suffix));
        }

        return $"accessible-video-to-text-{suffix.ToLowerInvariant()}-{appId}";
    }

    public static string CreateObjectKey() => $"{ObjectPrefix}{Guid.NewGuid():N}.mp3";

    public static IReadOnlyList<LifecycleConfiguration.Rule> CreateLifecycleRules()
    {
        return
        [
            new LifecycleConfiguration.Rule
            {
                id = "accessible-video-to-text-expire",
                filter = new LifecycleConfiguration.Filter { prefix = ObjectPrefix },
                status = "Enabled",
                expiration = new LifecycleConfiguration.Expiration { days = LifecycleDays }
            },
            new LifecycleConfiguration.Rule
            {
                id = "accessible-video-to-text-abort-incomplete",
                filter = new LifecycleConfiguration.Filter { prefix = ObjectPrefix },
                status = "Enabled",
                abortIncompleteMultiUpload = new LifecycleConfiguration.AbortIncompleteMultiUpload
                {
                    daysAfterInitiation = LifecycleDays
                }
            }
        ];
    }

    public static bool IsOwnedObjectKey(string key) =>
        !string.IsNullOrWhiteSpace(key) &&
        key.StartsWith(ObjectPrefix, StringComparison.Ordinal) &&
        !key[ObjectPrefix.Length..].Contains('/');

    public static void ValidateAppId(string appId)
    {
        if (string.IsNullOrWhiteSpace(appId) || appId.Length is < 5 or > 20 || appId.Any(character => !char.IsAsciiDigit(character)))
        {
            throw new ArgumentException("腾讯云 AppID 必须是 5 到 20 位数字。", nameof(appId));
        }
    }
}
