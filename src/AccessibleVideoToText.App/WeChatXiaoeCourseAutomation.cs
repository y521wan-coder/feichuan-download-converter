using System.Runtime.InteropServices;
using System.Windows.Automation;
using AccessibleVideoToText.Core;

namespace AccessibleVideoToText.App;

internal sealed record WeChatXiaoeCourseSelection(
    string Title,
    string PageUrl,
    int ReportedUpdateCount,
    IReadOnlyList<XiaoeCatalogItem> Videos);

internal sealed class WeChatXiaoeCourseAutomation
{
    private const string WeChatWindowClass = "Chrome_WidgetWin_0";
    private static readonly TimeSpan PageTimeout = TimeSpan.FromSeconds(15);

    public Task<WeChatXiaoeCourseSelection> DiscoverCurrentCourseAsync(
        CancellationToken cancellationToken) =>
        Task.Run(() => DiscoverCurrentCourse(cancellationToken), cancellationToken);

    public Task OpenEpisodeAsync(string title, CancellationToken cancellationToken) =>
        Task.Run(() => OpenEpisode(title, cancellationToken), cancellationToken);

    public Task ReturnToCatalogAsync(CancellationToken cancellationToken) =>
        Task.Run(() => ReturnToCatalog(cancellationToken), cancellationToken);

    private static WeChatXiaoeCourseSelection DiscoverCurrentCourse(
        CancellationToken cancellationToken)
    {
        var window = FindCourseWindow();
        var document = FindDocument(window);
        var pageUrl = ReadDocumentUrl(document);
        if (!XiaoeCourseCatalogParser.IsSupportedCoursePage(pageUrl))
        {
            throw new InvalidDataException(
                "微信中没有打开受支持的小鹅通课程页。请先在微信里打开自己已购买的课程。 ");
        }

        if (pageUrl.Contains("/v4/course/alive/", StringComparison.OrdinalIgnoreCase))
        {
            InvokeBack(window);
            document = WaitForDocument(
                window,
                url => url.Contains("/p/course/column/", StringComparison.OrdinalIgnoreCase),
                cancellationToken);
        }

        pageUrl = ReadDocumentUrl(document);
        if (!pageUrl.Contains("/p/course/column/", StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException(
                "当前页面不是小鹅通课程目录。请先打开这次已购买课程的目录页。 ");
        }

        LoadEntireCatalog(window, document, cancellationToken);
        document = FindDocument(window);
        var allTexts = ReadTextValues(document);
        var videos = ReadVideoItems(document);
        if (videos.Count == 0)
        {
            throw new InvalidDataException(
                "课程目录中没有找到可下载的直播回放。图文和推荐内容不会下载。 ");
        }

        return new WeChatXiaoeCourseSelection(
            document.Current.Name.Trim(),
            ReadDocumentUrl(document),
            XiaoeCourseCatalogParser.ParseReportedUpdateCount(allTexts),
            XiaoeCourseCatalogParser.SortChronologically(videos));
    }

    private static void OpenEpisode(string title, CancellationToken cancellationToken)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(title);
        var window = FindCourseWindow();
        var document = FindDocument(window);
        var pageUrl = ReadDocumentUrl(document);
        if (pageUrl.Contains("/v4/course/alive/", StringComparison.OrdinalIgnoreCase))
        {
            InvokeBack(window);
            document = WaitForDocument(
                window,
                url => url.Contains("/p/course/column/", StringComparison.OrdinalIgnoreCase),
                cancellationToken);
        }

        var target = FindVideoGroup(document, title);
        if (target is null)
        {
            LoadEntireCatalog(window, document, cancellationToken);
            document = FindDocument(window);
            target = FindVideoGroup(document, title);
        }

        if (target is null)
        {
            throw new InvalidDataException($"课程目录中找不到“{title}”，请刷新微信课程页后重试。 ");
        }

        if (target.TryGetCurrentPattern(ScrollItemPattern.Pattern, out var scrollObject) &&
            scrollObject is ScrollItemPattern scrollItem)
        {
            scrollItem.ScrollIntoView();
            Thread.Sleep(250);
        }

        if (!target.TryGetCurrentPattern(InvokePattern.Pattern, out var invokeObject) ||
            invokeObject is not InvokePattern invokePattern)
        {
            throw new InvalidDataException("微信课程条目当前无法打开，请刷新课程页后重试。 ");
        }

        invokePattern.Invoke();
        WaitForDocument(
            window,
            url => url.Contains("/v4/course/alive/", StringComparison.OrdinalIgnoreCase),
            cancellationToken);
    }

    private static void ReturnToCatalog(CancellationToken cancellationToken)
    {
        var window = FindCourseWindow();
        var document = FindDocument(window);
        if (ReadDocumentUrl(document).Contains("/p/course/column/", StringComparison.OrdinalIgnoreCase))
        {
            return;
        }

        InvokeBack(window);
        WaitForDocument(
            window,
            url => url.Contains("/p/course/column/", StringComparison.OrdinalIgnoreCase),
            cancellationToken);
    }

    private static AutomationElement FindCourseWindow()
    {
        var windows = AutomationElement.RootElement.FindAll(
            TreeScope.Children,
            new PropertyCondition(AutomationElement.ClassNameProperty, WeChatWindowClass));
        foreach (AutomationElement window in windows)
        {
            try
            {
                if (!window.Current.Name.Contains("微信", StringComparison.Ordinal))
                {
                    continue;
                }

                var document = FindDocument(window);
                if (XiaoeCourseCatalogParser.IsSupportedCoursePage(ReadDocumentUrl(document)))
                {
                    return window;
                }
            }
            catch (ElementNotAvailableException)
            {
            }
        }

        throw new InvalidDataException(
            "没有找到正在显示小鹅通课程的微信窗口。请先在微信中打开自己已购买的课程。 ");
    }

