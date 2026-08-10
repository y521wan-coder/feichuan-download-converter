using AccessibleVideoToText.Core;

namespace AccessibleVideoToText.Tests;

[TestClass]
public sealed class DownloadPipelinePlannerTests
{
    [TestMethod]
    public void Create_ClassifiesVideoMp3AndUnsupportedWithoutDeletingResults()
    {
        var plans = DownloadPipelinePlanner.Create([
            @"D:\result\one.mp4",
            @"D:\result\two.mp3",
            @"D:\result\cover.jpg",
            @"D:\result\audio.m4a",
            @"d:\result\ONE.mp4"
        ]);

        Assert.HasCount(4, plans);
        CollectionAssert.AreEqual(
            new[]
            {
                DownloadedMediaAction.ConvertVideo,
                DownloadedMediaAction.UseExistingMp3,
                DownloadedMediaAction.Skip,
                DownloadedMediaAction.Skip
            },
            plans.Select(plan => plan.Action).ToArray());
        Assert.IsTrue(plans.Where(plan => plan.Action == DownloadedMediaAction.Skip)
            .All(plan => plan.Reason.Contains("保留下载结果", StringComparison.Ordinal)));
    }
}
