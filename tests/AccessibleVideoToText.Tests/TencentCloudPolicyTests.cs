using AccessibleVideoToText.Infrastructure;
using TencentCloud.Asr.V20190614.Models;

namespace AccessibleVideoToText.Tests;

[TestClass]
public sealed class TencentCloudPolicyTests
{
    [TestMethod]
    public void RequestFactory_LocksOrdinaryRecordingParametersAndDisablesAddOns()
    {
        var request = TencentAsrRequestFactory.Create(new Uri("https://example.cos.ap-shanghai.myqcloud.com/random.mp3?signature=redacted"));

        Assert.AreEqual("16k_zh", request.EngineModelType);
        Assert.AreEqual(1UL, request.ChannelNum);
        Assert.AreEqual(0UL, request.SourceType);
        Assert.AreEqual(2UL, request.ResTextFormat);
        Assert.AreEqual(1L, request.ConvertNumMode);
        Assert.AreEqual(0L, request.FilterDirty);
        Assert.AreEqual(0L, request.FilterPunc);
        Assert.AreEqual(1L, request.FilterModal);
        Assert.AreEqual(0L, request.SpeakerDiarization);
        Assert.AreEqual(0L, request.EmotionRecognition);
        Assert.AreEqual(0L, request.EmotionalEnergy);
        Assert.IsNull(request.SpeakerNumber);
        Assert.IsNull(request.SpeakerRoles);
        Assert.IsNull(request.HotwordId);
        Assert.IsNull(request.HotwordList);
        Assert.IsNull(request.KeyWordLibIdList);
        Assert.IsNull(request.Extra);
    }

    [TestMethod]
    public void RequestFactory_RejectsNonHttpsUrl()
    {
        Assert.ThrowsExactly<ArgumentException>(() =>
            TencentAsrRequestFactory.Create(new Uri("http://example.invalid/audio.mp3")));
    }

    [TestMethod]
    public void ResultParser_PrefersSentenceDetailsAndReturnsOnlyBody()
    {
        var status = new TencentCloud.Asr.V20190614.Models.TaskStatus
        {
            Result = "[0:0.020,0:1.000] 不应采用。",
            ResultDetail =
            [
                new SentenceDetail { FinalSentence = "第一句。" },
                new SentenceDetail { FinalSentence = " 第二句！ " }
            ]
        };

        Assert.AreEqual("第一句。第二句！", TencentAsrResultParser.ExtractBody(status));
    }

    [TestMethod]
    public void ResultParser_StripsTimestampsFromFallbackResult()
    {
        var status = new TencentCloud.Asr.V20190614.Models.TaskStatus
        {
            Result = "[0:0.020,0:1.000] 第一句。\r\n[0:1.100,0:2.000] 第二句！\n"
        };

        Assert.AreEqual("第一句。第二句！", TencentAsrResultParser.ExtractBody(status));
    }

    [TestMethod]
    public void CosPolicy_UsesPrivateApplicationPrefixAndOneDayRules()
    {
        Assert.AreEqual("accessible-video-to-text-1250000000", TencentCosPolicy.BaseBucketName("1250000000"));
        var objectKey = TencentCosPolicy.CreateObjectKey();
        Assert.IsTrue(TencentCosPolicy.IsOwnedObjectKey(objectKey));
        StringAssert.StartsWith(objectKey, TencentCosPolicy.ObjectPrefix);
        StringAssert.EndsWith(objectKey, ".mp3");
        Assert.IsFalse(TencentCosPolicy.IsOwnedObjectKey("other-prefix/file.mp3"));

        var rules = TencentCosPolicy.CreateLifecycleRules();
        Assert.HasCount(2, rules);
        Assert.IsTrue(rules.All(rule => rule.status == "Enabled" && rule.filter.prefix == TencentCosPolicy.ObjectPrefix));
        Assert.AreEqual(1, rules.Single(rule => rule.expiration is not null).expiration.days);
        Assert.AreEqual(1, rules.Single(rule => rule.abortIncompleteMultiUpload is not null).abortIncompleteMultiUpload.daysAfterInitiation);
    }
}
