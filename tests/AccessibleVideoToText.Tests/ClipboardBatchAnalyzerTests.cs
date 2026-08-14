using AccessibleVideoToText.Core;

namespace AccessibleVideoToText.Tests;

[TestClass]
public sealed class ClipboardBatchAnalyzerTests
{
    [TestMethod]
    public void FormatRouter_PrefersRealFileDropThenFallsBackToText()
    {
        Assert.AreEqual(ClipboardPayloadKind.FileDrop, ClipboardFormatRouter.Choose(true, true));
        Assert.AreEqual(ClipboardPayloadKind.FileDrop, ClipboardFormatRouter.Choose(true, false));
        Assert.AreEqual(ClipboardPayloadKind.Text, ClipboardFormatRouter.Choose(false, true));
        Assert.AreEqual(ClipboardPayloadKind.None, ClipboardFormatRouter.Choose(false, false));
    }

    [TestMethod]
    public void Analyze_PreservesClipboardOrderAndClassifiesFiles()
    {
        var inspector = new FakePathInspector(
            @"D:\媒体\一.mp4",
            @"D:\媒体\二.mp3",
            @"D:\媒体\背景.m4a",
            @"D:\媒体\三.custom");
        var analyzer = new ClipboardBatchAnalyzer(inspector);

        var result = analyzer.Analyze([
            @"D:\媒体\一.mp4",
            @"D:\媒体\二.mp3",
            @"D:\媒体\背景.m4a",
            @"D:\媒体\三.custom"]);

        Assert.IsFalse(result.RejectedForLimit);
        CollectionAssert.AreEqual(
            new[] { "一.mp4", "二.mp3", "背景.m4a", "三.custom" },
            result.Accepted.Select(item => item.FileName).ToArray());
        CollectionAssert.AreEqual(
            new[] { MediaKind.Video, MediaKind.Mp3, MediaKind.Audio, MediaKind.ProbeCandidate },
            result.Accepted.Select(item => item.Kind).ToArray());
    }

    [TestMethod]
    public void Analyze_DeduplicatesCanonicalPathsCaseInsensitively()
    {
        var inspector = new FakePathInspector(@"D:\媒体\样例.mp4");
        var analyzer = new ClipboardBatchAnalyzer(inspector);

        var result = analyzer.Analyze([@"D:\媒体\样例.mp4", @"d:\媒体\样例.mp4"]);

        Assert.HasCount(1, result.Accepted);
        Assert.AreEqual(1, result.DuplicateCount);
    }

    [TestMethod]
    public void Analyze_RejectsEntireBatchWhenMoreThanOneHundredValidFiles()
    {
        var paths = Enumerable.Range(1, 101).Select(index => $@"D:\媒体\{index}.mp4").ToArray();
        var inspector = new FakePathInspector(paths);
        var analyzer = new ClipboardBatchAnalyzer(inspector);

        var result = analyzer.Analyze(paths);

        Assert.IsTrue(result.RejectedForLimit);
        Assert.IsEmpty(result.Accepted);
        StringAssert.Contains(result.Summary, "整批未加入");
    }

    [TestMethod]
    public void Analyze_SkipsDirectoriesMissingFilesAndKnownNonMedia()
    {
        var inspector = new FakePathInspector(@"D:\媒体\图片.jpg", @"D:\媒体\视频.mkv");
        inspector.Directories.Add(@"D:\媒体\文件夹");
        var analyzer = new ClipboardBatchAnalyzer(inspector);

        var result = analyzer.Analyze([
            @"D:\媒体\文件夹",
            @"D:\媒体\不存在.mp4",
            @"D:\媒体\图片.jpg",
            @"D:\媒体\视频.mkv"]);

        Assert.HasCount(1, result.Accepted);
        Assert.AreEqual(3, result.SkippedCount);
        Assert.AreEqual("视频.mkv", result.Accepted[0].FileName);
    }

    private sealed class FakePathInspector : IPathInspector
    {
        public FakePathInspector(params string[] files)
        {
            Files = new HashSet<string>(files, StringComparer.OrdinalIgnoreCase);
        }

        public HashSet<string> Files { get; }

        public HashSet<string> Directories { get; } = new(StringComparer.OrdinalIgnoreCase);

        public bool FileExists(string path) => Files.Contains(path);

        public bool DirectoryExists(string path) => Directories.Contains(path);

        public string GetFullPath(string path)
        {
            var normalized = path.Replace('/', '\\');
            return normalized.Length >= 2 && normalized[1] == ':'
                ? char.ToUpperInvariant(normalized[0]) + normalized[1..]
                : normalized;
        }
    }
}