    private static AutomationElement FindDocument(AutomationElement window)
    {
        return window.FindFirst(
                TreeScope.Descendants,
                new PropertyCondition(
                    AutomationElement.ControlTypeProperty,
                    ControlType.Document))
            ?? throw new InvalidDataException("无法读取微信中的课程页面，请重新打开该页面后重试。 ");
    }

    private static AutomationElement WaitForDocument(
        AutomationElement window,
        Func<string, bool> predicate,
        CancellationToken cancellationToken)
    {
        var deadline = DateTime.UtcNow + PageTimeout;
        while (DateTime.UtcNow < deadline)
        {
            cancellationToken.ThrowIfCancellationRequested();
            try
            {
                var document = FindDocument(window);
                if (predicate(ReadDocumentUrl(document)))
                {
                    return document;
                }
            }
            catch (ElementNotAvailableException)
            {
            }

            Thread.Sleep(200);
        }

        throw new InvalidDataException("等待微信课程页面打开超时，请确认网络正常后重试。 ");
    }

    private static void InvokeBack(AutomationElement window)
    {
        var buttons = window.FindAll(
            TreeScope.Descendants,
            new PropertyCondition(
                AutomationElement.ControlTypeProperty,
                ControlType.Button));
        foreach (AutomationElement button in buttons)
        {
            try
            {
                if (!button.Current.Name.Contains("后退", StringComparison.Ordinal) ||
                    !button.TryGetCurrentPattern(InvokePattern.Pattern, out var pattern) ||
                    pattern is not InvokePattern invokePattern)
                {
                    continue;
                }

                invokePattern.Invoke();
                return;
            }
            catch (ElementNotAvailableException)
            {
            }
        }

        throw new InvalidDataException("找不到微信页面的后退按钮，请手动返回课程目录后重试。 ");
    }

    private static void LoadEntireCatalog(
        AutomationElement window,
        AutomationElement document,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var handle = new IntPtr(window.Current.NativeWindowHandle);
        if (handle == IntPtr.Zero || !SetForegroundWindow(handle))
        {
            throw new InvalidDataException("无法激活微信课程窗口，请把微信窗口恢复后重试。 ");
        }

        SendKeys.SendWait("^{HOME}");
        Thread.Sleep(300);
        var expected = XiaoeCourseCatalogParser.ParseReportedUpdateCount(ReadTextValues(document));
        var previousCount = -1;
        var stablePasses = 0;
        for (var pass = 0; pass < 20; pass++)
        {
            cancellationToken.ThrowIfCancellationRequested();
            SendKeys.SendWait("^{END}");
            Thread.Sleep(800);
            document = FindDocument(window);
            var count = document.FindAll(
                TreeScope.Descendants,
                new PropertyCondition(AutomationElement.ClassNameProperty, "content-box")).Count;
            if (expected > 0 && count >= expected)
            {
                return;
            }

            stablePasses = count == previousCount ? stablePasses + 1 : 0;
            if (stablePasses >= 2)
            {
                return;
            }
            previousCount = count;
        }
    }

    private static IReadOnlyList<XiaoeCatalogItem> ReadVideoItems(AutomationElement document)
    {
        var result = new List<XiaoeCatalogItem>();
        var groups = document.FindAll(
            TreeScope.Descendants,
            new PropertyCondition(AutomationElement.ClassNameProperty, "content-box"));
        foreach (AutomationElement group in groups)
        {
            var item = XiaoeCourseCatalogParser.ParseVideoItem(ReadTextValues(group));
            if (item is not null)
            {
                result.Add(item);
            }
        }

        return result;
    }

    private static AutomationElement? FindVideoGroup(AutomationElement document, string title)
    {
        var groups = document.FindAll(
            TreeScope.Descendants,
            new PropertyCondition(AutomationElement.ClassNameProperty, "content-box"));
        foreach (AutomationElement group in groups)
        {
            var parsed = XiaoeCourseCatalogParser.ParseVideoItem(ReadTextValues(group));
            if (parsed is not null && string.Equals(parsed.Title, title, StringComparison.Ordinal))
            {
                return group;
            }
        }

        return null;
    }

    private static IReadOnlyList<string> ReadTextValues(AutomationElement root)
    {
        var result = new List<string>();
        var values = root.FindAll(
            TreeScope.Descendants,
            new PropertyCondition(
                AutomationElement.ControlTypeProperty,
                ControlType.Text));
        foreach (AutomationElement value in values)
        {
            try
            {
                if (!string.IsNullOrWhiteSpace(value.Current.Name))
                {
                    result.Add(value.Current.Name);
                }
            }
            catch (ElementNotAvailableException)
            {
            }
        }

        return result;
    }

    private static string ReadDocumentUrl(AutomationElement document)
    {
        if (document.TryGetCurrentPattern(ValuePattern.Pattern, out var pattern) &&
            pattern is ValuePattern valuePattern)
        {
            return valuePattern.Current.Value ?? string.Empty;
        }

        return string.Empty;
    }

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetForegroundWindow(IntPtr hWnd);
}
