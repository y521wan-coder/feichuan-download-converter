using AccessibleVideoToText.Core;

namespace AccessibleVideoToText.Tests;

[TestClass]
public sealed class XiaoeCourseCatalogTests
{
    [TestMethod]
    public void ParseVideoItem_AcceptsLiveAndRejectsGraphic()
    {
        var video = XiaoeCourseCatalogParser.ParseVideoItem([
            "万周迎 | 品读老子道篇第一",
            "直播 ",
            "2026.01.28 19:30 ",
            "已学3%"]);
        var graphic = XiaoeCourseCatalogParser.ParseVideoItem([
            "入群交流（太极经典文化课）",
            "图文 ",
            "2026.01.23"]);

        Assert.IsNotNull(video);
        Assert.AreEqual("万周迎 | 品读老子道篇第一", video.Title);
        Assert.AreEqual(new DateTime(2026, 1, 28, 19, 30, 0), video.PublishedAt);
        Assert.IsNull(graphic);
    }

    [TestMethod]
    public void SortChronologically_DeduplicatesAndKeepsStableNumbering()
    {
        var result = XiaoeCourseCatalogParser.SortChronologically([
            new("第三讲", new DateTime(2026, 3, 1)),
            new("第一讲", new DateTime(2026, 1, 1)),
            new("第二讲", new DateTime(2026, 2, 1)),
            new("第一讲", new DateTime(2026, 1, 1))]);

        CollectionAssert.AreEqual(
            new[] { "第一讲", "第二讲", "第三讲" },
            result.Select(item => item.Title).ToArray());
    }

    [TestMethod]
    public void CoursePageValidation_AllowsKnownXiaoeHostsOnly()
    {
        Assert.IsTrue(XiaoeCourseCatalogParser.IsSupportedCoursePage(
            "https://shop.h5.xet.pomoho.com/p/course/column/p_public?type=3"));
        Assert.IsTrue(XiaoeCourseCatalogParser.IsSupportedCoursePage(
            "https://shop.h5.xiaoeknow.com/v4/course/alive/l_public"));
        Assert.IsFalse(XiaoeCourseCatalogParser.IsSupportedCoursePage(
            "https://example.invalid/p/course/column/p_public"));
        Assert.AreEqual(10, XiaoeCourseCatalogParser.ParseReportedUpdateCount([
            "课程说明",
            "已更新10期"]));
    }
}
