namespace AccessibleVideoToText.Core;

public sealed record CloudRequestPolicy(
    string ApiVersion,
    string Endpoint,
    string EngineModelType,
    ulong ChannelNum,
    ulong SourceType,
    ulong ResTextFormat,
    ulong ConvertNumMode,
    ulong FilterDirty,
    ulong FilterPunc,
    ulong FilterModal,
    bool EnableSpeakerDiarization,
    bool EnableEmotionalRecognition,
    bool EnableSemanticSegmentation,
    bool EnableOralToWritten,
    string CosRegion)
{
    public static CloudRequestPolicy Locked { get; } = new(
        ApiVersion: "2019-06-14",
        Endpoint: "asr.tencentcloudapi.com",
        EngineModelType: "16k_zh",
        ChannelNum: 1,
        SourceType: 0,
        ResTextFormat: 2,
        ConvertNumMode: 1,
        FilterDirty: 0,
        FilterPunc: 0,
        FilterModal: 1,
        EnableSpeakerDiarization: false,
        EnableEmotionalRecognition: false,
        EnableSemanticSegmentation: false,
        EnableOralToWritten: false,
        CosRegion: "ap-shanghai");
}

