namespace AccessibleVideoToText.Core;

public enum JobStage
{
    Waiting,
    Probing,
    ConvertingMp3,
    PreparingCloudAudio,
    Uploading,
    SubmittingTranscription,
    CloudTranscribing,
    WritingTxt,
    CleaningUp,
    Succeeded,
    PartiallySucceeded,
    Failed,
    Cancelled,
    Recoverable
}

public static class JobStageExtensions
{
    public static string ToAccessibleText(this JobStage stage) => stage switch
    {
        JobStage.Waiting => "等待",
        JobStage.Probing => "探测",
        JobStage.ConvertingMp3 => "转换MP3",
        JobStage.PreparingCloudAudio => "准备云端音频",
        JobStage.Uploading => "上传",
        JobStage.SubmittingTranscription => "提交识别",
        JobStage.CloudTranscribing => "云端识别中",
        JobStage.WritingTxt => "写入TXT",
        JobStage.CleaningUp => "清理",
        JobStage.Succeeded => "成功",
        JobStage.PartiallySucceeded => "部分成功",
        JobStage.Failed => "失败",
        JobStage.Cancelled => "已取消",
        JobStage.Recoverable => "待恢复",
        _ => throw new ArgumentOutOfRangeException(nameof(stage), stage, null)
    };

    public static bool IsTerminal(this JobStage stage) => stage is
        JobStage.Succeeded or
        JobStage.PartiallySucceeded or
        JobStage.Failed or
        JobStage.Cancelled;
}

