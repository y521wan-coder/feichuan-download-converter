using AccessibleVideoToText.Core;

namespace AccessibleVideoToText.Tests;

[TestClass]
public sealed class CloudRequestPolicyTests
{
    [TestMethod]
    public void LockedPolicy_UsesOnlyOrdinaryRecordingRecognition()
    {
        var policy = CloudRequestPolicy.Locked;

        Assert.AreEqual("2019-06-14", policy.ApiVersion);
        Assert.AreEqual("asr.tencentcloudapi.com", policy.Endpoint);
        Assert.AreEqual("16k_zh", policy.EngineModelType);
        Assert.AreEqual(1UL, policy.ChannelNum);
        Assert.AreEqual(0UL, policy.SourceType);
        Assert.AreEqual(2UL, policy.ResTextFormat);
        Assert.AreEqual(1UL, policy.ConvertNumMode);
        Assert.AreEqual(0UL, policy.FilterDirty);
        Assert.AreEqual(0UL, policy.FilterPunc);
        Assert.AreEqual(1UL, policy.FilterModal);
        Assert.IsFalse(policy.EnableSpeakerDiarization);
        Assert.IsFalse(policy.EnableEmotionalRecognition);
        Assert.IsFalse(policy.EnableSemanticSegmentation);
        Assert.IsFalse(policy.EnableOralToWritten);
        Assert.AreEqual("ap-shanghai", policy.CosRegion);
        Assert.IsFalse(policy.EngineModelType.Contains("large", StringComparison.OrdinalIgnoreCase));
    }

    [TestMethod]
    public void Credentials_ToStringNeverLeaksSecrets()
    {
        var credentials = new CloudCredentials("123456", "AKID-not-real", "not-a-real-key");

        var text = credentials.ToString();

        Assert.IsFalse(text.Contains(credentials.SecretId, StringComparison.Ordinal));
        Assert.IsFalse(text.Contains(credentials.SecretKey, StringComparison.Ordinal));
    }
}
