using COSXML.CosException;
using TencentCloud.Common;

namespace AccessibleVideoToText.Infrastructure;

public static class TencentCloudErrorClassifier
{
    public static bool IsTransient(Exception exception)
    {
        if (exception is IOException or HttpRequestException or TimeoutException)
        {
            return true;
        }

        if (exception is CosServerException cosServer)
        {
            return cosServer.statusCode is 408 or 429 or >= 500;
        }

        if (exception is CosClientException)
        {
            return true;
        }

        if (exception is TencentCloudSDKException cloud)
        {
            var code = cloud.ErrorCode ?? string.Empty;
            return code.Contains("InternalError", StringComparison.OrdinalIgnoreCase) ||
                   code.Contains("RequestLimitExceeded", StringComparison.OrdinalIgnoreCase) ||
                   code.Contains("ResourceUnavailable", StringComparison.OrdinalIgnoreCase) ||
                   cloud.InnerException is HttpRequestException or IOException or TimeoutException;
        }

        return false;
    }
}
