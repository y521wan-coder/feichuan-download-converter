using System.Globalization;
using System.Text.RegularExpressions;

namespace AccessibleVideoToText.Core;

public sealed record XiaoeCatalogItem(string Title, DateTime? PublishedAt);

public sealed record XiaoeCourseCatalog(
    string Title,
    string PageUrl,
    int ReportedUpdateCount,
    IReadOnlyList<XiaoeCatalogItem> Videos);

public static partial class XiaoeCourseCatalogParser
{
    public static XiaoeCatalogItem? ParseVideoItem(IEnumerable<string> rawTexts)
    {
        ArgumentNullException.ThrowIfNull(rawTexts);
        var texts = rawTexts
            .Select(value => (value ?? string.Empty).Trim())
            .Where(value => value.Length > 0 && value != "|")
            .ToArray();
        if (texts.Length == 0 || !texts.Any(value => value == "直播"))
        {
            return null;
        }

        var title = texts[0];
        if (title.Length == 0 || title is "直播" or "图文")
        {
            return null;
        }

        DateTime? publishedAt = null;
        foreach (var value in texts.Skip(1))
        {
            if (DateTime.TryParseExact(
                    value,
                    ["yyyy.MM.dd HH:mm", "yyyy.MM.dd"],
                    CultureInfo.InvariantCulture,
                    DateTimeStyles.None,
                    out var parsed))
            {
                publishedAt = parsed;
                break;
            }
        }

        return new XiaoeCatalogItem(title, publishedAt);
    }

    public static IReadOnlyList<XiaoeCatalogItem> SortChronologically(
        IEnumerable<XiaoeCatalogItem> items)
    {
        ArgumentNullException.ThrowIfNull(items);
        return items
            .GroupBy(item => item.Title.Trim(), StringComparer.Ordinal)
            .Select(group => group.First())
            .OrderBy(item => item.PublishedAt ?? DateTime.MaxValue)
            .ThenBy(item => item.Title, StringComparer.CurrentCulture)
            .ToArray();
    }

    public static int ParseReportedUpdateCount(IEnumerable<string> rawTexts)
    {
        ArgumentNullException.ThrowIfNull(rawTexts);
        foreach (var value in rawTexts)
        {
            var match = UpdatedCountPattern().Match(value ?? string.Empty);
            if (match.Success && int.TryParse(match.Groups[1].Value, out var count))
            {
                return count;
            }
        }

        return 0;
    }

    public static bool IsSupportedCoursePage(string pageUrl)
    {
        if (!Uri.TryCreate(pageUrl, UriKind.Absolute, out var uri) ||
            uri.Scheme is not ("http" or "https"))
        {
            return false;
        }

        var host = uri.Host;
        var supportedHost = host.EndsWith(".xet.pomoho.com", StringComparison.OrdinalIgnoreCase) ||
            host.EndsWith(".xiaoeknow.com", StringComparison.OrdinalIgnoreCase) ||
            host.EndsWith(".xet.citv.cn", StringComparison.OrdinalIgnoreCase);
        return supportedHost &&
            (uri.AbsolutePath.Contains("/p/course/column/", StringComparison.OrdinalIgnoreCase) ||
             uri.AbsolutePath.Contains("/v4/course/alive/", StringComparison.OrdinalIgnoreCase));
    }

    [GeneratedRegex(@"已更新\s*(\d+)\s*期", RegexOptions.CultureInvariant)]
    private static partial Regex UpdatedCountPattern();
}
