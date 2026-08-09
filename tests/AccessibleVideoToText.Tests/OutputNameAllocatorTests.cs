using AccessibleVideoToText.Core;

namespace AccessibleVideoToText.Tests;

[TestClass]
public sealed class OutputNameAllocatorTests
{
    [TestMethod]
    public void FindAvailable_UsesSameNumberForMp3AndTxt()
    {
        var existing = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            Path.Combine(@"D:\输出", "课程.mp3"),
            Path.Combine(@"D:\输出", "课程 (1).txt")
        };

        var result = OutputNameAllocator.FindAvailable(
            @"D:\输出",
            "课程",
            requireMp3: true,
            requireTxt: true,
            existing.Contains);

        Assert.AreEqual("课程 (2)", result.Stem);
        Assert.EndsWith("课程 (2).mp3", result.Mp3Path);
        Assert.EndsWith("课程 (2).txt", result.TxtPath);
    }

    [TestMethod]
    public void CreatePartPath_PreservesPartBeforeFinalExtension()
    {
        var partPath = OutputNameAllocator.CreatePartPath(@"D:\输出\课程.mp3");

        StringAssert.Matches(partPath, new System.Text.RegularExpressions.Regex(@"课程\.[0-9a-f]{32}\.part\.mp3$"));
    }

    [TestMethod]
    public void FindAvailable_NeverRequiresAnUnrequestedOutput()
    {
        var existing = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            Path.Combine(@"D:\输出", "录音.mp3")
        };

        var result = OutputNameAllocator.FindAvailable(
            @"D:\输出",
            "录音",
            requireMp3: false,
            requireTxt: true,
            existing.Contains);

        Assert.AreEqual("录音", result.Stem);
        Assert.IsNull(result.Mp3Path);
        Assert.EndsWith("录音.txt", result.TxtPath);
    }
}

