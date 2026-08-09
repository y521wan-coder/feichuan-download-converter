using AccessibleVideoToText.Core;
using TencentCloud.Asr.V20190614.Models;

namespace AccessibleVideoToText.Infrastructure;

public static class TencentAsrRequestFactory
{
    public static CreateRecTaskRequest Create(Uri signedAudioUrl)
    {
        ArgumentNullException.ThrowIfNull(signedAudioUrl);
        if (!signedAudioUrl.IsAbsoluteUri || !string.Equals(signedAudioUrl.Scheme, Uri.UriSchemeHttps, StringComparison.OrdinalIgnoreCase))
        {
            throw new ArgumentException("腾讯云音频地址必须是 HTTPS 绝对地址。", nameof(signedAudioUrl));
        }

        var policy = CloudRequestPolicy.Locked;
        return new CreateRecTaskRequest
        {
            EngineModelType = policy.EngineModelType,
            ChannelNum = policy.ChannelNum,
            SourceType = policy.SourceType,
            Url = signedAudioUrl.AbsoluteUri,
            ResTextFormat = policy.ResTextFormat,
            ConvertNumMode = (long)policy.ConvertNumMode,
            FilterDirty = (long)policy.FilterDirty,
            FilterPunc = (long)policy.FilterPunc,
            FilterModal = (long)policy.FilterModal,
            SpeakerDiarization = 0,
            EmotionRecognition = 0,
            EmotionalEnergy = 0
        };
    }
}
