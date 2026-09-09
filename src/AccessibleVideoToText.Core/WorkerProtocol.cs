using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;

namespace AccessibleVideoToText.Core;

public static partial class WorkerProtocol
{
    public const string Name = "feichuan-worker";

    public const int Version = 1;

    public const int MaximumMessageBytes = 1024 * 1024;

    private static readonly HashSet<string> ForbiddenKeys = new(StringComparer.Ordinal)
    {
        "authorization",
        "browsercontext",
        "browsertoken",
        "cookie",
        "cookies",
        "mediaurl",
        "mediatoken",
        "qsignature",
        "secretid",
        "secretkey",
        "signature",
        "signedurl",
        "token"
    };

    public static string CreateRequest<TPayload>(string requestId, string messageType, TPayload payload)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(requestId);
        ArgumentException.ThrowIfNullOrWhiteSpace(messageType);
        var payloadElement = JsonSerializer.SerializeToElement(payload);
        EnsurePublicPayload(payloadElement);
        var message = new WorkerMessage(Name, Version, requestId, messageType, payloadElement);
        var json = JsonSerializer.Serialize(message);
        EnsureWithinLimit(json);
        return json;
    }

    public static WorkerMessage ParseMessage(string json)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(json);
        EnsureWithinLimit(json);
        WorkerMessage message;
        try
        {
            message = JsonSerializer.Deserialize<WorkerMessage>(json)
                ?? throw new WorkerProtocolException("invalid_message", "工作进程返回了空消息。");
        }
        catch (JsonException exception)
        {
            throw new WorkerProtocolException("invalid_json", "工作进程返回了无效 JSON。", exception);
        }

        if (!string.Equals(message.Protocol, Name, StringComparison.Ordinal))
        {
            throw new WorkerProtocolException("protocol_mismatch", "工作进程协议名称不匹配。");
        }

        if (message.Version != Version)
        {
            throw new WorkerProtocolException("version_mismatch", "工作进程协议版本不兼容。");
        }

        if (string.IsNullOrWhiteSpace(message.Id) || message.Id.Length > 128)
        {
            throw new WorkerProtocolException("invalid_id", "工作进程响应 ID 无效。");
        }

        if (string.IsNullOrWhiteSpace(message.Type) || message.Type.Length > 80)
        {
            throw new WorkerProtocolException("invalid_type", "工作进程响应类型无效。");
        }

        if (message.Payload.ValueKind != JsonValueKind.Object)
        {
            throw new WorkerProtocolException("invalid_payload", "工作进程 payload 必须是 JSON 对象。");
        }

        EnsurePublicPayload(message.Payload);
        return message;
    }

    public static void EnsurePublicPayload(JsonElement value, int depth = 0)
    {
        if (depth > 32)
        {
            throw new WorkerProtocolException("payload_too_deep", "协议消息嵌套层级过多。");
        }

        switch (value.ValueKind)
        {
            case JsonValueKind.Object:
                foreach (var property in value.EnumerateObject())
                {
                    if (ForbiddenKeys.Contains(NormalizeKey(property.Name)))
                    {
                        throw new WorkerProtocolException(
                            "sensitive_field",
                            "协议消息包含禁止传输的敏感字段。");
                    }

                    EnsurePublicPayload(property.Value, depth + 1);
                }

                break;
            case JsonValueKind.Array:
                foreach (var item in value.EnumerateArray())
                {
                    EnsurePublicPayload(item, depth + 1);
                }

                break;
            case JsonValueKind.String:
                if (SensitiveValueRegex().IsMatch(value.GetString() ?? string.Empty))
                {
                    throw new WorkerProtocolException(
                        "sensitive_value",
                        "协议消息包含禁止传输的敏感内容。");
                }

                break;
        }
    }

    public static string SanitizeDiagnostic(string text)
    {
        if (string.IsNullOrWhiteSpace(text))
        {
            return string.Empty;
        }

        var sanitized = SignedUrlRegex().Replace(text, "[已隐藏签名地址]");
        sanitized = LabeledSecretRegex().Replace(sanitized, "$1=[已隐藏]");
        return sanitized.Length <= 2048 ? sanitized : sanitized[..2048] + "…";
    }

    private static string NormalizeKey(string key)
    {
        var builder = new StringBuilder(key.Length);
        foreach (var character in key)
        {
            if (character is >= 'a' and <= 'z')
            {
                builder.Append(character);
            }
            else if (character is >= 'A' and <= 'Z')
            {
                builder.Append(char.ToLowerInvariant(character));
            }
            else if (character is >= '0' and <= '9')
            {
                builder.Append(character);
            }
        }

        return builder.ToString();
    }

    private static void EnsureWithinLimit(string json)
    {
        if (Encoding.UTF8.GetByteCount(json) > MaximumMessageBytes)
        {
            throw new WorkerProtocolException("message_too_large", "协议消息超过大小限制。");
        }
    }

    [GeneratedRegex(
        @"(?i)(?:\bAKID[0-9A-Za-z]{8,}\b|\b(?:authorization|cookie|q-signature|x-cos-security-token)\s*[:=]|https?://\S+\?(?:\S*&)?(?:q-signature|x-cos-security-token)=|https?://\S*(?:xet\.tech|xiaoeknow\.com)/\S*\.(?:m3u8|ts)\?\S+)",
        RegexOptions.CultureInvariant)]
    private static partial Regex SensitiveValueRegex();

    [GeneratedRegex(
        @"(?i)(?:https?://\S+\?(?:\S*&)?(?:q-signature|x-cos-security-token)=\S+|https?://\S*(?:xet\.tech|xiaoeknow\.com)/\S*\.(?:m3u8|ts)\?\S+)",
        RegexOptions.CultureInvariant)]
    private static partial Regex SignedUrlRegex();

    [GeneratedRegex(
        @"(?i)\b(secretid|secretkey|authorization|cookie|q-signature|x-cos-security-token)\s*[:=]\s*\S+",
        RegexOptions.CultureInvariant)]
    private static partial Regex LabeledSecretRegex();
}

public sealed record WorkerMessage(
    [property: JsonPropertyName("protocol")] string Protocol,
    [property: JsonPropertyName("version")] int Version,
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("type")] string Type,
    [property: JsonPropertyName("payload")] JsonElement Payload);

public sealed class WorkerProtocolException : Exception
{
    public WorkerProtocolException(string code, string message, Exception? innerException = null)
        : base(message, innerException)
    {
        Code = code;
    }

    public string Code { get; }
}
