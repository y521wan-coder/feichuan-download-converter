using System.Text.RegularExpressions;
using TencentCloud.Asr.V20190614.Models;

namespace AccessibleVideoToText.Infrastructure;

public static partial class TencentAsrResultParser
{
    public static string ExtractBody(TencentCloud.Asr.V20190614.Models.TaskStatus status)
    {
        ArgumentNullException.ThrowIfNull(status);

        var detailedSentences = status.ResultDetail?
            .Select(sentence => sentence.FinalSentence)
            .Where(sentence => !string.IsNullOrWhiteSpace(sentence))
            .Select(sentence => sentence!.Trim())
            .ToArray();

        if (detailedSentences is { Length: > 0 })
        {
            return string.Concat(detailedSentences).Trim();
        }

        if (string.IsNullOrWhiteSpace(status.Result))
        {
            return string.Empty;
        }

        var withoutTimestamps = TimestampPrefixRegex().Replace(status.Result, string.Empty);
        var lines = withoutTimestamps
            .Replace("\r\n", "\n", StringComparison.Ordinal)
            .Replace('\r', '\n')
            .Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        return string.Concat(lines).Trim();
    }

    [GeneratedRegex(@"(?m)^\s*\[\d+:[\d.]+,\d+:[\d.]+\]\s*")]
    private static partial Regex TimestampPrefixRegex();
}
